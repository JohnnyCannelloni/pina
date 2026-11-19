"""The HTTP endpoints.

    GET  /api/trips                            the trip list
    GET  /api/geocode?q=                       find a city to add
    POST /api/trips/{id}/destinations          add a destination, start its sync
    GET  /api/destinations/{id}                destination + sync progress
    POST /api/destinations/{id}/sync           re-run / resume the sync
    GET  /api/destinations/{id}/places         your saved collection
    POST /api/destinations/{id}/places         save a place
    GET  /api/destinations/{id}/nearby         cached OSM places you could save

Note what `nearby` is now: a plain indexed SELECT against our own osm_places
table. It used to call Overpass on every request, which is why it took seconds
or returned a 502. Compare the two versions in git — moving a third-party
dependency off the request path is most of what this increment is.

This is the middle of the trace. Follow a save from here:

    AddPlaceForm.handleSubmit (React)
      -> POST /api/destinations/1/places
      -> create_place (below)
      -> PlaceCreate validates the body          (schemas.py)
      -> Place(...) builds the row               (models.py)
      -> db.add / db.commit issues the INSERT    (db.py holds the Session)
      -> PlaceRead serialises the saved row back (schemas.py)
      -> React prepends it to state
"""

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.db import get_db
from app.geocode import GeocodeError, NominatimClient, get_geocoder
from app.models import (
    Category,
    Destination,
    ExtractState,
    OsmExtract,
    OsmPlace,
    Place,
    SyncState,
    Trip,
)
from app.regions import UnsupportedRegion, resolve_region
from app.schemas import (
    BoundingBox,
    Cluster,
    Coverage,
    DestinationCreate,
    DestinationDetail,
    GeocodeCandidate,
    NearbyResponse,
    PlaceCreate,
    PlaceRead,
    Suggestion,
    TripRead,
)
from app.sync import clamp_area, get_sync_runner

router = APIRouter(prefix="/api")

# Module-level constant rather than a literal list in the signature below.
# A mutable default argument is evaluated once at import and shared by every
# call, so anything that mutated it would corrupt the default for the life of
# the process. A tuple cannot be mutated, which removes the hazard entirely.
ALL_CATEGORIES = (Category.RESTAURANT, Category.CAFE, Category.SIGHT)

#: Above this many places in the viewport, return clusters instead of pins.
#: A few hundred markers is already dense on screen; a few thousand is a smear
#: the browser struggles to render and the user cannot read.
CLUSTER_THRESHOLD = 400

#: Target grid cells per side when clustering. 8x8 = up to 64 bubbles, which
#: reads as a density map rather than either one blob or a fog of circles.
CLUSTER_GRID = 8

#: No cap is needed on the number of cells: the step is viewport-relative, so
#: the grid itself bounds the output at about (CLUSTER_GRID + 1)^2. See the note
#: in _clusters_in about the LIMIT that used to be here and why it was wrong.


@router.get("/trips", response_model=list[TripRead])
def list_trips(db: Session = Depends(get_db)) -> list[Trip]:
    """Every trip, with its destinations nested.

    The UI calls this on load to discover that Venice is destination 1 and where
    to centre the map, so no id or coordinate is hardcoded in the frontend.

    selectinload tells SQLAlchemy to fetch all the destinations in one extra
    query. Without it, serialising N trips would lazily fire one query per trip
    — the N+1 problem. With two rows seeded you would never notice; it is here
    because this is the shape of the bug, not because it hurts yet.
    """
    stmt = select(Trip).options(selectinload(Trip.destinations)).order_by(Trip.id)
    return list(db.scalars(stmt))


def _get_destination_or_404(db: Session, destination_id: int) -> Destination:
    """Look up a destination, or raise a clean 404.

    Both place endpoints need this. Without the explicit check, a POST to a
    destination that does not exist would reach Postgres and fail the foreign
    key, surfacing as a 500 and an ugly traceback. A 404 with a readable
    `detail` is the honest answer: the client asked about something absent.
    """
    destination = db.get(Destination, destination_id)
    if destination is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Destination {destination_id} not found",
        )
    return destination


@router.get("/destinations/{destination_id}/places", response_model=list[PlaceRead])
def list_places(
    destination_id: int, db: Session = Depends(get_db)
) -> list[Place]:
    """The destination's saved places — the one read both views share.

    The list renders all of these. The map renders the subset with coordinates.
    Because there is a single source, switching views cannot show stale data and
    a refresh cannot lose a pin.
    """
    _get_destination_or_404(db, destination_id)

    stmt = (
        select(Place)
        .where(Place.destination_id == destination_id)
        # Must-go first, then newest. desc() on a boolean puts true before
        # false in Postgres, which is what the flag is for.
        .order_by(Place.must_go.desc(), Place.created_at.desc(), Place.id.desc())
    )
    return list(db.scalars(stmt))


