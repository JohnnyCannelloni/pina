/**
 * the map view: saved pins plus nearby osm places.
 *
 * markers load on open and whenever you stop panning. that's affordable because
 * /nearby is an indexed query on our own osm_places table.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import L from "leaflet";
import {
  MapContainer,
  Marker,
  Popup,
  TileLayer,
  useMap,
  useMapEvents,
} from "react-leaflet";

import { fetchNearby } from "./api";
import { CategoryFilter } from "./CategoryFilter";
import { ClusterMarkers } from "./ClusterMarkers";
import { mustGoIcon, placeIcon } from "./leafletIcons";
import { MapLegend } from "./MapLegend";
import { PlaceInfo } from "./PlaceInfo";
import {
  CITY_ZOOM,
  LABEL_ZOOM,
  MAX_FIT_ZOOM,
  MOVE_DEBOUNCE_MS,
  OSM_ATTRIBUTION,
  OSM_TILE_URL,
} from "./mapConfig";
import { SuggestionMarkers } from "./SuggestionMarkers";
import {
  ALL_CATEGORIES,
  CATEGORY_LABELS,
  type Bounds,
  type Category,
  type DestinationDetail,
  type NearbyResponse,
  type Place,
  type Suggestion,
} from "./types";

interface MapViewProps {
  places: Place[];
  destination: DestinationDetail;
  onShowList: () => void;
  onSaved: (place: Place) => void;
  focusedPlaceId: number | null;
  /** ask pina results, drawn highlighted on top. */
  askResults: Suggestion[];
  /** an ask result to fly to (its source_ref), from "show on map". */
  focusedRef: string | null;
  /** reports the visible area whenever it settles, for ask pina. */
  onViewportChange: (bounds: Bounds) => void;
}

type PinnedPlace = Place & { latitude: number; longitude: number };

function hasPin(place: Place): place is PinnedPlace {
  return place.latitude !== null && place.longitude !== null;
}

/** frames the map once per mount: on saved pins if there are any, otherwise
 *  on the destination's stored area. */
function FitToArea({
  destination,
  pinned,
}: {
  destination: DestinationDetail;
  pinned: PinnedPlace[];
}) {
  const map = useMap();
  const hasFitted = useRef(false);

  useEffect(() => {
    if (hasFitted.current) {
      return;
    }
    hasFitted.current = true;

    if (pinned.length > 0) {
      map.fitBounds(
        L.latLngBounds(pinned.map((p) => [p.latitude, p.longitude])),
        { padding: [40, 40], maxZoom: MAX_FIT_ZOOM },
      );
      return;
    }

    map.fitBounds(
      L.latLngBounds(
        [destination.south, destination.west],
        [destination.north, destination.east],
      ),
      { padding: [20, 20], maxZoom: MAX_FIT_ZOOM },
    );
  }, [map, destination, pinned]);

  return null;
}

/**
 * loads places for whatever is on screen, again after every pan.
 *
 * `moveend` fires once a gesture settles, the debounce absorbs quick pans, and
 * the request id stops a slow old response overwriting a newer one.
 */
function SuggestionLoader({
  destinationId,
  categories,
  onLoaded,
  onError,
  onZoom,
}: {
  destinationId: number;
  categories: readonly Category[];
  onLoaded: (response: NearbyResponse) => void;
  onError: (message: string | null) => void;
  onZoom: (zoom: number) => void;
}) {
  const map = useMap();
  // only the newest request may update the map.
  const requestId = useRef(0);
  const timer = useRef<number | null>(null);

  const load = useCallback(async () => {
    const bounds = map.getBounds();
    const id = ++requestId.current;

    try {
      const response = await fetchNearby(
        destinationId,
        {
          south: bounds.getSouth(),
          west: bounds.getWest(),
          north: bounds.getNorth(),
          east: bounds.getEast(),
        },
        categories,
      );
      if (id === requestId.current) {
        onLoaded(response);
        onError(null);
      }
    } catch (caught) {
      if (id === requestId.current) {
        // a local read failing means our own api is down.
        onError(
          caught instanceof Error ? caught.message : "Could not load places.",
        );
      }
    }
  }, [map, destinationId, categories, onLoaded, onError]);

  const schedule = useCallback(() => {
    if (timer.current !== null) {
      window.clearTimeout(timer.current);
    }
    timer.current = window.setTimeout(() => void load(), MOVE_DEBOUNCE_MS);
  }, [load]);

  useMapEvents({
    moveend: () => {
      onZoom(map.getZoom());
      schedule();
    },
  });

  // load on mount and whenever the categories change (the server filters them).
  useEffect(() => {
    onZoom(map.getZoom());
    void load();
    return () => {
      if (timer.current !== null) {
        window.clearTimeout(timer.current);
      }
    };
  }, [load, map, onZoom]);

  return null;
}

