/**
 * api data shapes. field names match the json, the pydantic schemas
 * (backend/app/schemas.py) and the database columns, hence snake_case.
 */

/** matches Category in models.py. */
export type Category = "restaurant" | "cafe" | "sight";

/** every category, in display order. */
export const ALL_CATEGORIES: readonly Category[] = [
  "restaurant",
  "cafe",
  "sight",
];

/** where a saved place came from. matches PlaceSource in models.py. */
export type PlaceSource = "manual" | "osm";

/** features you can ask for. matches Feature in schemas.py. */
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

/** what osm says about a place. any field may be missing. */
export interface PlaceDetails {
  /** osm spellings, e.g. ["italian", "coffee_shop"]. see formatCuisine. */
  cuisine: string[];
  opening_hours: string | null;
  website: string | null;
  phone: string | null;
  street: string | null;
  features: Feature[];
}

/** a saved place. latitude/longitude are null when it has no pin. */
export interface Place {
  id: number;
  destination_id: number;
  name: string;
  category: Category;
  must_go: boolean;
  /** null together with longitude. */
  latitude: number | null;
  longitude: number | null;
  source: PlaceSource;
  /** osm element id ("node/123") if saved from the map, else null. */
  source_ref: string | null;
  /** iso 8601 string. */
  created_at: string;
  /** from the osm cache for map-saved places; null for manual ones. */
  details: PlaceDetails | null;
}

/**
 * an osm place you could save. it has no id; source_ref is its identity and
 * becomes Place.source_ref when saved.
 */
export interface Suggestion {
  source_ref: string;
  name: string;
  category: Category;
  /** always set, unlike a Place. */
  latitude: number;
  longitude: number;
  already_saved: boolean;
  details: PlaceDetails | null;
}

/** one grid cell of a zoomed-out view, positioned at its places' average. */
export interface Cluster {
  latitude: number;
  longitude: number;
  count: number;
}

/** whether imported data covers the viewport, so an empty map can say why. */
export type Coverage = "covered" | "partial" | "missing";

/** the map's response for one viewport. `mode` says which list is filled. */
export interface NearbyResponse {
  mode: "places" | "clusters";
  /** matching places in view. in cluster mode the counts sum to this. */
  total: number;
  coverage: Coverage;
  places: Suggestion[];
  clusters: Cluster[];
}

/** what the llm understood a question to mean, as a search. */
export interface SearchIntent {
  category: Category | null;
  cuisines: string[];
  features: Feature[];
  name_words: string[];
  area: string | null;
}

/** matches AskResponse in schemas.py. */
export interface AskResponse {
  /** the reply sentence, written by the server from the results. */
  reply: string;
  interpretation: SearchIntent;
  /** e.g. "near Alfama", "in the map view". */
  area_label: string;
  /** false when the named area couldn't be found and the search widened. */
  area_found: boolean;
  total: number;
  results: Suggestion[];
  /** requested features nothing matched; the search ran without them. */
  dropped_features: Feature[];
}

/** a map rectangle. matches BoundingBox in schemas.py. */
export interface Bounds {
  south: number;
  west: number;
  north: number;
  east: number;
}

/** state of a destination's place cache. matches SyncState in models.py. */
export type SyncState = "pending" | "syncing" | "ready" | "failed";

/** a destination: where it is, its area, and whether its places arrived. */
export interface Destination {
  id: number;
  name: string;
  latitude: number;
  longitude: number;
  /** the stored area; the map opens on these bounds. */
  south: number;
  west: number;
  north: number;
  east: number;
  sync_state: SyncState;
  sync_error: string | null;
  synced_at: string | null;
}

/** state of a region's extract. download and import are separate steps. */
export type ExtractState =
  | "pending"
  | "downloading"
  | "importing"
  | "ready"
  | "failed";

/** destination plus cache status, polled while places load. */
export interface DestinationDetail extends Destination {
  osm_place_count: number;
  /** geofabrik region, e.g. "portugal". null when no region covers it. */
  region: string | null;
  extract_state: ExtractState | null;
}

export interface Trip {
  id: number;
  name: string;
  destinations: Destination[];
}

/** a geocoder candidate, before the user picks one. */
export interface GeocodeCandidate {
  /** e.g. "Venice, Veneto, Italy". */
  display_name: string;
  /** e.g. "Venice". */
  name: string;
  latitude: number;
  longitude: number;
  south: number;
  west: number;
  north: number;
  east: number;
  osm_ref: string;
  /** iso 3166-1 alpha-2. */
  country_code: string | null;
}

/** POST body for a new destination: a candidate without its label. */
export type NewDestination = Omit<GeocodeCandidate, "display_name">;

/** POST body for a new place. id, created_at and destination_id aren't sent. */
export interface NewPlace {
  name: string;
  category: Category;
  must_go: boolean;
  latitude: number | null;
  longitude: number | null;
  /** omitted by the manual form; "osm" plus source_ref for a map save. */
  source?: PlaceSource;
  source_ref?: string;
}

/** category labels for the dropdown, list and popups. */
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

/** "italian_pizza" -> "Italian pizza". */
export function formatCuisine(cuisine: string): string {
  const words = cuisine.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}
