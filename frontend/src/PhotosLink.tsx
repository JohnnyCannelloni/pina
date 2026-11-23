/**
 * A link to a place's Google Maps page, where its photos are.
 *
 * Why a link rather than photos in the app: OpenStreetMap has photos for about
 * 0.3% of restaurants (measured on Portugal: 75 of 29,046), so the open data
 * cannot show dishes. Google Maps can — people upload photos of what they ate —
 * but showing those *inside* Pina needs a Places API key with billing.
 *
 * A Maps URL needs no key at all (Google documents this), costs nothing, and
 * opens the real listing with its photos, reviews and hours. It is the cheapest
 * thing that actually answers "what is the food like here?".
 */

/** How far in the Maps link zooms. 18 is street level, so the search only
 *  considers what is on the block around the pin. */
const PLACE_ZOOM = 18;

/**
 * Google Maps URL that opens the place's own page.
 *
 * With coordinates, the URL is `/maps/search/<name>/@lat,lng,zoom`: a search
 * for the name, run at the pin at street level. A name alone, even with the
 * city ("Caffè Florian, Venice"), often matches several places, and then Maps
 * shows a list of results instead of the place. Searching at the exact spot
 * leaves one match, and Maps opens it directly.
 *
 * Without coordinates (a saved place with no pin), all we have is the name, so
 * it falls back to the documented `api=1` search with the city added.
 *
 * Opening a place page with certainty would need its Google place ID, which
 * only the paid Places API gives out. OpenStreetMap doesn't store Google IDs.
 */
export function googleMapsUrl(
  name: string,
  near: string,
  latitude: number | null = null,
  longitude: number | null = null,
): string {
  if (latitude !== null && longitude !== null) {
    // encodeURIComponent, not URLSearchParams: the name is a path segment
    // here, so a "/" in it must become %2F and a space %20, not "+".
    const at = `@${latitude.toFixed(6)},${longitude.toFixed(6)},${PLACE_ZOOM}z`;
    return `https://www.google.com/maps/search/${encodeURIComponent(name)}/${at}`;
  }
  const params = new URLSearchParams({ api: "1", query: `${name}, ${near}` });
  return `https://www.google.com/maps/search/?${params.toString()}`;
}

interface PhotosLinkProps {
  name: string;
  /** The destination's name, used only when there are no coordinates. */
  near: string;
  latitude?: number | null;
  longitude?: number | null;
  className?: string;
}

export function PhotosLink({
  name,
  near,
  latitude = null,
  longitude = null,
  className,
}: PhotosLinkProps) {
  return (
    <a
      // A new tab, so the map and any half-filled form survive.
      // noopener stops the opened page from reaching back into this one
      // through window.opener.
      href={googleMapsUrl(name, near, latitude, longitude)}
      target="_blank"
      rel="noopener noreferrer"
      className={`photos-link${className ? ` ${className}` : ""}`}
      aria-label={`Photos of ${name} on Google Maps (opens in a new tab)`}
    >
      📷 Photos
    </a>
  );
}
