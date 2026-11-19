/**
 * The Places view: every saved place, pinned or not, unfiltered.
 *
 * This list is the complete record — the map's category chips do not apply
 * here. That is deliberate: a hidden row in a list gives no visual hint that
 * anything is missing, whereas a hidden marker on a map is obvious from the
 * chip state. So the list stays the reliable answer to "where did my place go?".
 *
 * A pinned place is a button that sends the map to it. An unpinned one is not,
 * because there is nowhere to fly to.
 */

import { PhotosLink } from "./PhotosLink";
import { CATEGORY_ICONS, CATEGORY_LABELS, type Place } from "./types";

interface PlaceListProps {
  places: Place[];
  destinationName: string;
  /** Switches to the Map view, pans to this place and opens its popup. */
  onFocusPlace: (placeId: number) => void;
  /** Deletes the place. Called only after the user has confirmed. */
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
                {place.latitude === null ? (
                  <span className="place__nopin"> · no pin</span>
                ) : (
                  <span className="place__showmap"> · show on map</span>
                )}
              </span>
            </div>

            {place.must_go && (
              // A text label, not only a colour or an icon, so the status is
              // available to a screen reader and in greyscale.
              <span className="badge badge--must-go">★ Must-go</span>
            )}
          </>
        );

        return (
          // The Delete button is a *sibling* of the row, not inside it: a
          // pinned row is itself a <button>, and a button inside a button is
          // invalid HTML — clicks would also bubble and fly to the map.
          <li key={place.id} className="places__item">
            {place.latitude === null ? (
              // A div, not a disabled button: there is no action here, and a
              // disabled button would still be announced as an unavailable
              // control rather than as plain content.
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
              <PhotosLink name={place.name} near={destinationName} />
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
