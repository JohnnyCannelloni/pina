/**
 * Every call to the backend goes through here.
 *
 * The one rule this file enforces: a non-2xx response **throws**. `fetch` does
 * not do that on its own — it only rejects on a network failure, so a 422 or a
 * 500 arrives as a perfectly ordinary resolved promise with `ok: false`. Code
 * that forgets to check `response.ok` therefore treats a failed save as a
 * success, which is precisely the bug that makes a form silently lose data.
 *
 * Throwing means every caller must either handle the failure or let it bubble,
 * and the component can use one try/catch for both "the API said no" and "the
 * API is not running".
 */

import type {
  Bounds,
  Category,
  DestinationDetail,
  GeocodeCandidate,
  NearbyResponse,
  NewDestination,
  NewPlace,
  Place,
  Trip,
} from "./types";

/** Relative, not absolute: Vite proxies /api to localhost:8000 in dev, and the
 *  same path works in production when API and static files share a host. */
const BASE_URL = "/api";

/** An error carrying the HTTP status, so callers can distinguish 404 from 500. */
export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/**
 * Turn a FastAPI error body into one human-readable sentence.
 *
 * FastAPI uses `detail` for both of its error styles, with different types:
 *   - HTTPException  -> detail is a string:  "Destination 9 not found"
 *   - validation 422 -> detail is an array:  [{loc: ["body","name"], msg: ...}]
 * Handling only the first gives the user "[object Object]" the first time they
 * submit something invalid.
 */
async function extractErrorMessage(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json();
    if (body && typeof body === "object" && "detail" in body) {
      const detail = (body as { detail: unknown }).detail;

      if (typeof detail === "string") {
        return detail;
      }

      if (Array.isArray(detail)) {
        const messages = detail
          .map((item) => {
            if (item && typeof item === "object" && "msg" in item) {
              const { msg, loc } = item as { msg: unknown; loc?: unknown };
              // loc is like ["body", "name"]; the last entry is the field.
              const field = Array.isArray(loc) ? loc[loc.length - 1] : undefined;
              return field && field !== "body" ? `${field}: ${msg}` : String(msg);
            }
            return null;
          })
          .filter((msg): msg is string => msg !== null);

        if (messages.length > 0) {
          return messages.join("; ");
        }
      }
    }
  } catch {
    // Body was not JSON (a proxy error page, an empty 500). Fall through to the
    // generic message below rather than masking the real status with a crash.
  }
  return `Request failed with status ${response.status}`;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE_URL}${path}`, init);
  } catch {
    // fetch rejects only here: DNS failure, connection refused, offline. In
    // practice while developing, this means uvicorn is not running — so say so,
    // rather than showing the browser's opaque "Failed to fetch".
    throw new ApiError(
      "Could not reach the server. Is the backend running on port 8000?",
      0,
    );
  }

  if (!response.ok) {
    throw new ApiError(await extractErrorMessage(response), response.status);
  }

  // 204 No Content (a DELETE) has an empty body, and response.json() throws
  // on an empty body — which would turn a successful delete into an error.
  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}

/** GET /api/trips — how the UI discovers Venice's id and map centre. */
export function fetchTrips(): Promise<Trip[]> {
  return request<Trip[]>("/trips");
}

/** GET /api/destinations/{id}/places — the single read both views share. */
export function fetchPlaces(destinationId: number): Promise<Place[]> {
  return request<Place[]>(`/destinations/${destinationId}/places`);
}

/**
 * POST /api/destinations/{id}/places — the save.
 *
 * Returns the created place, including the `id` and `created_at` Postgres
 * generated, so the caller can put the real record straight into state.
 */
export function createPlace(
  destinationId: number,
  place: NewPlace,
): Promise<Place> {
  return request<Place>(`/destinations/${destinationId}/places`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(place),
  });
}

/**
 * DELETE /api/destinations/{id}/places/{placeId} — remove a saved place.
 *
 * Only your collection changes; a place saved from the map becomes a
 * suggestion again.
 */
export function deletePlace(
  destinationId: number,
  placeId: number,
): Promise<void> {
  return request<void>(`/destinations/${destinationId}/places/${placeId}`, {
    method: "DELETE",
  });
}

/**
 * GET /api/destinations/{id}/nearby — what to draw for this viewport.
 *
 * A read of our own indexed table, not a third-party call, which is why it can
 * run on every map movement. Returns either real places or cluster counts
 * depending on how many are in view — see NearbyResponse.
 */
export function fetchNearby(
  destinationId: number,
  bounds: Bounds,
  categories: readonly Category[],
): Promise<NearbyResponse> {
  // URLSearchParams handles the encoding, and `append` called repeatedly is how
  // you build the ?categories=restaurant&categories=cafe form that FastAPI
  // parses into a list. A comma-joined single value would arrive as one string
  // named "restaurant,cafe" and fail validation.
  const params = new URLSearchParams({
    south: String(bounds.south),
    west: String(bounds.west),
    north: String(bounds.north),
    east: String(bounds.east),
  });
  for (const category of categories) {
    params.append("categories", category);
  }

  return request<NearbyResponse>(
    `/destinations/${destinationId}/nearby?${params.toString()}`,
  );
}

/**
 * GET /api/geocode — candidate destinations for a typed name.
 *
 * Several are returned on purpose: "Venice" exists in Italy, Florida and
 * California, so the user picks. This is the one place a third party is called
 * during an interaction, and it is cheap — one lookup when someone adds a city.
 */
export function geocode(query: string): Promise<GeocodeCandidate[]> {
  const params = new URLSearchParams({ q: query });
  return request<GeocodeCandidate[]>(`/geocode?${params.toString()}`);
}

/**
 * POST /api/trips/{id}/destinations — add a destination and start its sync.
 *
 * Returns immediately with sync_state 'pending'; fetching its places happens in
 * the background. Poll getDestination to follow it.
 *
 * The chosen candidate is sent back in full rather than just its name, so the
 * destination is built from exactly the place the user picked — re-geocoding
 * server-side could silently resolve to a different one.
 */
export function createDestination(
  tripId: number,
  destination: NewDestination,
): Promise<DestinationDetail> {
  return request<DestinationDetail>(`/trips/${tripId}/destinations`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(destination),
  });
}

/** GET /api/destinations/{id} — destination plus sync progress. */
export function getDestination(
  destinationId: number,
): Promise<DestinationDetail> {
  return request<DestinationDetail>(`/destinations/${destinationId}`);
}

/**
 * DELETE /api/destinations/{id} — remove a destination and its saved places.
 *
 * The downloaded map data is kept, so re-adding the city fills it instantly.
 */
export function deleteDestination(destinationId: number): Promise<void> {
  return request<void>(`/destinations/${destinationId}`, { method: "DELETE" });
}

/**
 * POST /api/destinations/{id}/sync — re-run or resume the place sync.
 *
 * Safe at any time: finished areas are skipped and re-fetched places are
 * updated rather than duplicated. Your saved places are never touched.
 */
export function startSync(destinationId: number): Promise<DestinationDetail> {
  return request<DestinationDetail>(`/destinations/${destinationId}/sync`, {
    method: "POST",
  });
}
