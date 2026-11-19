"""Pydantic schemas — the shape of the JSON going in and coming out.

Why these exist when `models.py` already describes a place: the ORM model is the
*storage* shape and the schema is the *wire* shape, and they differ. A client
may not set `id` or `created_at`; the database must never be handed a `category`
it has not agreed to. Separating them means a request body can be validated and
rejected with a 422 before any SQL runs.

Two separate classes per direction is the convention worth internalising:
  PlaceCreate — what a client may send   (no id, no created_at)
  PlaceRead   — what the API sends back  (everything)
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.models import Category, ExtractState, PlaceSource, SyncState

# Reused by PlaceCreate. strip_whitespace runs *before* min_length, so a name of
# "   " becomes "" and is then rejected — which is the behaviour you want, and
# is easy to get wrong by validating length first.
PlaceName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]

# ge/le = "greater or equal"/"less or equal". These mirror the ck_places_*_range
# constraints in the migration. The schema check exists to produce a readable
# 422; the database check exists to actually be true.
Latitude = Annotated[float, Field(ge=-90, le=90)]
Longitude = Annotated[float, Field(ge=-180, le=180)]


class PlaceCreate(BaseModel):
    """The body of POST /api/destinations/{id}/places.

    Note what is absent: `id`, `created_at`, and `destination_id`. The first two
    belong to the database, and the third comes from the URL path — accepting it
    in the body as well would let a client send a path and body that disagree.
    """

    name: PlaceName
    category: Category
    must_go: bool = False

    # Optional: a place with no pin is normal. Default None rather than required
    # means an older client that knows nothing about the map still works.
    latitude: Latitude | None = None
    longitude: Longitude | None = None

    # Defaults to MANUAL, so the save form does not have to send either field
    # and the original POST body from the first increment still works unchanged.
    # The map's popup save sends source="osm" plus the element id.
    source: PlaceSource = PlaceSource.MANUAL
    source_ref: str | None = None

    @model_validator(mode="after")
    def source_ref_matches_source(self) -> Self:
        """An OSM place must carry its reference; a manual one must not.

        Mirrors ck_places_source_ref_present in migration 0002 — the same
        two-layer arrangement as the coordinate rule below. This copy produces a
        readable 422; the constraint is the one that cannot be bypassed.
        """
        if (self.source is PlaceSource.OSM) != (self.source_ref is not None):
            raise ValueError(
                "source_ref is required when source is 'osm', "
                "and must be omitted otherwise"
            )
        return self

    @model_validator(mode="after")
    def coordinates_are_both_or_neither(self) -> Self:
        """Reject a half-dropped pin.

        A field_validator cannot do this — it only sees one field. A
        model_validator with mode="after" runs once both fields are parsed, so
        it can compare them.

        The same rule is enforced by ck_places_coords_both_or_neither in the
        database. That is deliberate duplication, not an oversight: this copy
        exists to return a 422 that says what is wrong, while the database copy
        is the one that cannot be bypassed by a future endpoint or a stray psql
        INSERT. Validation is for error messages; constraints are for truth.
        """
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError(
                "latitude and longitude must be provided together, or both omitted"
            )
        return self


class PlaceRead(BaseModel):
    """A saved place as the API returns it."""

    # from_attributes lets Pydantic read a SQLAlchemy object's attributes
    # directly (place.name), instead of requiring a dict. Without it,
    # PlaceRead.model_validate(place) raises.
    model_config = ConfigDict(from_attributes=True)

    id: int
    destination_id: int
    name: str
    category: Category
    must_go: bool
    latitude: float | None
    longitude: float | None
    source: PlaceSource
    source_ref: str | None
    created_at: datetime


class DestinationRead(BaseModel):
    """A destination: where it is, how big it is, and whether it is synced."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    latitude: float
    longitude: float
    # The stored area. The map opens on these bounds rather than on a fixed
    # zoom around the centre, so a small town and a big city both frame well.
    south: float
    west: float
    north: float
    east: float
    sync_state: SyncState
    sync_error: str | None
    synced_at: datetime | None


class DestinationDetail(DestinationRead):
    """DestinationRead plus cache status — what the UI polls while filling.

    Inherits rather than repeating the fields, so adding a field to
    DestinationRead cannot leave the two out of step.
    """

    #: How many OSM places are cached. Jumps from 0 to a few thousand when an
    #: import finishes, so it is the number worth showing.
    osm_place_count: int
    #: The Geofabrik region, e.g. "europe/portugal". None when we have no
    #: extract for this country — the destination still works for manual saves.
    region: str | None
    #: The extract's own state. Lets the UI distinguish a multi-minute download
    #: from a seconds-long parse, which the destination's sync_state cannot.
    extract_state: ExtractState | None


class DestinationCreate(BaseModel):
    """Create a destination from a geocoder candidate.

    The client sends back a result it got from /api/geocode. The alternative —
    sending just the name and geocoding again server-side — would re-query
    Nominatim and could silently resolve to a *different* candidate than the one
    the user picked.
    """

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    latitude: Latitude
    longitude: Longitude
    south: Latitude
    west: Longitude
    north: Latitude
    east: Longitude
    osm_ref: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    #: ISO 3166-1 alpha-2. Decides which Geofabrik extract gets downloaded.
    #: Optional: a destination whose country we cannot determine is still
    #: usable, it just cannot auto-fill its places.
    country_code: str | None = None

    @model_validator(mode="after")
    def corners_are_ordered(self) -> Self:
        """Mirrors ck_destinations_bbox_ordered.

        Worth having at this layer too: the commonest way to get here with a
        bad box is mixing up a geocoder's axis order, and a 422 naming the
        problem beats a constraint violation. See the note in geocode.py about
        Nominatim returning [min_lat, max_lat, min_lon, max_lon].
        """
        if self.north <= self.south:
            raise ValueError("north must be greater than south")
        if self.east <= self.west:
            raise ValueError("east must be greater than west")
        return self


