/**
 * a link to a place's google maps page, for photos and reviews.
 *
 * osm has photos for ~0.3% of restaurants, and showing google's photos in the
 * app needs a paid places api key. a maps url needs no key.
 */

/** street level, so the search only considers the block around the pin. */
const PLACE_ZOOM = 18;

/**
 * google maps url that opens the place's own page.
 *
 * with coordinates: `/maps/search/<name>/@lat,lng,zoom`, a name search at the
 * pin, which usually has a single match and opens it directly. without them:
 * the documented `api=1` search with the city appended. a guaranteed place
 * page would need a google place id, which only the paid api provides.
 */
export function googleMapsUrl(
  name: string,
  near: string,
  latitude: number | null = null,
  longitude: number | null = null,
): string {
  if (latitude !== null && longitude !== null) {
    // the name is a path segment here, so "/" -> %2F and space -> %20.
    const at = `@${latitude.toFixed(6)},${longitude.toFixed(6)},${PLACE_ZOOM}z`;
    return `https://www.google.com/maps/search/${encodeURIComponent(name)}/${at}`;
  }
  const params = new URLSearchParams({ api: "1", query: `${name}, ${near}` });
  return `https://www.google.com/maps/search/?${params.toString()}`;
}

interface PhotosLinkProps {
  name: string;
  /** the destination's name, used only without coordinates. */
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
      // new tab, so the map and any half-filled form survive. noopener keeps
      // the opened page from reaching window.opener.
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
