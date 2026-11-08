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

/** Close enough to see streets and canals, wide enough to hold central Venice. */
export const CITY_ZOOM = 14;

/** Used when fitting bounds to existing pins, so a single pin is not zoomed to
 *  maximum detail. */
export const MAX_FIT_ZOOM = 16;
