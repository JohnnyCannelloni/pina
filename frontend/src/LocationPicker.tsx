/**
 * optional mini-map in the save form: click to drop or move the pin.
 *
 * stateless: the pin lives in AddPlaceForm's state with the other fields.
 */

import { MapContainer, Marker, TileLayer, useMapEvents } from "react-leaflet";

import { draftIcon } from "./leafletIcons";
import { CITY_ZOOM, OSM_ATTRIBUTION, OSM_TILE_URL } from "./mapConfig";

export interface Coordinates {
  latitude: number;
  longitude: number;
}

interface LocationPickerProps {
  /** where the map opens: the destination's coordinates. */
  center: Coordinates;
  /** the chosen pin, or null for no pin. */
  value: Coordinates | null;
  onChange: (coordinates: Coordinates) => void;
}

/** map events need a child of MapContainer, where the map instance is available. */
function ClickCapture({ onChange }: { onChange: (c: Coordinates) => void }) {
  useMapEvents({
    click(event) {
      // rename to match the api fields.
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
        // the form scrolls on a phone, so wheel zoom would hijack it. buttons
        // and pinch still work.
        scrollWheelZoom={false}
      >
        <TileLayer url={OSM_TILE_URL} attribution={OSM_ATTRIBUTION} />
        <ClickCapture onChange={onChange} />
        {/* one marker or none; clicking again moves it. */}
        {value !== null && (
          <Marker
            position={[value.latitude, value.longitude]}
            icon={draftIcon}
            // dragging for fine adjustment.
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
