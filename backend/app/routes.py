"""http endpoints.

    GET    /api/trips                            trips with their destinations
    GET    /api/geocode?q=                       find a city to add
    POST   /api/trips/{id}/destinations          add a destination, start its sync
    GET    /api/destinations/{id}                destination + sync progress
    DELETE /api/destinations/{id}                remove it and its saved places
    POST   /api/destinations/{id}/sync           re-run / resume the sync
    GET    /api/destinations/{id}/places         saved places
    POST   /api/destinations/{id}/places         save a place
    DELETE /api/destinations/{id}/places/{pid}   remove a saved place
    GET    /api/destinations/{id}/nearby         cached osm places in a viewport
    POST   /api/destinations/{id}/ask            ask pina: a question -> places
"""

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app import ask
from app.config import settings
from app.db import get_db
from app.geocode import GeocodeError, NominatimClient, get_geocoder
from app.llm import Llm, LlmError, LlmUnavailable, get_llm
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
    AskRequest,
    AskResponse,
    BoundingBox,
    Cluster,
    Coverage,
    DestinationCreate,
    DestinationDetail,
    Feature,
    GeocodeCandidate,
    NearbyResponse,
    PlaceCreate,
    PlaceRead,
    TripRead,
)
from app.sync import cancel_download, clamp_area, get_sync_runner

router = APIRouter(prefix="/api")

# a tuple, so the default below can't be mutated.
ALL_CATEGORIES = (Category.RESTAURANT, Category.CAFE, Category.SIGHT)

#: above this many places in the viewport, return clusters instead of pins.
CLUSTER_THRESHOLD = 400

#: target grid cells per side when clustering.
CLUSTER_GRID = 8



@router.get("/trips", response_model=list[TripRead])
def list_trips(db: Session = Depends(get_db)) -> list[Trip]:
    """every trip, with destinations loaded in one extra query."""
    stmt = select(Trip).options(selectinload(Trip.destinations)).order_by(Trip.id)
    return list(db.scalars(stmt))


def _get_destination_or_404(db: Session, destination_id: int) -> Destination:
    """the destination, or a 404."""
    destination = db.get(Destination, destination_id)
    if destination is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Destination {destination_id} not found",
        )
    return destination


def _with_details(db: Session, places: list[Place]) -> list[PlaceRead]:
    """saved places with their osm details, looked up in one query."""
    details = ask.details_for_refs(
        db, (place.source_ref for place in places if place.source_ref)
    )
    return [
        PlaceRead.model_validate(place).model_copy(
            update={"details": details.get(place.source_ref or "")}
        )
        for place in places
    ]


@router.get("/destinations/{destination_id}/places", response_model=list[PlaceRead])
def list_places(
    destination_id: int, db: Session = Depends(get_db)
) -> list[PlaceRead]:
    """a destination's saved places. both the list and the map read this."""
    _get_destination_or_404(db, destination_id)

    stmt = (
        select(Place)
        .where(Place.destination_id == destination_id)
        # must-go first, then newest.
        .order_by(Place.must_go.desc(), Place.created_at.desc(), Place.id.desc())
    )
    return _with_details(db, list(db.scalars(stmt)))


@router.post(
    "/destinations/{destination_id}/places",
    response_model=PlaceRead,
    status_code=status.HTTP_201_CREATED,
)
def create_place(
    destination_id: int,
    payload: PlaceCreate,
    db: Session = Depends(get_db),
) -> PlaceRead:
    """save one place. validation happens in PlaceCreate."""
    destination = _get_destination_or_404(db, destination_id)

    # destination_id comes from the url, never the body.
    place = Place(
        destination_id=destination.id,
        name=payload.name,
        # store the plain string the check constraint expects.
        category=str(payload.category),
        must_go=payload.must_go,
        latitude=payload.latitude,
        longitude=payload.longitude,
        source=str(payload.source),
        source_ref=payload.source_ref,
    )

    # refresh picks up the generated id and created_at.
    db.add(place)
    try:
        db.commit()
    except IntegrityError as exc:
        # the session is unusable after a failed commit, so roll back first.
        db.rollback()

        # 409 only for the duplicate-osm-place index; anything else is our bug.
        if "ix_places_destination_source_ref" in str(exc.orig):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"'{payload.name}' is already saved to this destination.",
            ) from exc
        raise

    db.refresh(place)

    # return the saved row so the ui can use the real id without refetching.
    return _with_details(db, [place])[0]