class GeocodeCandidate(BaseModel):
    """One possible destination for a typed name.

    A list of these is returned rather than a single best match, because
    "Venice" is a real place in Italy, Florida, and California — and silently
    picking one is how a trip ends up on the wrong continent.
    """

    model_config = ConfigDict(from_attributes=True)

    display_name: str
    name: str
    latitude: float
    longitude: float
    south: float
    west: float
    north: float
    east: float
    osm_ref: str
    country_code: str | None


class TripRead(BaseModel):
    """A trip with its destinations nested.

    Nesting means the UI finds Venice's id and map centre in one request, so no
    destination id is ever hardcoded in the React code.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    destinations: list[DestinationRead]


# --- nearby-place discovery -------------------------------------------------

#: Maximum width or height of a bounding box, in degrees.
#:
#: This used to be 0.08 (~9 km), because /nearby forwarded the box straight to
#: Overpass and a large request was an abuse of someone else's donated compute.
#: Now that /nearby reads our own indexed table, that reasoning is gone — and
#: the old value was actively wrong: a map zoomed out to show a whole city spans
#: well over 0.08 degrees, so legitimate views were being rejected with a 422.
#:
#: What is left is a sanity check against a nonsense request, not a cost
#: control. Response size is bounded by MAX_SUGGESTIONS in routes.py, and the
#: query is scoped to one destination and served by an index, so a wide box is
#: cheap. 5 degrees is far more than any city view and still catches a client
#: asking for a hemisphere.
MAX_BBOX_SPAN_DEGREES = 5.0


class BoundingBox(BaseModel):
    """The visible map rectangle to search inside.

    This is a FastAPI dependency rather than four loose query parameters, which
    puts the validation here instead of in the endpoint body. routes.py then
    gets a value that is already known to be sane.
    """

    south: Latitude
    west: Longitude
    north: Latitude
    east: Longitude

    @model_validator(mode="after")
    def corners_are_ordered(self) -> Self:
        if self.north <= self.south:
            raise ValueError("north must be greater than south")
        if self.east <= self.west:
            raise ValueError("east must be greater than west")
        return self

    @model_validator(mode="after")
    def span_is_reasonable(self) -> Self:
        """Refuse an absurdly large box.

        A sanity check, not a cost control — see MAX_BBOX_SPAN_DEGREES. The
        earlier, much tighter version of this limit was written when the box was
        forwarded to Overpass, and it silently broke zoomed-out map views once
        the data moved into our own database. A limit that outlives its reason
        becomes a bug.
        """
        if (
            self.north - self.south > MAX_BBOX_SPAN_DEGREES
            or self.east - self.west > MAX_BBOX_SPAN_DEGREES
        ):
            raise ValueError(
                f"search area is too large; each side must span at most "
                f"{MAX_BBOX_SPAN_DEGREES} degrees. Zoom in and try again."
            )
        return self

    @property
    def center(self) -> tuple[float, float]:
        """(latitude, longitude) of the middle — used to rank results."""
        return ((self.south + self.north) / 2, (self.west + self.east) / 2)


class Suggestion(BaseModel):
    """A place from OpenStreetMap that you could save — a candidate.

    Note the absence of an `id`: the map never needs our primary key.
    `source_ref` is the identity, and it becomes Place.source_ref if you save
    one.
    """

    source_ref: str
    name: str
    category: Category
    latitude: float
    longitude: float
    #: True when a place with this source_ref is already in this destination.
    #: The map hides those, so you are never offered something you have saved.
    already_saved: bool


class Cluster(BaseModel):
    """A count of places in one grid cell, for a zoomed-out view.

    Returned instead of individual places when the viewport holds too many to
    draw usefully. The coordinates are the *average* position of the places in
    the cell, not the cell's centre, so the bubble sits where the places
    actually are rather than on an arbitrary grid line.
    """

    latitude: float
    longitude: float
    count: int


class Coverage(StrEnum):
    """Whether we have data for the requested viewport at all.

    This exists because an empty list is ambiguous, and that ambiguity is what
    made two earlier versions of this feature feel broken: a map with no markers
    looked identical whether the area was genuinely empty or had never been
    imported. Saying which is the difference between "nothing here" and
    "nothing *yet*".
    """

    #: An imported region covers the whole viewport.
    COVERED = "covered"
    #: Part of the viewport is covered; panning further may show nothing.
    PARTIAL = "partial"
    #: No imported region overlaps the viewport at all.
    MISSING = "missing"


class NearbyResponse(BaseModel):
    """What the map gets back for one viewport.

    One envelope rather than separate /places and /clusters endpoints: both
    need the same bounding-box parsing, the same category filter, and the same
    `already_saved` join, and the client wants one round trip per pan.

    `mode` tells the client which of the two lists to read. The other is empty.
    """

    #: "places" or "clusters" — decided by the server from the actual count,
    #: not from a zoom level the client claims. See routes.list_nearby.
    mode: Literal["places", "clusters"]
    #: Total matching places in the viewport, regardless of mode. In cluster
    #: mode the cluster counts sum to this — which is the assertion that catches
    #: a mis-written GROUP BY.
    total: int
    coverage: Coverage
    places: list[Suggestion] = []
    clusters: list[Cluster] = []
