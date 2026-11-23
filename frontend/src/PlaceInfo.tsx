/**
 * what osm knows about a place, plus the photos link. used in both map popups
 * and in ask pina results. each line is drawn only if the data exists.
 */

import { PhotosLink } from "./PhotosLink";
import { FEATURE_LABELS, formatCuisine, type PlaceDetails } from "./types";

interface PlaceInfoProps {
  name: string;
  /** the destination's name, for the photos search. */
  near: string;
  latitude: number | null;
  longitude: number | null;
  details: PlaceDetails | null;
  /** skip cuisine and features when the caller already shows them. */
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
        // osm's own syntax, shown as written.
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
          // the backend only stores http(s) links (extract.py _clean_website).
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
