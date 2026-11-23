"""ask pina's search: a SearchIntent in, real places out. no llm here.

    inside the search box
    AND ((category AND cuisine) OR name contains a hint word)
    AND every requested feature
    ORDER BY name match first, then distance from the box centre

only about a third of restaurants have a cuisine tag, so name words
("trattoria") count as evidence too. without a cuisine they only rank.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from collections.abc import Iterable, Mapping

from sqlalchemy import and_, case, func, or_, select, text
from sqlalchemy.orm import Session

from app.geocode import GeocodeError, GeocodeResult, NominatimClient
from app.models import Category, Destination, OsmPlace, Place
from app.schemas import BoundingBox, Feature, PlaceDetails, SearchIntent, Suggestion

#: results per answer.
RESULT_LIMIT = 25

#: cuisines offered to the model. the long tail only slows it down.
MAX_CUISINES = 80

#: minimum half-side of a named area's box (~650 m), so a landmark includes
#: the streets around it...
MIN_AREA_HALF_SPAN = 0.006
#: ...and the maximum (~3.3 km), so a big district isn't the whole city.
MAX_AREA_HALF_SPAN = 0.03

#: an "area" at least this fraction of the destination on both axes is the
#: city itself under another name (lisbon / lisboa).
WHOLE_CITY_FRACTION = 0.5

#: geocoder candidates to pick an area from.
AREA_CANDIDATES = 10

#: how far past the destination's box an area may be, in degrees.
AREA_SEARCH_MARGIN = 0.05

#: osm tag values that count as having a feature. each alternative is a
#: fragment for `tags @> ...`. "only" counts; "limited" and "no" don't.
FEATURE_TAGS: dict[Feature, list[dict[str, str]]] = {
    Feature.OUTDOOR_SEATING: [{"outdoor_seating": "yes"}],
    Feature.VEGETARIAN: [
        {"diet:vegetarian": "yes"},
        {"diet:vegetarian": "only"},
        # vegan implies vegetarian; osm rarely tags both.
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
    """python version of the sql filter, used to label results."""
    return any(
        all(tags.get(key) == value for key, value in alternative.items())
        for alternative in FEATURE_TAGS[feature]
    )


def details_from_tags(tags: Mapping[str, object] | None) -> PlaceDetails | None:
    """osm_places.tags -> PlaceDetails. None if there are no tags."""
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
    """details for saved places by source_ref, in one query."""
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
    """cached places -> Suggestions, marking ones already saved here.

    shared by /nearby and /ask so a place looks the same either way.
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
    """cuisine values present in a box, commonest first. raw sql for the
    set-returning jsonb function."""
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
    #: e.g. "near Alfama", "in the map view", "in Lisboa".
    label: str
    #: false when a named area couldn't be found.
    found: bool = True


def _box_around(result: GeocodeResult) -> BoundingBox:
    """a search box around a geocoded area, within the min/max size.

    longitude is scaled by 1/cos(latitude) so the box is roughly square on
    the ground.
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
    """decide where to search: named area, then map view, then destination.

    if the area can't be found the search still runs wider, with found=False.
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
        # more candidates than usual; we pick by importance below.
        results = geocoder.search(
            intent.area, viewbox=viewbox, limit=AREA_CANDIDATES
        )
    except GeocodeError:
        # an unreachable geocoder costs precision, not the answer.
        results = []

    if not results:
        return SearchArea(fallback.box, fallback.label, found=False)

    found = max(results, key=lambda result: result.importance)
    # the "area" is the city itself (lisbon vs lisboa), so search all of it.
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
    """run the intent in the box. returns (total matches, closest results)."""
    conditions = [
        OsmPlace.latitude >= box.south,
        OsmPlace.latitude <= box.north,
        OsmPlace.longitude >= box.west,
        OsmPlace.longitude <= box.east,
    ]

    # autoescape: % and _ are literal.
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
        # name words are alternative evidence for the cuisine, and a name
        # match also overrides the category guess ("pastéis de nata" came back
        # as restaurant, hiding the café named after them).
        cuisine = or_(
            *(OsmPlace.tags.contains({"cuisine": [c]}) for c in intent.cuisines)
        )
        typed = and_(category, cuisine) if category is not None else cuisine
        conditions.append(or_(typed, named) if named is not None else typed)
    elif category is not None:
        # without a cuisine, name words only rank. as a filter, "vegano" hid
        # every vegan café not named that.
        conditions.append(category)

    # every requested feature must be present.
    for feature in intent.features:
        conditions.append(
            or_(*(OsmPlace.tags.contains(alt) for alt in FEATURE_TAGS[feature]))
        )

    where = and_(*conditions)
    total = db.scalar(select(func.count()).select_from(OsmPlace).where(where)) or 0

    # squared flat-map distance. fine for ranking within a city.
    center_lat, center_lon = box.center
    lon_scale = math.cos(math.radians(center_lat))
    distance = func.power(OsmPlace.latitude - center_lat, 2) + func.power(
        (OsmPlace.longitude - center_lon) * lon_scale, 2
    )

    # name matches first, then nearest.
    ordering = [distance, OsmPlace.id]
    if named is not None:
        ordering.insert(0, case((named, 0), else_=1))

    places = db.scalars(
        select(OsmPlace).where(where).order_by(*ordering).limit(limit)
    ).all()
    return total, to_suggestions(db, destination_id, places)