@router.post(
    "/destinations/{destination_id}/places",
    response_model=PlaceRead,
    status_code=status.HTTP_201_CREATED,
)
def create_place(
    destination_id: int,
    payload: PlaceCreate,
    db: Session = Depends(get_db),
) -> Place:
    """Save one place. This is the endpoint the Save button hits.

    By the time the body of this function runs, FastAPI has already:
      - parsed the JSON body into a PlaceCreate,
      - rejected a blank name, an unknown category, an out-of-range coordinate,
        or a half-dropped pin, with a 422 and a field-level message.

    So there is no validation code here. That is the point of the schema.
    """
    destination = _get_destination_or_404(db, destination_id)

    # Build the row. destination_id comes from the URL, never the body, so the
    # place cannot be filed under a different destination than the one the
    # client addressed.
    place = Place(
        destination_id=destination.id,
        name=payload.name,
        # payload.category is a Category (a StrEnum). str() stores the plain
        # "restaurant" that ck_places_category expects, rather than relying on
        # how the enum happens to serialise.
        category=str(payload.category),
        must_go=payload.must_go,
        latitude=payload.latitude,
        longitude=payload.longitude,
        source=str(payload.source),
        source_ref=payload.source_ref,
    )

    # Three steps, and they are genuinely different:
    #   add    — stage the object in the Session's unit of work. No SQL yet.
    #   commit — flush the INSERT and commit the transaction. The row is now
    #            durable; this is the moment the save actually happens.
    #   refresh— re-SELECT the row to pick up what the database generated:
    #            `id` from the sequence and `created_at` from DEFAULT now().
    #            Without it those attributes would be None in the response.
    db.add(place)
    try:
        db.commit()
    except IntegrityError as exc:
        # A constraint in the database refused the row. The one we expect here
        # is ix_places_destination_source_ref: this OSM place is already saved
        # to this destination.
        #
        # The rollback is not optional. After a failed commit the Session is in
        # a broken state and every later query on it would fail too — including
        # the ones serving the rest of this request.
        db.rollback()

        # 409 Conflict, not 400 or 500: the request was well-formed and the
        # server is healthy, but it conflicts with the current state of the
        # resource. That is precisely what 409 means, and the UI can tell the
        # user "you already saved this" rather than "something went wrong".
        #
        # Checking the constraint name rather than returning 409 for any
        # IntegrityError matters — a different constraint failing is a bug in
        # our code, and should surface as a 500 rather than be mislabelled as
        # the user's fault.
        if "ix_places_destination_source_ref" in str(exc.orig):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"'{payload.name}' is already saved to this destination.",
            ) from exc
        raise

    db.refresh(place)

    # Returning the saved row (rather than 201 with an empty body) lets React
    # drop the real record — with its real id — straight into state, so the new
    # place and its pin appear without a second round trip.
    return place


def get_bounding_box(
    south: Annotated[float, Query(description="Southern edge of the search box.")],
    west: Annotated[float, Query(description="Western edge of the search box.")],
    north: Annotated[float, Query(description="Northern edge of the search box.")],
    east: Annotated[float, Query(description="Eastern edge of the search box.")],
) -> BoundingBox:
    """Assemble the four edge parameters into a validated BoundingBox.

    Why this exists rather than annotating the endpoint with the model directly:

    - `Annotated[BoundingBox, Depends()]` calls the model as a plain
      constructor, so a ValidationError from its validators escapes as an
      unhandled exception and the client gets a 500 instead of a 422.
    - `Annotated[BoundingBox, Query()]` is FastAPI 0.115's query-parameter-model
      feature, which *does* produce a 422 — but on this version it stops
      flattening the model as soon as the same signature also declares another
      `Query()` parameter (our `categories`), and silently starts looking for a
      single query param literally named "bbox". Both of those were found by
      the tests in tests/test_nearby_api.py, which is the argument for writing
      the error-path tests at all.

    So: four explicit floats, assembled by hand, with the validation error
    translated deliberately. Re-raising as FastAPI's own
    RequestValidationError is the detail that matters — it means a bad bounding
    box produces exactly the same 422 body shape as every other validation
    failure in the API, rather than a bespoke error format for this one
    endpoint.
    """
    try:
        return BoundingBox(south=south, west=west, north=north, east=east)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


