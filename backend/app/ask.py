"""Ask Pina's search: a SearchIntent in, real places out.

READ THIS SECOND, after app/llm.py. There is no LLM in this file, and that is
the design: the model's whole contribution is the SearchIntent it filled in.
Everything here is ordinary SQL against osm_places, so a result can only be a
place that exists in OpenStreetMap, with a real pin.

The shape of the query:

    inside the search box                          (indexed coordinates)
    AND ((category = ... AND cuisine is X)  OR  name contains "trattoria")
    AND has feature 1  AND has feature 2 ...       (GIN index on tags)
    ORDER BY name matched first, then distance from the centre of the box

The OR in the middle is the one subtle part. Only about a third of
restaurants carry a cuisine tag, so "Italian" by tag alone misses most of them.
Names fill much of the gap — "Trattoria da Mario" is Italian whatever its tags
say — so the model also supplies name words, and either kind of evidence counts.
Without a cuisine, name words only move matching names up the list; see
search() for the measured reason.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from collections.abc import Iterable, Mapping

from sqlalchemy import and_, case, func, or_, select, text
from sqlalchemy.orm import Session

from app.geocode import GeocodeError, GeocodeResult, NominatimClient
from app.models import Category, Destination, OsmPlace, Place
from app.schemas import BoundingBox, Feature, PlaceDetails, SearchIntent, Suggestion

#: How many results to return. Enough to choose from, few enough to read.
RESULT_LIMIT = 25

#: How many cuisines to offer the model. The long tail ("uzbek", 1 place) is
#: noise that makes the prompt longer and the model slower.
MAX_CUISINES = 80

#: A named area becomes a box around it at least this big (half-side, in
#: degrees of latitude — ~650 m), so "near the Colosseum" includes the streets
#: around it, not just the monument's own outline...
MIN_AREA_HALF_SPAN = 0.006
#: ...and at most this big (~3.3 km), so a large district does not quietly
#: turn into "the whole city".
MAX_AREA_HALF_SPAN = 0.03

#: A geocoded "area" at least this fraction of the destination's size, on both
#: axes, is the city itself under another name (Lisbon / Lisboa).
WHOLE_CITY_FRACTION = 0.5

#: Geocoder candidates to choose an area from.
AREA_CANDIDATES = 10

#: How far outside the destination's own box an area may be (degrees). The
#: stored box is clamped, so a neighbourhood can sit just beyond its edge.
AREA_SEARCH_MARGIN = 0.05

#: Which OSM tag values count as having a feature. A list of alternatives per
#: feature; each alternative is a JSON fragment for `tags @> ...`, which the
#: GIN index from migration 0006 serves. "only" counts (a vegan-only café is
#: certainly vegan), "limited" and "no" do not.
FEATURE_TAGS: dict[Feature, list[dict[str, str]]] = {
    Feature.OUTDOOR_SEATING: [{"outdoor_seating": "yes"}],
    Feature.VEGETARIAN: [
        {"diet:vegetarian": "yes"},
        {"diet:vegetarian": "only"},
        # Vegan food is vegetarian; OSM rarely tags both.
        {"diet:vegan": "yes"},
        {"diet:vegan": "only"},
    ],
    Feature.VEGAN: [{"diet:vegan": "yes"}, {"diet:vegan": "only"}],
    Feature.GLUTEN_FREE: [
        {"diet:gluten_free": "yes"},
        {"diet:gluten_free": "only"},
    ],
    Feature.WHEELCHAIR: [{"wheelchair": "yes"}],
    Feature.TAKEAWAY: [{"takeaway": "yes"}, {"takeaway": "only"}],
    Feature.DELIVERY: [{"delivery": "yes"}, {"delivery": "only"}],
}


# --- details -----------------------------------------------------------------


def has_feature(tags: Mapping[str, object], feature: Feature) -> bool:
    """The Python twin of the SQL filter, for labelling a result."""
    return any(
        all(tags.get(key) == value for key, value in alternative.items())
        for alternative in FEATURE_TAGS[feature]
    )


def details_from_tags(tags: Mapping[str, object] | None) -> PlaceDetails | None:
    """osm_places.tags -> the PlaceDetails the API sends. None if no tags."""
    if not tags:
        return None
    cuisine = tags.get("cuisine")
    return PlaceDetails(
        cuisine=list(cuisine) if isinstance(cuisine, list) else [],
        opening_hours=_text(tags.get("opening_hours")),
        website=_text(tags.get("website")),
        phone=_text(tags.get("phone")),
        street=_text(tags.get("addr:street")),
        features=[feature for feature in Feature if has_feature(tags, feature)],
    )


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def details_for_refs(db: Session, refs: Iterable[str]) -> dict[str, PlaceDetails]:
    """Details for saved places, looked up by source_ref in one query.

    One query for the whole list rather than one per place — the N+1 shape the
    rest of the API also avoids.
    """
    wanted = {ref for ref in refs if ref}
    if not wanted:
        return {}
    rows = db.execute(
        select(OsmPlace.source_ref, OsmPlace.tags).where(
            OsmPlace.source_ref.in_(wanted), OsmPlace.tags.is_not(None)
        )
    ).all()
    found: dict[str, PlaceDetails] = {}
    for ref, tags in rows:
        details = details_from_tags(tags)
        if details is not None:
            found[ref] = details
    return found


def to_suggestions(
    db: Session, destination_id: int, places: Iterable[OsmPlace]
) -> list[Suggestion]:
    """Cached places -> Suggestions, marking the ones already saved here.

    Shared by the map's /nearby and by /ask, so a place looks the same — same
    details, same "already saved" rule — whichever way you found it.
    """
    saved_refs = set(
        db.scalars(
            select(Place.source_ref).where(
                Place.destination_id == destination_id,
                Place.source_ref.is_not(None),
            )
        )
    )
    return [
        Suggestion(
            source_ref=place.source_ref,
            name=place.name,
            category=Category(place.category),
            latitude=place.latitude,
            longitude=place.longitude,
            already_saved=place.source_ref in saved_refs,
            details=details_from_tags(place.tags),
        )
        for place in places
    ]


# --- where -------------------------------------------------------------------


def destination_box(destination: Destination) -> BoundingBox:
    return BoundingBox(
        south=destination.south,
        west=destination.west,
        north=destination.north,
        east=destination.east,
    )


def cuisines_in(db: Session, box: BoundingBox) -> list[str]:
    """The cuisine values present in a box, commonest first.

    These become the enum the model must choose from. Raw SQL because
    `jsonb_array_elements_text` in FROM is a set-returning function, which is
    clearer written out than coaxed through the ORM.
    """
    rows = db.execute(
        text(
            """
            SELECT cuisine, count(*) AS n
              FROM osm_places,
                   jsonb_array_elements_text(tags -> 'cuisine') AS cuisine
             WHERE latitude BETWEEN :south AND :north
               AND longitude BETWEEN :west AND :east
               AND tags ? 'cuisine'
             GROUP BY cuisine
             ORDER BY n DESC, cuisine
             LIMIT :limit
            """
        ),
        {
            "south": box.south,
            "north": box.north,
            "west": box.west,
            "east": box.east,
            "limit": MAX_CUISINES,
        },
    ).all()
    return [cuisine for cuisine, _ in rows]


@dataclass(frozen=True)
class SearchArea:
    box: BoundingBox
    #: "near Alfama", "in the map view", "in Lisbon".
    label: str
    #: False when a named area could not be found.
    found: bool = True


def _box_around(result: GeocodeResult) -> BoundingBox:
    """A search box for a geocoded area: its own outline, within limits.

    The minimum matters for landmarks (a monument's outline is a few metres
    across), the maximum for big districts. Longitude spans are widened by
    1/cos(latitude) so the box is roughly square on the ground, not on paper.
    """
    lat, lon = result.latitude, result.longitude
    scale = 1 / max(math.cos(math.radians(lat)), 0.1)

    half_lat = min(
        max((result.north - result.south) / 2, MIN_AREA_HALF_SPAN),
        MAX_AREA_HALF_SPAN,
    )
    half_lon = min(
        max((result.east - result.west) / 2, MIN_AREA_HALF_SPAN * scale),
        MAX_AREA_HALF_SPAN * scale,
    )
    return BoundingBox(
        south=max(lat - half_lat, -90),
        north=min(lat + half_lat, 90),
        west=max(lon - half_lon, -180),
        east=min(lon + half_lon, 180),
    )


def resolve_area(
    intent: SearchIntent,
    destination: Destination,
    viewport: BoundingBox | None,
    geocoder: NominatimClient,
) -> SearchArea:
    """Decide where to search.

    A named area wins; then the map's current view; then the whole destination.
    If the area cannot be found the search still runs, more widely, and says
    so — an honest "couldn't find Alfama, searched all of Lisbon" beats both an
    error and a silent substitution.
    """
    fallback = (
        SearchArea(viewport, "in the map view")
        if viewport is not None
        else SearchArea(destination_box(destination), f"in {destination.name}")
    )
    if intent.area is None:
        return fallback

    viewbox = (
        destination.south - AREA_SEARCH_MARGIN,
        destination.west - AREA_SEARCH_MARGIN,
        destination.north + AREA_SEARCH_MARGIN,
        destination.east + AREA_SEARCH_MARGIN,
    )
    try:
        # More candidates than a destination search: the right one is often
        # not Nominatim's first, so we choose by importance below.
        results = geocoder.search(
            intent.area, viewbox=viewbox, limit=AREA_CANDIDATES
        )
    except GeocodeError:
        # The area is a refinement, not the point. An unreachable geocoder
        # should cost the user precision, not the whole answer.
        results = []

    if not results:
        return SearchArea(fallback.box, fallback.label, found=False)

    found = max(results, key=lambda result: result.importance)
    # "pizza in Lisbon" while the destination is called "Lisboa": the "area"
    # is the city itself. Its outline spans most of the destination, so it is
    # searched as the whole destination rather than shrunk to a few blocks
    # around the city's centre point.
    if (found.north - found.south) >= WHOLE_CITY_FRACTION * (
        destination.north - destination.south
    ) and (found.east - found.west) >= WHOLE_CITY_FRACTION * (
        destination.east - destination.west
    ):
        return SearchArea(destination_box(destination), f"in {destination.name}")

    return SearchArea(_box_around(found), f"near {intent.area}")


# --- what --------------------------------------------------------------------


def search(
    db: Session,
    destination_id: int,
    intent: SearchIntent,
    box: BoundingBox,
    limit: int = RESULT_LIMIT,
) -> tuple[int, list[Suggestion]]:
    """Run the intent inside the box. Returns (total matches, the closest few)."""
    conditions = [
        OsmPlace.latitude >= box.south,
        OsmPlace.latitude <= box.north,
        OsmPlace.longitude >= box.west,
        OsmPlace.longitude <= box.east,
    ]

    # autoescape: a "%" or "_" in a word is a literal, not a wildcard.
    name_match = [
        OsmPlace.name.icontains(word, autoescape=True) for word in intent.name_words
    ]
    named = or_(*name_match) if name_match else None

    category = (
        OsmPlace.category == str(intent.category)
        if intent.category is not None
        else None
    )

    if intent.cuisines:
        # With a cuisine, a name word is *alternative evidence* for it: tagged
        # italian, OR "trattoria" in the name — see the module docstring.
        #
        # And a name match also overrides the category guess. Measured: the
        # model filed "pastéis de nata" under restaurant, which hid the café
        # literally called "Fábrica de Pastéis de Nata". The category is the
        # model's guess; a name that says it outright is evidence.
        cuisine = or_(
            *(OsmPlace.tags.contains({"cuisine": [c]}) for c in intent.cuisines)
        )
        typed = and_(category, cuisine) if category is not None else cuisine
        conditions.append(or_(typed, named) if named is not None else typed)
    elif category is not None:
        # Without a cuisine, name words only *rank*. Measured: for "a vegan
        # café" the model offered "vegano", and as a filter that threw away
        # every vegan café not literally named so.
        conditions.append(category)

    # Every feature asked for must be present; any alternative spelling counts.
    for feature in intent.features:
        conditions.append(
            or_(*(OsmPlace.tags.contains(alt) for alt in FEATURE_TAGS[feature]))
        )

    where = and_(*conditions)
    total = db.scalar(select(func.count()).select_from(OsmPlace).where(where)) or 0

    # Squared distance on a locally flat map: good enough to rank places a few
    # kilometres apart, and far cheaper than true great-circle distance.
    center_lat, center_lon = box.center
    lon_scale = math.cos(math.radians(center_lat))
    distance = func.power(OsmPlace.latitude - center_lat, 2) + func.power(
        (OsmPlace.longitude - center_lon) * lon_scale, 2
    )

    # Places whose name says it first, then nearest.
    ordering = [distance, OsmPlace.id]
    if named is not None:
        ordering.insert(0, case((named, 0), else_=1))

    places = db.scalars(
        select(OsmPlace).where(where).order_by(*ordering).limit(limit)
    ).all()
    return total, to_suggestions(db, destination_id, places)
