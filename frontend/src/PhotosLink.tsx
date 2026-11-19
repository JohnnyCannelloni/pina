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

/**
 * Google Maps search URL for a place, e.g. "Caffè Florian, Venice".
 *
 * The city is appended because a name alone is ambiguous — there are a lot of
 * cafés called "Central" — and Maps URLs accept either a name *or*
 * coordinates, not both.
 */
export function googleMapsUrl(name: string, near: string): string {
  const params = new URLSearchParams({ api: "1", query: `${name}, ${near}` });
  return `https://www.google.com/maps/search/?${params.toString()}`;
}

interface PhotosLinkProps {
  name: string;
  /** The destination's name, to disambiguate the search. */
  near: string;
  className?: string;
}

export function PhotosLink({ name, near, className }: PhotosLinkProps) {
  return (
    <a
      // A new tab, so the map and any half-filled form survive.
      // noopener stops the opened page from reaching back into this one
      // through window.opener.
      href={googleMapsUrl(name, near)}
      target="_blank"
      rel="noopener noreferrer"
      className={`photos-link${className ? ` ${className}` : ""}`}
      aria-label={`Photos of ${name} on Google Maps (opens in a new tab)`}
    >
      📷 Photos
    </a>
  );
}