@router.get("/destinations/{destination_id}/nearby", response_model=NearbyResponse)
def list_nearby(
    destination_id: int,
    # Already validated by get_bounding_box above: corners ordered, span within
    # the limit. A bad box is a 422 that never reaches Overpass.
    bbox: Annotated[BoundingBox, Depends(get_bounding_box)],
    categories: Annotated[
        list[Category],
        # Repeated query params: ?categories=restaurant&categories=cafe.
        # FastAPI validates each against the Category enum, so an unknown
        # category is a 422 rather than a silently empty result.
        Query(description="Which kinds of place to look for."),
    ] = list(ALL_CATEGORIES),
    db: Session = Depends(get_db),
) -> NearbyResponse:
    """Cached OpenStreetMap places inside this rectangle.

    **This is a database read.** No outbound call, no `httpx`, no third party
    in the request path at all — which is the whole point of this increment.
    The previous version called Overpass here and took 3-30 seconds, or
    returned a 502 when the public instance was at capacity (which, measured,
    was most of the time). Now it is a few milliseconds and cannot fail, which
    is what lets the map load markers automatically as you pan.

    The places come from app/sync.py, which filled osm_places in the background.
    If a region has not finished importing this returns whatever has landed so
    far, with `coverage` saying so — partial data beats an error, and an
    explanation beats a blank map.

    **Two lists, one envelope.** A zoomed-out view of Manhattan can hold tens of
    thousands of places; drawing them is useless and shipping them is wasteful.
    So the server counts first and decides:

      - few enough to draw  -> `mode="places"`,   real pins
      - too many            -> `mode="clusters"`, counts per grid cell

    The decision is made from the **actual count**, not from a zoom level the
    client reports. Zoom alone would cluster an empty suburb and send thousands
    of pins for midtown — density varies far more than zoom does.
    """
    _get_destination_or_404(db, destination_id)

    category_values = [str(category) for category in categories]

    # The filter every query below shares. Built once as a list of conditions
    # rather than repeated, so the count and the fetch can never disagree about
    # what they are counting.
    in_viewport = (
        OsmPlace.latitude >= bbox.south,
        OsmPlace.latitude <= bbox.north,
        OsmPlace.longitude >= bbox.west,
        OsmPlace.longitude <= bbox.east,
        OsmPlace.category.in_(category_values),
    )

    # Served by ix_osm_places_coords. A count is cheap enough to always do, and
    # it is what makes the mode decision honest.
    total = db.scalar(
        select(func.count()).select_from(OsmPlace).where(*in_viewport)
    ) or 0

    coverage = _coverage_for(db, bbox)

    if total > CLUSTER_THRESHOLD:
        return NearbyResponse(
            mode="clusters",
            total=total,
            coverage=coverage,
            clusters=_clusters_in(db, bbox, in_viewport),
        )

    places = list(db.scalars(select(OsmPlace).where(*in_viewport)))

    # Which of these are already saved? One query for the whole set rather than
    # one per place — the per-item version would be the N+1 problem again.
    #
    # Note this is still scoped to the destination: the places are global, but
    # *your collection* is not. The same café is "already saved" in Venice and
    # not in Mestre, and that is correct.
    saved_refs = set(
        db.scalars(
            select(Place.source_ref).where(
                Place.destination_id == destination_id,
                Place.source_ref.is_not(None),
            )
        )
    )

    return NearbyResponse(
        mode="places",
        total=total,
        coverage=coverage,
        places=[
            Suggestion(
                source_ref=place.source_ref,
                name=place.name,
                category=Category(place.category),
                latitude=place.latitude,
                longitude=place.longitude,
                already_saved=place.source_ref in saved_refs,
            )
            for place in places
        ],
    )


