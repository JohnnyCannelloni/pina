"""pydantic schemas for request and response bodies.

PlaceCreate is what a client may send; PlaceRead is what the api returns.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.models import Category, PlaceSource, SyncState

# strip runs before min_length, so a name of spaces is rejected.
PlaceName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]

# mirror the ck_places_*_range constraints, to return a readable 422.
Latitude = Annotated[float, Field(ge=-90, le=90)]
Longitude = Annotated[float, Field(ge=-180, le=180)]


class PlaceCreate(BaseModel):
    """body of POST /api/destinations/{id}/places. destination_id comes from the url."""

    name: PlaceName
    category: Category
    must_go: bool = False

    # a place without a pin is fine.
    latitude: Latitude | None = None
    longitude: Longitude | None = None

    # the manual form omits these; map saves send source="osm" plus the osm id.
    source: PlaceSource = PlaceSource.MANUAL
    source_ref: str | None = None

    @model_validator(mode="after")
    def source_ref_matches_source(self) -> Self:
        """osm places need a source_ref, manual ones must not. mirrors a db constraint."""
        if (self.source is PlaceSource.OSM) != (self.source_ref is not None):
            raise ValueError(
                "source_ref is required when source is 'osm', "
                "and must be omitted otherwise"
            )
        return self

    @model_validator(mode="after")
    def coordinates_are_both_or_neither(self) -> Self:
        """reject a half-dropped pin. mirrors ck_places_coords_both_or_neither."""
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError(
                "latitude and longitude must be provided together, or both omitted"
            )
        return self


class Feature(StrEnum):
    """features you can ask for. a fixed enum so the llm can't invent one.

    the osm tag mapping is FEATURE_TAGS in app/ask.py.
    """

    OUTDOOR_SEATING = "outdoor_seating"
    VEGETARIAN = "vegetarian"
    VEGAN = "vegan"
    GLUTEN_FREE = "gluten_free"
    WHEELCHAIR = "wheelchair"
    TAKEAWAY = "takeaway"
    DELIVERY = "delivery"


class PlaceDetails(BaseModel):
    """what osm says about a place beyond its name and pin. all optional."""

    #: osm spellings, e.g. ["italian", "pizza"].
    cuisine: list[str] = []
    opening_hours: str | None = None
    website: str | None = None
    phone: str | None = None
    street: str | None = None
    #: askable features this place has.
    features: list[Feature] = []


class PlaceRead(BaseModel):
    """a saved place as the api returns it."""

    # read attributes straight off orm objects.
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
    #: from the osm cache, looked up by source_ref. None for manual places.
    details: PlaceDetails | None = None


class DestinationRead(BaseModel):
    """a destination, its area and its sync state."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    latitude: float
    longitude: float
    # the map opens on these bounds.
    south: float
    west: float
    north: float
    east: float
    sync_state: SyncState
    sync_error: str | None
    synced_at: datetime | None


class DestinationDetail(DestinationRead):
    """DestinationRead plus cache status, polled by the ui while syncing."""

    #: cached osm places inside the destination's area.
    osm_place_count: int


class DestinationCreate(BaseModel):
    """create a destination from a candidate returned by /api/geocode.

    sending the candidate back (rather than a name to geocode again) keeps the
    one the user actually picked.
    """

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    latitude: Latitude
    longitude: Longitude
    south: Latitude
    west: Longitude
    north: Latitude
    east: Longitude
    osm_ref: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    # other candidate fields the client sends back (e.g. country_code) are ignored.

    @model_validator(mode="after")
    def corners_are_ordered(self) -> Self:
        """mirrors ck_destinations_bbox_ordered."""
        if self.north <= self.south:
            raise ValueError("north must be greater than south")
        if self.east <= self.west:
            raise ValueError("east must be greater than west")
        return self


class GeocodeCandidate(BaseModel):
    """one possible destination for a typed name."""

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
    """a trip with its destinations nested."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    destinations: list[DestinationRead]


# --- nearby places -----------------------------------------------------------

#: sanity limit on a bounding box side, in degrees. results are capped
#: elsewhere, so this only rejects nonsense requests.
MAX_BBOX_SPAN_DEGREES = 5.0


class BoundingBox(BaseModel):
    """the visible map rectangle to search inside."""

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
        """refuse an absurdly large box. see MAX_BBOX_SPAN_DEGREES."""
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
        """(latitude, longitude) of the middle, used to rank results."""
        return ((self.south + self.north) / 2, (self.west + self.east) / 2)


class Suggestion(BaseModel):
    """an osm place you could save. source_ref is its identity; there's no id."""

    source_ref: str
    name: str
    category: Category
    latitude: float
    longitude: float
    #: already saved to this destination.
    already_saved: bool
    details: PlaceDetails | None = None


class Cluster(BaseModel):
    """a count of places in one grid cell, positioned at their average location."""

    latitude: float
    longitude: float
    count: int


class Coverage(StrEnum):
    """whether synced destinations cover the viewport, so an empty map can say why."""

    #: one synced destination's area covers the whole viewport.
    COVERED = "covered"
    #: part of the viewport is covered.
    PARTIAL = "partial"
    #: no synced destination overlaps it.
    MISSING = "missing"


class NearbyResponse(BaseModel):
    """what the map gets for one viewport. `mode` says which list is filled."""

    #: decided from the actual count, not the zoom level.
    mode: Literal["places", "clusters"]
    #: matches in the viewport. in cluster mode the counts sum to this.
    total: int
    coverage: Coverage
    places: list[Suggestion] = []
    clusters: list[Cluster] = []


# --- ask pina ----------------------------------------------------------------


class SearchIntent(BaseModel):
    """what the llm understood a question to mean, as a structured search.

    the only thing the llm produces. it never names places; app/ask.py runs
    this against osm_places.
    """

    # length limits: clients send this back as AskRequest.previous, and it
    # goes into the prompt.
    category: Category | None = None
    #: osm cuisine values that exist in the destination, e.g. "italian".
    cuisines: list[Annotated[str, StringConstraints(max_length=40)]] = Field(
        default=[], max_length=20
    )
    features: list[Feature] = Field(default=[], max_length=len(Feature))
    #: words to look for in names, for places without a cuisine tag.
    name_words: list[Annotated[str, StringConstraints(max_length=40)]] = Field(
        default=[], max_length=10
    )
    #: a neighbourhood or landmark, or None for the whole city / map view.
    area: Annotated[str, StringConstraints(max_length=120)] | None = None


class AskRequest(BaseModel):
    question: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)
    ]
    #: the map's current view, used when the question names no area.
    viewport: BoundingBox | None = None
    #: the previous search, so a follow-up refines it.
    previous: SearchIntent | None = None


class AskResponse(BaseModel):
    #: the reply sentence, built from the results by ask.describe.
    reply: str = ""
    interpretation: SearchIntent
    #: e.g. "near Alfama", "in the map view".
    area_label: str
    #: false when a named area couldn't be found and the search widened.
    area_found: bool = True
    #: matches before the limit.
    total: int
    results: list[Suggestion]
    #: requested features that nothing matched, so the search ran without them.
    dropped_features: list[Feature] = []
