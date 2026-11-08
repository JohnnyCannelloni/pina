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
  /** ISO 8601 string; JSON has no date type. */
  created_at: string;
}

/** A destination. Its coordinates are where the map opens. */
export interface Destination {
  id: number;
  name: string;
  latitude: number;
  longitude: number;
}

export interface Trip {
  id: number;
  name: string;
  destinations: Destination[];
}

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