def _clusters_in(
    db: Session, bbox: BoundingBox, in_viewport: tuple
) -> list[Cluster]:
    """Group the viewport into a grid and count each cell.

    The grid step is derived from the viewport, so you always get roughly
    CLUSTER_GRID x CLUSTER_GRID cells whatever the zoom — the bubbles stay the
    same visual size as you zoom out, rather than merging into one.

    `avg(latitude)` rather than the cell centre: a bubble then sits on the
    actual centre of mass of its places, so a cluster over a coastline appears
    over the town and not out at sea.

    Plain SQL, no PostGIS. Real spatial clustering (ST_ClusterKMeans) produces
    prettier groupings, but needs the extension and a much slower query for a
    result nobody would notice at this scale.
    """
    lat_step = max((bbox.north - bbox.south) / CLUSTER_GRID, 1e-6)
    lon_step = max((bbox.east - bbox.west) / CLUSTER_GRID, 1e-6)

    # floor(x / step) is the cell index. Grouping by the *expression* keeps this
    # to one pass with no temporary table.
    lat_cell = func.floor(OsmPlace.latitude / lat_step)
    lon_cell = func.floor(OsmPlace.longitude / lon_step)

    rows = db.execute(
        select(
            func.avg(OsmPlace.latitude),
            func.avg(OsmPlace.longitude),
            func.count().label("n"),
        )
        .where(*in_viewport)
        .group_by(lat_cell, lon_cell)
        # Biggest first, purely so the renderer draws the dense bubbles on top.
        .order_by(func.count().desc())
        #
        # Deliberately NO LIMIT. There was one, and it was a bug: the grid step
        # is derived from the viewport, so the cell count is already bounded at
        # roughly CLUSTER_GRID + 1 per side (the viewport edges can straddle
        # cells, so 8 becomes 9). A LIMIT on top of that silently dropped cells
        # while `total` still counted their places — so the bubbles no longer
        # added up to the total.
        #
        # Caught by the `sum(counts) == total` assertion in
        # tests/test_nearby_api.py, which is exactly why that assertion is
        # worth writing: the truncated response still looked perfectly
        # plausible.
    ).all()

    return [
        Cluster(latitude=float(lat), longitude=float(lon), count=int(count))
        for lat, lon, count in rows
    ]


def _coverage_for(db: Session, bbox: BoundingBox) -> Coverage:
    """Do we have imported data for this viewport?

    Compared against the bounding boxes of ready extracts. The point is to
    distinguish an empty area from an unimported one — an empty map with no
    explanation is what made two earlier versions of this feature feel broken.

    Deliberately approximate: a single extract containing the whole viewport is
    COVERED, any overlap is PARTIAL, no overlap is MISSING. A viewport spanning
    two adjacent imported regions reports PARTIAL even though it is really
    fully covered. Resolving that needs genuine geometry union, and being
    slightly pessimistic is the right direction for this to err in.
    """
    extracts = list(
        db.execute(
            select(
                OsmExtract.south,
                OsmExtract.west,
                OsmExtract.north,
                OsmExtract.east,
            ).where(
                OsmExtract.state == ExtractState.READY,
                OsmExtract.south.is_not(None),
            )
        ).all()
    )

    overlaps = False
    for south, west, north, east in extracts:
        if (
            south <= bbox.south
            and north >= bbox.north
            and west <= bbox.west
            and east >= bbox.east
        ):
            return Coverage.COVERED
        if (
            south <= bbox.north
            and north >= bbox.south
            and west <= bbox.east
            and east >= bbox.west
        ):
            overlaps = True

    return Coverage.PARTIAL if overlaps else Coverage.MISSING


# --- destinations ----------------------------------------------------------


@router.get("/geocode", response_model=list[GeocodeCandidate])
def geocode(
    q: Annotated[str, Query(min_length=1, description="A place name to look up.")],
    geocoder: NominatimClient = Depends(get_geocoder),
) -> list[GeocodeCandidate]:
    """Find candidate destinations for a typed name.

    Returns several, deliberately — "Venice" is a real place in Italy, Florida
    and California, so the user picks rather than us guessing.

    This *does* call a third party, and that is fine here: it happens once when
    someone types a city, not on every map movement. See the note at the top of
    app/geocode.py on why Nominatim is the right service for this and the wrong
    one for listing restaurants.
    """
    try:
        results = geocoder.search(q)
    except GeocodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    return [GeocodeCandidate.model_validate(result) for result in results]