@router.delete(
    "/destinations/{destination_id}/places/{place_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_place(
    destination_id: int,
    place_id: int,
    db: Session = Depends(get_db),
) -> None:
    """remove a saved place. the osm cache is untouched, so it becomes a
    suggestion again."""
    _get_destination_or_404(db, destination_id)

    place = db.get(Place, place_id)
    # don't allow deleting another destination's place through this url.
    if place is None or place.destination_id != destination_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Place {place_id} not found in destination {destination_id}",
        )

    db.delete(place)
    db.commit()


def get_bounding_box(
    south: Annotated[float, Query(description="Southern edge of the search box.")],
    west: Annotated[float, Query(description="Western edge of the search box.")],
    north: Annotated[float, Query(description="Northern edge of the search box.")],
    east: Annotated[float, Query(description="Eastern edge of the search box.")],
) -> BoundingBox:
    """build a BoundingBox from the four query params, as a dependency.

    fastapi 0.115 either returns a 500 for model validation errors
    (Depends()) or mis-parses the params next to `categories` (Query()), so
    the box is built by hand and errors are re-raised as a normal 422.
    """
    try:
        return BoundingBox(south=south, west=west, north=north, east=east)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


@router.get("/destinations/{destination_id}/nearby", response_model=NearbyResponse)
def list_nearby(
    destination_id: int,
    # already validated by get_bounding_box.
    bbox: Annotated[BoundingBox, Depends(get_bounding_box)],
    categories: Annotated[
        list[Category],
        # ?categories=restaurant&categories=cafe
        Query(description="Which kinds of place to look for."),
    ] = list(ALL_CATEGORIES),
    db: Session = Depends(get_db),
) -> NearbyResponse:
    """cached osm places in a rectangle. a plain indexed read, no outside calls.

    counts first: few enough returns pins (`mode="places"`), too many returns
    grid counts (`mode="clusters"`). decided by the real count, not the zoom.
    `coverage` says whether the area has been imported at all.
    """
    _get_destination_or_404(db, destination_id)

    category_values = [str(category) for category in categories]

    # shared by the count and the fetch, so they always agree.
    in_viewport = (
        OsmPlace.latitude >= bbox.south,
        OsmPlace.latitude <= bbox.north,
        OsmPlace.longitude >= bbox.west,
        OsmPlace.longitude <= bbox.east,
        OsmPlace.category.in_(category_values),
    )

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

    # "already saved" is per destination, even though places are global.
    return NearbyResponse(
        mode="places",
        total=total,
        coverage=coverage,
        places=ask.to_suggestions(db, destination_id, places),
    )


@router.post("/destinations/{destination_id}/ask", response_model=AskResponse)
def ask_pina(
    destination_id: int,
    payload: AskRequest,
    db: Session = Depends(get_db),
    llm: Llm = Depends(get_llm),
    geocoder: NominatimClient = Depends(get_geocoder),
) -> AskResponse:
    """ask pina: "italian with outdoor seating near alfama" -> real places.

    1. the llm turns the question into a SearchIntent (app/llm.py).
    2. a named area is geocoded inside the destination.
    3. sql finds matching places, closest first (app/ask.py).

    the interpretation is returned too, so the ui can show what was understood.
    """
    destination = _get_destination_or_404(db, destination_id)
    destination_box = ask.destination_box(destination)

    try:
        intent = llm.parse(
            payload.question,
            destination.name,
            ask.cuisines_in(db, destination_box),
            previous=payload.previous,
        )
    except LlmUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except LlmError as exc:
        # 502: the model behind us gave a bad answer.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc

    area = ask.resolve_area(intent, destination, payload.viewport, geocoder)
    total, results = ask.search(db, destination.id, intent, area.box)

    # nothing has every feature? try once without them, and say which were
    # dropped. feature tags are sparse, so "not recorded" is the likely reason.
    dropped: list[Feature] = []
    if total == 0 and intent.features:
        relaxed = intent.model_copy(update={"features": []})
        total, results = ask.search(db, destination.id, relaxed, area.box)
        if total:
            dropped = list(intent.features)

    return AskResponse(
        reply=ask.describe(intent, area, total, dropped),
        interpretation=intent,
        area_label=area.label,
        area_found=area.found,
        total=total,
        results=results,
        dropped_features=dropped,
    )


