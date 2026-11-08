/**
 * The optional mini-map inside the save form. Click to drop or move a pin.
 *
 * READ THIS FOURTH — after AddPlaceForm. It is where the two numbers in the
 * POST body come from, and it is deliberately dumb: it holds no state of its
 * own and saves nothing. It takes a value, reports clicks, and that is all. The
 * pin lives in AddPlaceForm's form state alongside the name and category, which
 * is why it is submitted by the same Save button and cleared by the same reset.
 */

import { MapContainer, Marker, TileLayer, useMapEvents } from "react-leaflet";

import { draftIcon } from "./leafletIcons";
import { CITY_ZOOM, OSM_ATTRIBUTION, OSM_TILE_URL } from "./mapConfig";

export interface Coordinates {
  latitude: number;
  longitude: number;
}

interface LocationPickerProps {
  /** Where the map opens — the destination's own coordinates, from the API. */
  center: Coordinates;
  /** The pin currently chosen, or null for "no pin". */
  value: Coordinates | null;
  onChange: (coordinates: Coordinates) => void;
}

/**
 * Leaflet's map instance is not a React component, so map events are not React
 * props. `useMapEvents` is react-leaflet's bridge: it must be called from a
 * component *inside* MapContainer, because that is where the map instance is
 * available on context. Hence this tiny renderless child rather than an
 * onClick on MapContainer itself.
 */
function ClickCapture({ onChange }: { onChange: (c: Coordinates) => void }) {
  useMapEvents({
    click(event) {
      // e.latlng is Leaflet's naming; renamed here so that from this point on
      // the values carry the same names as the API and the database columns.
      onChange({ latitude: event.latlng.lat, longitude: event.latlng.lng });
    },
  });
  return null;
}

export function LocationPicker({ center, value, onChange }: LocationPickerProps) {
  return (
    <div className="picker">
      <MapContainer
        className="picker__map"
        center={[center.latitude, center.longitude]}
        zoom={CITY_ZOOM}
        // The form is a scrollable column on a phone. Scroll-wheel zoom would
        // hijack that scroll the moment the cursor crossed the map, so it is
        // off; the +/- buttons and pinch-zoom still work.
        scrollWheelZoom={false}
      >
        <TileLayer url={OSM_TILE_URL} attribution={OSM_ATTRIBUTION} />
        <ClickCapture onChange={onChange} />
        {/* One marker or none — clicking again moves this one rather than
            adding a second, because `value` is a single value, not a list. */}
        {value !== null && (
          <Marker
            position={[value.latitude, value.longitude]}
            icon={draftIcon}
            // Dragging is a second, more precise way to adjust the pin, and it
            // is how most people expect a map marker to behave.
            draggable
            eventHandlers={{
              dragend(event) {
                const { lat, lng } = event.target.getLatLng();
                onChange({ latitude: lat, longitude: lng });
              },
            }}
          />
        )}
      </MapContainer>
    </div>
  );
}
