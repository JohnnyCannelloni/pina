/**
 * the places view: every saved place, unfiltered (the map's category chips
 * don't apply here). pinned rows fly to the map; unpinned rows aren't buttons.
 */

import { PhotosLink } from "./PhotosLink";
import {
  CATEGORY_ICONS,
  CATEGORY_LABELS,
  formatCuisine,
  type Place,
} from "./types";

interface PlaceListProps {
  places: Place[];
  destinationName: string;
  /** switch to the map, pan to this place and open its popup. */
  onFocusPlace: (placeId: number) => void;
  /** delete the place. only called after the user confirms. */
  onDeletePlace: (placeId: number) => void;
}

export function PlaceList({
  places,
  destinationName,
  onFocusPlace,
  onDeletePlace,
}: PlaceListProps) {
  if (places.length === 0) {
    return (
      <div className="empty" role="status">
        <p className="empty__title">No places saved for {destinationName} yet.</p>
        <p className="empty__hint">
          Add one above, or open the Map and press{" "}
          <strong>Search this area</strong> to find places near{" "}
          {destinationName}.
        </p>
      </div>
    );
  }

  return (
    <ul className="places">
      {places.map((place) => {
        const content = (
          <>
            <span className="place__icon" aria-hidden="true">
              {CATEGORY_ICONS[place.category]}
            </span>

            <div className="place__body">
              <span className="place__name">{place.name}</span>
              <span className="place__meta">
                {CATEGORY_LABELS[place.category]}
                {/* just the cuisine; hours and links are in the map popup. */}
                {place.details && place.details.cuisine.length > 0 &&
                  ` · ${place.details.cuisine.map(formatCuisine).join(", ")}`}
                {place.latitude === null ? (
                  <span className="place__nopin"> · no pin</span>
                ) : (
                  <span className="place__showmap"> · show on map</span>
                )}
              </span>
            </div>

            {place.must_go && (
              // a text label, so it works for screen readers and in greyscale.
              <span className="badge badge--must-go">★ Must-go</span>
            )}
          </>
        );

        return (
          // actions sit beside the row, since a pinned row is itself a button.
          <li key={place.id} className="places__item">
            {place.latitude === null ? (
              // a div, not a disabled button: there's no action here.
              <div className="place">{content}</div>
            ) : (
              <button
                type="button"
                className="place place--clickable"
                onClick={() => onFocusPlace(place.id)}
              >
                {content}
              </button>
            )}
            <div className="place__actions">
              <PhotosLink
                name={place.name}
                near={destinationName}
                latitude={place.latitude}
                longitude={place.longitude}
              />
              <button
                type="button"
                className="linkbutton place__delete"
                aria-label={`Delete ${place.name}`}
                onClick={() => {
                  if (window.confirm(`Delete ${place.name}?`)) {
                    onDeletePlace(place.id);
                  }
                }}
              >
                Delete
              </button>
            </div>
          </li>
        );
      })}
    </ul>
  );
}
