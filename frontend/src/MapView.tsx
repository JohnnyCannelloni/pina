/**
 * The Map view: the same saved places as the list, filtered to those with a pin.
 *
 * There is no separate map data source and no separate fetch. `places` is the
 * identical array PlaceList renders, passed down from DestinationPage. That is
 * what makes "save a place, switch to Map, refresh, find the pin in the same
 * spot" true without any syncing — the pin is just a column on the row.
 */

import { useEffect, useRef } from "react";
import L from "leaflet";
import { MapContainer, Marker, Popup, TileLayer, useMap } from "react-leaflet";

import { mustGoIcon, placeIcon } from "./leafletIcons";
import {
  CITY_ZOOM,
  MAX_FIT_ZOOM,
  OSM_ATTRIBUTION,
  OSM_TILE_URL,
} from "./mapConfig";
import { CATEGORY_LABELS, type Destination, type Place } from "./types";

interface MapViewProps {
  places: Place[];
  destination: Destination;
  /** Lets the "N places have no pin" notice send the user to the list. */
  onShowList: () => void;
}

/**
 * A place that definitely has coordinates.
 *
 * TypeScript cannot tell that `place.latitude !== null` also narrows
 * `place.longitude`, because they are independent fields as far as the type
 * system knows — the both-or-neither rule lives in the database, not the type.
 * This alias plus the type guard below gives the compiler that knowledge once,
 * instead of forcing a non-null assertion at every use.
 */
type PinnedPlace = Place & { latitude: number; longitude: number };

function hasPin(place: Place): place is PinnedPlace {
  return place.latitude !== null && place.longitude !== null;
}

/**
 * Frames the map on the pins, once per mount.
 *
 * Leaflet's imperative API is the right tool here: "fit these bounds" is an
 * action, not a prop. `useMap` reaches the map instance from inside
 * MapContainer.
 *
 * The ref guard makes this fire once per mount rather than on every render.
 * Switching tabs unmounts this component, so going to Map always reframes on
 * the current pins — but saving a place while already on Map will not yank the
 * view out from under you.
 */
function FitToPins({ pinned }: { pinned: PinnedPlace[] }) {
  const map = useMap();
  const hasFitted = useRef(false);

  useEffect(() => {
    if (hasFitted.current || pinned.length === 0) {
      return;
    }
    hasFitted.current = true;

    const bounds = L.latLngBounds(
      pinned.map((place) => [place.latitude, place.longitude]),
    );
    // maxZoom stops a single pin from zooming to maximum street detail, which
    // loses all sense of where in the city it is. padding keeps markers off
    // the very edge of the frame.
    map.fitBounds(bounds, { padding: [40, 40], maxZoom: MAX_FIT_ZOOM });
  }, [map, pinned]);

  return null;
}

export function MapView({ places, destination, onShowList }: MapViewProps) {
  const pinned = places.filter(hasPin);
  const withoutPin = places.length - pinned.length;

  return (
    <div className="mapview">
      <MapContainer
        className="mapview__map"
        // The fallback when there are no pins at all: the destination's own
        // coordinates, which came from the database — so there is no Venice
        // constant anywhere in this codebase.
        center={[destination.latitude, destination.longitude]}
        zoom={CITY_ZOOM}
        scrollWheelZoom
      >
        <TileLayer url={OSM_TILE_URL} attribution={OSM_ATTRIBUTION} />
        <FitToPins pinned={pinned} />

        {pinned.map((place) => (
          <Marker
            key={place.id}
            position={[place.latitude, place.longitude]}
            icon={place.must_go ? mustGoIcon : placeIcon}
          >
            {/* Selecting a marker opens this. Leaflet handles the click, the
                positioning, and closing it on the next click. */}
            <Popup>
              <span className="popup__name">{place.name}</span>
              <span className="popup__meta">
                {CATEGORY_LABELS[place.category]}
              </span>
              <span
                className={
                  place.must_go ? "popup__must-go" : "popup__must-go--no"
                }
              >
                {place.must_go ? "★ Must-go" : "Not marked must-go"}
              </span>
            </Popup>
          </Marker>
        ))}
      </MapContainer>

      {/* Empty state: distinct from the list's, because "nothing saved" and
          "things saved, none pinned" need different advice. */}
      {pinned.length === 0 && (
        <div className="mapview__empty" role="status">
          <p className="empty__title">No pinned places yet.</p>
          <p className="empty__hint">
            {places.length === 0
              ? `Save a place for ${destination.name} and drop a pin to see it here.`
              : "Drop a pin while saving a place to see it on the map."}
          </p>
        </div>
      )}

      {/* A place saved without a pin is not on the map, so say so here rather
          than letting the user wonder where it went. */}
      {withoutPin > 0 && (
        <p className="mapview__note">
          {withoutPin} {withoutPin === 1 ? "place has" : "places have"} no pin
          yet.{" "}
          <button type="button" className="linkbutton" onClick={onShowList}>
            See all places
          </button>
        </p>
      )}
    </div>
  );
}
