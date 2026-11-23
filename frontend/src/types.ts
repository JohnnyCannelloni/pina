/**
 * The API's data shapes, mirrored in TypeScript.
 *
 * These field names are identical to the JSON keys, which are identical to the
 * Pydantic schema fields, which are identical to the Postgres column names:
 * `must_go` is `must_go` everywhere. Snake_case looks slightly un-idiomatic in
 * TypeScript, and that is the trade — a single name you can grep from this file
 * to the migration, with no mapping layer in between to get out of step.
 *
 * Backend counterpart: backend/app/schemas.py
 */

/** Matches the Category enum in models.py and ck_places_category in 0001. */
export type Category = "restaurant" | "cafe" | "sight";

/** Every category, in display order. One array so the filter chips, the form
 *  dropdown, and the default search all agree. */
export const ALL_CATEGORIES: readonly Category[] = [
  "restaurant",
  "cafe",
  "sight",
];

/**
 * Where a saved place came from. Matches PlaceSource in models.py and
 * ck_places_source in migration 0002.
 */
export type PlaceSource = "manual" | "osm";

/** Things you can ask a place to have. Matches Feature in schemas.py. */
export type Feature =
  | "outdoor_seating"
  | "vegetarian"
  | "vegan"
  | "gluten_free"
  | "wheelchair"
  | "takeaway"
  | "delivery";

export const FEATURE_LABELS: Record<Feature, string> = {
  outdoor_seating: "Outdoor seating",
  vegetarian: "Vegetarian",
  vegan: "Vegan",
  gluten_free: "Gluten-free",
  wheelchair: "Wheelchair access",
  takeaway: "Takeaway",
  delivery: "Delivery",
};

/** What OpenStreetMap says about a place. Every field may be missing — OSM
 *  coverage is patchy. Matches PlaceDetails in schemas.py. */
export interface PlaceDetails {
  /** OSM spellings, e.g. ["italian", "coffee_shop"]. See formatCuisine. */
  cuisine: string[];
  opening_hours: string | null;
  website: string | null;
  phone: string | null;
  street: string | null;
  features: Feature[];
}

/** A saved place. `latitude`/`longitude` are null when it has no pin. */
export interface Place {
  id: number;
  destination_id: number;
  name: string;
  category: Category;
  must_go: boolean;
  /** Null together with longitude — see ck_places_coords_both_or_neither. */
  latitude: number | null;
  longitude: number | null;
  source: PlaceSource;
  /** The OpenStreetMap element id ("node/123") if saved from the map, else null. */
  source_ref: string | null;
  /** ISO 8601 string; JSON has no date type. */
  created_at: string;
  /** From the OSM cache for places saved from the map; null for manual ones. */
  details: PlaceDetails | null;
}

/**
 * A place from OpenStreetMap that you could save — a candidate, not a record.
 *
 * Note there is no `id`: suggestions are never stored, so they have no primary
 * key. `source_ref` is their identity, and it becomes Place.source_ref if you
 * save one. Backend counterpart: the Suggestion schema in schemas.py.
 */
export interface Suggestion {
  source_ref: string;
  name: string;
  category: Category;
  /** Never null, unlike a Place — a suggestion always has a position. */
  latitude: number;
  longitude: number;
  already_saved: boolean;
  details: PlaceDetails | null;
}

/** One grid cell of a zoomed-out view: how many places, and where they are.
 *
 *  The coordinates are the *average* position of the places in the cell, not
 *  the cell's centre, so a bubble sits on the places rather than on a grid
 *  line. Matches Cluster in schemas.py. */
export interface Cluster {
  latitude: number;
  longitude: number;
  count: number;
}

/** Whether the server has imported data for the requested viewport.
 *
 *  Exists because an empty list is ambiguous: a map with no markers looks the
 *  same whether the area is genuinely empty or was never imported. Saying which
 *  is the difference between "nothing here" and "nothing *yet*". */
export type Coverage = "covered" | "partial" | "missing";

/** What the map gets back for one viewport.
 *
 *  `mode` says which list to read; the other is empty. The server decides from
 *  the actual count, not from a zoom level we send — density varies far more
 *  than zoom does. Matches NearbyResponse in schemas.py. */
