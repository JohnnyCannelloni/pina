/**
 * Shared map constants, so the form's mini-map and the full map view cannot
 * drift apart.
 *
 * OpenStreetMap raster tiles need no API key, no account, and no billing —
 * unlike Mapbox or Google Maps, and unlike the vector-tile hosts MapLibre
 * usually points at. For a single-user app running on localhost that is the
 * whole decision.
 *
 * The attribution string is not decorative: OSM's tile usage policy requires
 * visible credit. Leaflet renders it in the corner automatically.
 */

export const OSM_TILE_URL = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png";

export const OSM_ATTRIBUTION =
  '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

/**
 * Fallback zoom when a destination has no usable bounds.
 *
 * Raised from 14 to 16. At 14 the OSM basemap shows almost no place labels, so
 * the map was a grey street diagram — part of why it read as uninformative. 16
 * is close enough that the tiles themselves name shops and restaurants, before
 * any of our markers are drawn.
 *
 * Normally unused: the map now opens on the destination's stored bounding box,
 * so a small town and a big city both frame correctly instead of sharing one
 * arbitrary zoom.
 */
export const CITY_ZOOM = 16;

/**
 * Above this zoom, markers show their names as permanent labels.
 *
 * Below it a dense city would be unreadable overlapping text; above it, being
 * able to read "Trattoria alla Madonna" without clicking is most of what
 * "informative" means.
 */
export const LABEL_ZOOM = 17;

/** Used when fitting bounds to existing pins, so a single pin is not zoomed to
 *  maximum detail. */
export const MAX_FIT_ZOOM = 16;

/**
 * There is deliberately no MIN_SEARCH_ZOOM any more.
 *
 * The previous version gated searching at zoom 15 while the map opened at zoom
 * 14 — so the Search button was disabled the instant you opened the Map tab,
 * and the whole discovery feature appeared not to exist. Two constants in this
 * file, never compared with each other.
 *
 * The gate existed to protect Overpass from large queries. Markers now come
 * from our own `osm_places` table via an indexed query, so there is nothing to
 * protect and no button to press: the map just loads what is in view. Deleting
 * the constant is the fix — a gate that cannot disagree with the opening zoom
 * is one that cannot be set wrong.
 *
 * How many markers one view may return is now capped server-side by
 * MAX_SUGGESTIONS in routes.py, which is where a rendering limit belongs.
 */

/** Debounce before refetching after the map moves, in milliseconds.
 *
 *  The fetch is a local database read, so this is not about protecting a
 *  service — it is about not issuing twenty requests during one drag. `moveend`
 *  already waits for the gesture to settle; this absorbs rapid successive pans.
 */
export const MOVE_DEBOUNCE_MS = 300;