# --- the reply ---------------------------------------------------------------

#: how each feature reads after "with".
FEATURE_PHRASES: dict[Feature, str] = {
    Feature.OUTDOOR_SEATING: "outdoor seating",
    Feature.VEGETARIAN: "vegetarian options",
    Feature.VEGAN: "vegan options",
    Feature.GLUTEN_FREE: "gluten-free options",
    Feature.WHEELCHAIR: "wheelchair access",
    Feature.TAKEAWAY: "takeaway",
    Feature.DELIVERY: "delivery",
}

CATEGORY_NOUNS: dict[Category | None, tuple[str, str]] = {
    Category.RESTAURANT: ("restaurant", "restaurants"),
    Category.CAFE: ("café", "cafés"),
    Category.SIGHT: ("sight", "sights"),
    None: ("place", "places"),
}


def _and(items: list[str]) -> str:
    """["a"] -> "a", ["a", "b"] -> "a and b", ["a", "b", "c"] -> "a, b and c"."""
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


#: cuisines named after a people or place, which english capitalises.
_PROPER_CUISINE = re.compile(r"(an|ese|ish|ch|ern)$")
_PROPER_CUISINE_EXTRA = frozenset(
    {"thai", "greek", "israeli", "nepali", "pakistani", "basque", "swiss",
     "bangladeshi", "iraqi", "kurdish", "tex-mex"}
)


def _cuisine_word(cuisine: str) -> str:
    """ "italian" -> "Italian", "middle_eastern" -> "Middle eastern",
    "seafood" -> "seafood". "fish" isn't a nationality."""
    words = cuisine.replace("_", " ")
    if cuisine != "fish" and (
        _PROPER_CUISINE.search(cuisine) or cuisine in _PROPER_CUISINE_EXTRA
    ):
        return words[:1].upper() + words[1:]
    return words


def _thing(intent: SearchIntent, count: int, features: list[Feature]) -> str:
    """ "Italian restaurants with outdoor seating", "sushi cafés", ... """
    singular, plural = CATEGORY_NOUNS[intent.category]
    noun = singular if count == 1 else plural
    cuisines = [_cuisine_word(c) for c in intent.cuisines]
    if cuisines:
        noun = f"{_and(cuisines)} {noun}"
    if features:
        noun += " with " + _and([FEATURE_PHRASES[f] for f in features])
    return noun


def describe(
    intent: SearchIntent,
    area: SearchArea,
    total: int,
    dropped: list[Feature],
) -> str:
    """pina's reply, written from the results rather than by the model.

    a template so it's instant and can only describe what was found. a hosted
    model could replace this later.
    """
    sentences: list[str] = []

    if not area.found and intent.area:
        sentences.append(
            f"I couldn't find “{intent.area}” on the map, so I searched "
            f"{area.label.removeprefix('in ')} instead."
        )

    # describe the results without the dropped features...
    kept = [f for f in intent.features if f not in dropped]

    if total == 0:
        sentences.append(
            f"I couldn't find any {_thing(intent, 2, kept)} {area.label}. "
            "Try asking for less, or somewhere else."
        )
        return " ".join(sentences)

    count = f"{total:,}"
    sentences.append(f"{count} {_thing(intent, total, kept)} {area.label}.")

    # ...and say they were dropped.
    if dropped:
        missing = _and([FEATURE_PHRASES[f] for f in dropped])
        sentences.append(
            f"None of them are recorded with {missing} — OpenStreetMap only "
            "notes that for some places, so check the Photos."
        )

    return " ".join(sentences)