export interface NearbyResponse {
  mode: "places" | "clusters";
  /** Total matching places in view, whichever mode. In cluster mode the counts
   *  sum to this. */
  total: number;
  coverage: Coverage;
  places: Suggestion[];
  clusters: Cluster[];
}

/** What the local LLM understood a question to mean. It never names a place;
 *  this is a search. Matches SearchIntent in schemas.py. */
export interface SearchIntent {
  category: Category | null;
  cuisines: string[];
  features: Feature[];
  name_words: string[];
  area: string | null;
}

/** Matches AskResponse in schemas.py. */
export interface AskResponse {
  interpretation: SearchIntent;
  /** "near Alfama", "in the map view", "in Lisboa". */
  area_label: string;
  /** False when the named area could not be found and the search widened. */
  area_found: boolean;
  total: number;
  results: Suggestion[];
  /** Asked-for features nothing here had; the search ran without them. */
  dropped_features: Feature[];
}

/** The map rectangle to search inside. Matches BoundingBox in schemas.py. */
export interface Bounds {
  south: number;
  west: number;
  north: number;
  east: number;
}

/**
 * How a destination's OpenStreetMap place cache is doing.
 * Matches SyncState in models.py.
 */
export type SyncState = "pending" | "syncing" | "ready" | "failed";

/** A destination: where it is, how big it is, and whether its places arrived. */
export interface Destination {
  id: number;
  name: string;
  latitude: number;
  longitude: number;
  /** The stored area. The map opens on these bounds, so a town and a city
   *  both frame correctly instead of sharing one arbitrary zoom. */
  south: number;
  west: number;
  north: number;
  east: number;
  sync_state: SyncState;
  sync_error: string | null;
  synced_at: string | null;
}

/**
 * How a region's OpenStreetMap extract is doing. Matches ExtractState in
 * models.py. Downloading and importing are separate because they are very
 * different waits — minutes of network versus seconds of parsing.
 */
export type ExtractState =
  | "pending"
  | "downloading"
  | "importing"
  | "ready"
  | "failed";

/** Destination plus cache status — what the UI polls while places load. */
export interface DestinationDetail extends Destination {
  osm_place_count: number;
  /** Geofabrik region, e.g. "europe/portugal". Null when we have no map data
   *  for this country; the destination still works for manual saves. */
  region: string | null;
  extract_state: ExtractState | null;
}

export interface Trip {
  id: number;
  name: string;
  destinations: Destination[];
}

/** A candidate destination from the geocoder, before you pick one. */
export interface GeocodeCandidate {
  /** "Venice, Veneto, Italy" — the full label, so Venice Italy and Venice
   *  Florida are distinguishable. */
  display_name: string;
  /** "Venice" — what the destination gets called. */
  name: string;
  latitude: number;
  longitude: number;
  south: number;
  west: number;
  north: number;
  east: number;
  osm_ref: string;
  /** ISO 3166-1 alpha-2. Decides which OSM extract gets downloaded. */
  country_code: string | null;
}

/** The POST body for creating a destination: a candidate, minus its label. */
export type NewDestination = Omit<GeocodeCandidate, "display_name">;

/**
 * The POST body. Mirrors PlaceCreate: no `id`, no `created_at` (the database
 * owns those) and no `destination_id` (it comes from the URL path).
 */
export interface NewPlace {
  name: string;
  category: Category;
  must_go: boolean;
  latitude: number | null;
  longitude: number | null;
  /**
   * Omitted by the manual form (the backend defaults it to "manual"); set to
   * "osm" with a source_ref when saving a map suggestion. The backend rejects
   * a mismatch between the two — see ck_places_source_ref_present.
   */
  source?: PlaceSource;
  source_ref?: string;
}

/** For the category dropdown, and for rendering a label in the list and popup. */
export const CATEGORY_LABELS: Record<Category, string> = {
  restaurant: "Restaurant",
  cafe: "Café",
  sight: "Sight",
};

export const CATEGORY_ICONS: Record<Category, string> = {
  restaurant: "🍝",
  cafe: "☕",
  sight: "🏛️",
};

/** "italian_pizza" -> "Italian pizza". OSM's spellings are lowercase with
 *  underscores; this is the only place that knows how to show them. */
export function formatCuisine(cuisine: string): string {
  const words = cuisine.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}
