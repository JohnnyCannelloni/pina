/**
 * What OpenStreetMap knows about a place, plus the Photos link.
 *
 * Used in both map popups and in the Ask Pina results, so a place reads the
 * same wherever you meet it. Everything is optional: OSM records a cuisine for
 * about a third of restaurants and opening hours for a fifth, so a line is
 * drawn only when there is something to put on it. A place with no details at
 * all still gets the Photos link, which is always available.
 */

import { PhotosLink } from "./PhotosLink";
import { FEATURE_LABELS, formatCuisine, type PlaceDetails } from "./types";

interface PlaceInfoProps {
  name: string;
  /** The destination's name, for the Photos search. */
  near: string;
  latitude: number | null;
  longitude: number | null;
  details: PlaceDetails | null;
  /** Skip cuisine and features, when the caller already shows them (an Ask
   *  result's summary line). */
  withoutSummary?: boolean;
}

export function PlaceInfo({
  name,
  near,
  latitude,
  longitude,
  details,
  withoutSummary = false,
}: PlaceInfoProps) {
  return (
    <div className="info">
      {!withoutSummary && details !== null && details.cuisine.length > 0 && (
        <span className="info__line">
          {details.cuisine.map(formatCuisine).join(", ")}
        </span>
      )}

      {!withoutSummary && details !== null && details.features.length > 0 && (
        <span className="info__features">
          {details.features.map((feature) => (
            <span key={feature} className="info__feature">
              {FEATURE_LABELS[feature]}
            </span>
          ))}
        </span>
      )}

      {details?.opening_hours && (
        // OSM's own syntax ("Mo-Fr 12:00-15:00; Sa 19:00-23:00"), shown as
        // written. Parsing it into "open now" is a project of its own.
        <span className="info__line info__muted">🕒 {details.opening_hours}</span>
      )}

      {details?.street && (
        <span className="info__line info__muted">📍 {details.street}</span>
      )}

      <span className="info__links">
        <PhotosLink
          name={name}
          near={near}
          latitude={latitude}
          longitude={longitude}
        />
        {details?.website && (
          // The backend only lets http(s) links through (extract.py
          // _clean_website), so this href cannot be a javascript: URL.
          <a
            href={details.website}
            target="_blank"
            rel="noopener noreferrer"
            className="photos-link"
          >
            🌐 Website
          </a>
        )}
        {details?.phone && (
          <a href={`tel:${details.phone.replace(/\s/g, "")}`} className="photos-link">
            📞 {details.phone}
          </a>
        )}
      </span>
    </div>
  );
}