@router.post(
    "/trips/{trip_id}/destinations",
    response_model=DestinationDetail,
    status_code=status.HTTP_201_CREATED,
)
def create_destination(
    trip_id: int,
    payload: DestinationCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    start_sync: Callable[[int], None] = Depends(get_sync_runner),
) -> DestinationDetail:
    """Add a destination to a trip and start fetching its places.

    Returns immediately with sync_state='pending'. The actual fetching happens
    after the response is sent, so adding Lisbon is instant even though
    populating its map takes a while.
    """
    trip = db.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Trip {trip_id} not found",
        )

    # Shrink an oversized geocoder box to something syncable. Nominatim's
    # "Tokyo" spans ~80km; see MAX_DESTINATION_SPAN_DEGREES in sync.py.
    south, west, north, east = clamp_area(
        payload.latitude,
        payload.longitude,
        payload.south,
        payload.west,
        payload.north,
        payload.east,
    )

    # Resolve the region up front, so an unsupported location is reported
    # immediately on the created destination rather than discovered minutes
    # later when the background job gives up.
    #
    # Resolved from the coordinates, not the country code: that is what picks
    # us/new-york (476 MB) over us (11,706 MB). See app/regions.py.
    region: str | None = None
    unsupported: str | None = None
    try:
        region = resolve_region(payload.latitude, payload.longitude).id
    except UnsupportedRegion as exc:
        unsupported = str(exc)

    destination = Destination(
        trip_id=trip.id,
        name=payload.name,
        latitude=payload.latitude,
        longitude=payload.longitude,
        south=south,
        west=west,
        north=north,
        east=east,
        osm_ref=payload.osm_ref,
        country_code=payload.country_code,
        region=region,
        # A destination we cannot auto-fill is created anyway — you can still
        # save places by hand — but says so instead of sitting at 'pending'
        # forever with no explanation.
        sync_state=SyncState.PENDING if region else SyncState.FAILED,
        sync_error=unsupported,
    )
    db.add(destination)

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if "ix_destinations_trip_osm_ref" in str(exc.orig):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"{payload.name} is already a destination on this trip.",
            ) from exc
        raise

    db.refresh(destination)

    # The background work: download the region's extract if we do not have it,
    # then parse it. FastAPI runs this after the response is sent — which is
    # exactly why sync_destination opens its own Session rather than taking
    # `db`: by then get_db's `finally` has closed this one.
    #
    # Skipped when there is no region (nothing to fetch), and when auto-import
    # is off (a client must not be able to start a 476 MB download by typing a
    # city name — see AUTO_IMPORT_REGIONS in config.py).
    if region and settings.AUTO_IMPORT_REGIONS:
        background_tasks.add_task(start_sync, destination.id)
    elif region:
        destination.sync_error = (
            f"Map data for {region} has not been loaded on this server yet."
        )
        db.commit()
        db.refresh(destination)

    return _destination_detail(db, destination)


@router.get("/destinations/{destination_id}", response_model=DestinationDetail)
def get_destination(
    destination_id: int, db: Session = Depends(get_db)
) -> DestinationDetail:
    """A destination with its sync progress. The UI polls this while syncing."""
    destination = _get_destination_or_404(db, destination_id)
    return _destination_detail(db, destination)


@router.post("/destinations/{destination_id}/sync", response_model=DestinationDetail)
def resync_destination(
    destination_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    start_sync: Callable[[int], None] = Depends(get_sync_runner),
) -> DestinationDetail:
    """Re-run the import, resuming whatever is incomplete.

    Safe to call at any time. An already-downloaded extract is reused, a
    partial download resumes from where it stopped, and the place upsert means
    re-imported places are updated rather than duplicated.

    Your saved places are never touched — only the osm_places cache.
    """
    destination = _get_destination_or_404(db, destination_id)

    destination.sync_state = SyncState.PENDING
    destination.sync_error = None
    db.commit()
    db.refresh(destination)

    if settings.AUTO_IMPORT_REGIONS:
        background_tasks.add_task(start_sync, destination_id)
    return _destination_detail(db, destination)


def _destination_detail(
    db: Session, destination: Destination
) -> DestinationDetail:
    """Attach cache status to a destination.

    Counted with SQL rather than len(destination.osm_places): loading several
    thousand cached rows into Python just to count them would make polling this
    endpoint more expensive than the import it is reporting on.
    """
    # Geographic, not a foreign key: osm_places is global since 0005, so "how
    # many places does this destination have" means "how many are inside its
    # area".
    osm_place_count = db.scalar(
        select(func.count())
        .select_from(OsmPlace)
        .where(
            OsmPlace.latitude >= destination.south,
            OsmPlace.latitude <= destination.north,
            OsmPlace.longitude >= destination.west,
            OsmPlace.longitude <= destination.east,
        )
    ) or 0

    # The extract's own state is what the UI narrates during a sync —
    # "downloading 250 MB" and "importing" are very different waits, and the
    # destination's own sync_state cannot distinguish them.
    extract_state: str | None = None
    if destination.region:
        extract_state = db.scalar(
            select(OsmExtract.state).where(
                OsmExtract.region == destination.region
            )
        )

    return DestinationDetail(
        id=destination.id,
        name=destination.name,
        latitude=destination.latitude,
        longitude=destination.longitude,
        south=destination.south,
        west=destination.west,
        north=destination.north,
        east=destination.east,
        sync_state=SyncState(destination.sync_state),
        sync_error=destination.sync_error,
        synced_at=destination.synced_at,
        osm_place_count=osm_place_count,
        region=destination.region,
        extract_state=ExtractState(extract_state) if extract_state else None,
    )