/** pans to the focused place and opens its popup. */
function FlyToFocused({
  focusedPlaceId,
  markers,
}: {
  focusedPlaceId: number | null;
  markers: React.RefObject<Map<number, L.Marker>>;
}) {
  const map = useMap();

  useEffect(() => {
    if (focusedPlaceId === null) {
      return;
    }
    const marker = markers.current?.get(focusedPlaceId);
    if (!marker) {
      return;
    }
    map.flyTo(marker.getLatLng(), Math.max(map.getZoom(), MAX_FIT_ZOOM), {
      duration: 0.6,
    });
    map.once("moveend", () => marker.openPopup());
  }, [focusedPlaceId, map, markers]);

  return null;
}

/** reports the visible area on open and after every pan. */
function ViewportReporter({
  onChange,
}: {
  onChange: (bounds: Bounds) => void;
}) {
  const map = useMap();
  const report = useCallback(() => {
    const b = map.getBounds();
    onChange({
      south: b.getSouth(),
      west: b.getWest(),
      north: b.getNorth(),
      east: b.getEast(),
    });
  }, [map, onChange]);

  useMapEvents({ moveend: report });
  useEffect(report, [report]);
  return null;
}

/**
 * frames a new set of ask results, or flies to one of them.
 *
 * runs per results array, so a new answer is framed once and later panning
 * isn't undone.
 */
function ShowAskResults({
  results,
  focusedRef,
  markers,
}: {
  results: Suggestion[];
  focusedRef: string | null;
  markers: React.RefObject<Map<string, L.Marker>>;
}) {
  const map = useMap();

  useEffect(() => {
    if (focusedRef !== null) {
      const marker = markers.current?.get(focusedRef);
      if (marker) {
        map.flyTo(marker.getLatLng(), Math.max(map.getZoom(), MAX_FIT_ZOOM), {
          duration: 0.6,
        });
        map.once("moveend", () => marker.openPopup());
        return;
      }
    }
    if (results.length > 0) {
      map.fitBounds(
        L.latLngBounds(results.map((r) => [r.latitude, r.longitude])),
        { padding: [40, 40], maxZoom: MAX_FIT_ZOOM },
      );
    }
  }, [results, focusedRef, map, markers]);

  return null;
}

