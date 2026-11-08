/**
 * The Places view: every saved place, pinned or not.
 *
 * This list is the complete record. The map shows a subset (only places with
 * coordinates), so anything saved without a pin is still findable here — which
 * is why the map view links back to this one.
 */

import { CATEGORY_ICONS, CATEGORY_LABELS, type Place } from "./types";

interface PlaceListProps {
  places: Place[];
  destinationName: string;
}

export function PlaceList({ places, destinationName }: PlaceListProps) {
  if (places.length === 0) {
    return (
      <div className="empty" role="status">
        <p className="empty__title">No places saved for {destinationName} yet.</p>
        <p className="empty__hint">
          Add your first one above — a name and a category is all it takes.
        </p>
      </div>
    );
  }

  return (
    <ul className="places">
      {places.map((place) => (
        <li key={place.id} className="place">
          <span className="place__icon" aria-hidden="true">
            {CATEGORY_ICONS[place.category]}
          </span>

          <div className="place__body">
            <span className="place__name">{place.name}</span>
            <span className="place__meta">
              {CATEGORY_LABELS[place.category]}
              {/* Showing pin status in the list answers "why isn't this on the
                  map?" without the user having to switch views and count. */}
              {place.latitude === null && (
                <span className="place__nopin"> · no pin</span>
              )}
            </span>
          </div>

          {place.must_go && (
            // A text label, not only a colour or an icon, so the status is
            // available to a screen reader and in greyscale.
            <span className="badge badge--must-go">★ Must-go</span>
          )}
        </li>
      ))}
    </ul>
  );
}
