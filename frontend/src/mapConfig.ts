/**
 * shared map constants for the mini-map and the full map view.
 *
 * openstreetmap raster tiles need no api key. the attribution is required by
 * osm's tile usage policy.
 */

export const OSM_TILE_URL = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png";

export const OSM_ATTRIBUTION =
  '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

/** fallback zoom when a destination has no usable bounds. at 16 the tiles
 *  label shops and restaurants. */
export const CITY_ZOOM = 16;

/** above this zoom, markers show their names as permanent labels. */
export const LABEL_ZOOM = 17;

/** max zoom when fitting to pins, so a single pin isn't zoomed all the way in. */
export const MAX_FIT_ZOOM = 16;

/** debounce before refetching after the map moves, in ms. */
export const MOVE_DEBOUNCE_MS = 300;