export function MapView({
  places,
  destination,
  onShowList,
  onSaved,
  focusedPlaceId,
  askResults,
  focusedRef,
  onViewportChange,
}: MapViewProps) {
  const [shown, setShown] = useState<ReadonlySet<Category>>(
    () => new Set(ALL_CATEGORIES),
  );
  // the whole response: `mode` decides what to draw, `coverage` what to say
  // when it's empty.
  const [nearby, setNearby] = useState<NearbyResponse>({
    mode: "places",
    total: 0,
    coverage: "missing",
    places: [],
    clusters: [],
  });
  const [loadError, setLoadError] = useState<string | null>(null);
  const [zoom, setZoom] = useState(CITY_ZOOM);

  const markers = useRef<Map<number, L.Marker>>(new Map());
  const askMarkers = useRef<Map<string, L.Marker>>(new Map());
  const handleAskMarker = useCallback(
    (sourceRef: string, marker: L.Marker | null) => {
      if (marker) {
        askMarkers.current.set(sourceRef, marker);
      } else {
        askMarkers.current.delete(sourceRef);
      }
    },
    [],
  );

  const pinned = useMemo(() => places.filter(hasPin), [places]);

  // osm places already saved, derived from `places`.
  const savedRefs = useMemo(
    () =>
      new Set(
        places
          .map((place) => place.source_ref)
          .filter((ref): ref is string => ref !== null),
      ),
    [places],
  );

  // memoised so SuggestionLoader's effect doesn't refetch every render.
  const activeCategories = useMemo(
    () => ALL_CATEGORIES.filter((category) => shown.has(category)),
    [shown],
  );

  const visiblePins = pinned.filter((place) => shown.has(place.category));
  // ask results not yet saved. not filtered by the chips: you asked for them.
  const visibleAskResults = askResults.filter(
    (result) => !savedRefs.has(result.source_ref),
  );
  const askRefs = new Set(visibleAskResults.map((r) => r.source_ref));

  // empty in clusters mode, where the server sent counts instead.
  const visibleSuggestions = nearby.places.filter(
    (suggestion) =>
      !suggestion.already_saved &&
      !savedRefs.has(suggestion.source_ref) &&
      // drawn once, as a highlighted result.
      !askRefs.has(suggestion.source_ref),
  );
  const showingClusters = nearby.mode === "clusters";

  const withoutPin = places.length - pinned.length;
  const showLabels = zoom >= LABEL_ZOOM;

  const handleLoaded = useCallback((response: NearbyResponse) => {
    setNearby(response);
  }, []);

  return (
    <div className="mapview">
      <div className="mapview__controls">
        <CategoryFilter selected={shown} onChange={setShown} />
        <span className="mapview__hint" role="status">
          {showingClusters
            ? `${nearby.total.toLocaleString()} places here — zoom in or tap a circle`
            : visibleSuggestions.length > 0
              ? `${visibleSuggestions.length} place${
                  visibleSuggestions.length === 1 ? "" : "s"
                } to explore here`
              : nearby.coverage === "missing"
                ? "No map data loaded for this area yet."
                : "No places here — try panning or zooming out."}
          {!showingClusters &&
            !showLabels &&
            visibleSuggestions.length > 0 &&
            " · zoom in for names"}
        </span>
      </div>

      {loadError !== null && (
        <p className="error" role="alert">
          <strong>Could not load places.</strong> {loadError}
        </p>
      )}

      <MapContainer
        className="mapview__map"
        center={[destination.latitude, destination.longitude]}
        zoom={CITY_ZOOM}
        scrollWheelZoom
      >
        <TileLayer url={OSM_TILE_URL} attribution={OSM_ATTRIBUTION} />
        <FitToArea destination={destination} pinned={pinned} />
        <SuggestionLoader
          destinationId={destination.id}
          categories={activeCategories}
          onLoaded={handleLoaded}
          onError={setLoadError}
          onZoom={setZoom}
        />
        <FlyToFocused focusedPlaceId={focusedPlaceId} markers={markers} />
        <ViewportReporter onChange={onViewportChange} />
        <ShowAskResults
          results={askResults}
          focusedRef={focusedRef}
          markers={askMarkers}
        />

        {visiblePins.map((place) => (
          <Marker
            key={place.id}
            position={[place.latitude, place.longitude]}
            icon={place.must_go ? mustGoIcon : placeIcon}
            ref={(instance) => {
              if (instance) {
                markers.current.set(place.id, instance);
              } else {
                markers.current.delete(place.id);
              }
            }}
          >
            <Popup>
              <span className="popup__name">{place.name}</span>
              <span className="popup__meta">
                {CATEGORY_LABELS[place.category]}
                {place.source === "osm" && " · from OpenStreetMap"}
              </span>
              <span
                className={
                  place.must_go ? "popup__must-go" : "popup__must-go--no"
                }
              >
                {place.must_go ? "★ Must-go" : "Not marked must-go"}
              </span>
              <PlaceInfo
                name={place.name}
                near={destination.name}
                latitude={place.latitude}
                longitude={place.longitude}
                details={place.details}
              />
            </Popup>
          </Marker>
        ))}

        {/* one or the other, chosen by `mode`. */}
        {showingClusters ? (
          <ClusterMarkers clusters={nearby.clusters} />
        ) : (
          <SuggestionMarkers
            suggestions={visibleSuggestions}
            destinationId={destination.id}
            destinationName={destination.name}
            onSaved={onSaved}
            showLabels={showLabels}
          />
        )}

        <SuggestionMarkers
          suggestions={visibleAskResults}
          destinationId={destination.id}
          destinationName={destination.name}
          onSaved={onSaved}
          showLabels
          highlighted
          onMarker={handleAskMarker}
        />
      </MapContainer>

      <MapLegend />

      {/* say why the map is empty: never imported vs. genuinely nothing here. */}
      {nearby.coverage === "missing" && nearby.total === 0 && (
        <p className="mapview__note" role="status">
          No OpenStreetMap data has been loaded for this area yet.
          {destination.region
            ? ` Run: python -m app.fill_places --region ${destination.region}`
            : " Pina could not work out which map region covers it."}
        </p>
      )}

      {nearby.coverage === "partial" && (
        <p className="mapview__note" role="status">
          Part of this view is outside the loaded map data — panning further may
          show nothing.
        </p>
      )}

      {withoutPin > 0 && (
        <p className="mapview__note">
          {withoutPin} saved {withoutPin === 1 ? "place has" : "places have"} no
          pin.{" "}
          <button type="button" className="linkbutton" onClick={onShowList}>
            See all places
          </button>
        </p>
      )}
    </div>
  );
}