def _clusters_in(
    db: Session, bbox: BoundingBox, in_viewport: tuple
) -> list[Cluster]:
    """group the viewport into a grid and count each cell.

    the step is relative to the viewport, so there are always about
    CLUSTER_GRID x CLUSTER_GRID cells. bubbles sit at the cells' average
    position rather than their centre.
    """
    lat_step = max((bbox.north - bbox.south) / CLUSTER_GRID, 1e-6)
    lon_step = max((bbox.east - bbox.west) / CLUSTER_GRID, 1e-6)

    # floor(x / step) is the cell index.
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
        # biggest first, so dense bubbles draw on top.
        .order_by(func.count().desc())
        # no limit: the grid already bounds the cell count, and a limit would
        # make the counts stop adding up to `total`.
    ).all()

    return [
        Cluster(latitude=float(lat), longitude=float(lon), count=int(count))
        for lat, lon, count in rows
    ]


def _coverage_for(db: Session, bbox: BoundingBox) -> Coverage:
    """whether ready extracts cover this viewport.

    approximate: one extract containing it all is COVERED, any overlap is
    PARTIAL, none is MISSING. a view spanning two regions reports PARTIAL.
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


# --- destinations ------------------------------------------------------------


@router.get("/geocode", response_model=list[GeocodeCandidate])
def geocode(
    q: Annotated[str, Query(min_length=1, description="A place name to look up.")],
    geocoder: NominatimClient = Depends(get_geocoder),
) -> list[GeocodeCandidate]:
    """candidate destinations for a typed name. several, so the user picks."""
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
    """add a destination and start filling its places in the background.

    returns straight away with sync_state='pending'.
    """
    trip = db.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Trip {trip_id} not found",
        )

    # shrink an oversized geocoder box, see MAX_DESTINATION_SPAN_DEGREES.
    south, west, north, east = clamp_area(
        payload.latitude,
        payload.longitude,
        payload.south,
        payload.west,
        payload.north,
        payload.east,
    )

    # resolve the region now, so an unsupported location shows up immediately.
    # resolved from coordinates, not the country, to pick the smallest region.
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
        # unsupported destinations are still created, for manual saves.
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

    # runs after the response is sent. skipped without a region, or when
    # auto-import is off (see AUTO_IMPORT_REGIONS).
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
    """a destination with its sync progress. the ui polls this while syncing."""
    destination = _get_destination_or_404(db, destination_id)
    return _destination_detail(db, destination)


@router.delete(
    "/destinations/{destination_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_destination(destination_id: int, db: Session = Depends(get_db)) -> None:
    """remove a destination and its saved places.

    the downloaded map data is kept, since it's shared by the whole region.
    if this was the region's last destination, a running download is
    cancelled.
    """
    destination = _get_destination_or_404(db, destination_id)
    region = destination.region

    db.delete(destination)
    db.commit()

    if region is not None:
        still_needed = db.scalar(
            select(func.count())
            .select_from(Destination)
            .where(Destination.region == region)
        )
        if not still_needed:
            cancel_download(region)


@router.post("/destinations/{destination_id}/sync", response_model=DestinationDetail)
def resync_destination(
    destination_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    start_sync: Callable[[int], None] = Depends(get_sync_runner),
) -> DestinationDetail:
    """re-run the import, resuming whatever is incomplete. saved places are untouched."""
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
    """a destination plus its cache status, counted in sql."""
    # by area, since osm_places is global.
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

    # lets the ui tell a long download from a short import.
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
