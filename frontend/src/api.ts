/**
 * every backend call goes through here. any non-2xx response throws, since
 * fetch only rejects on network errors.
 */

import type {
  AskResponse,
  Bounds,
  Category,
  DestinationDetail,
  GeocodeCandidate,
  NearbyResponse,
  NewDestination,
  NewPlace,
  Place,
  SearchIntent,
  Trip,
} from "./types";

/** relative: vite proxies /api in dev. */
const BASE_URL = "/api";

/** an error carrying the http status. */
export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/**
 * one readable message from a fastapi error body. `detail` is a string for
 * HTTPException and an array of field errors for a 422.
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
    // not json (a proxy error page, an empty 500); use the generic message.
  }
  return `Request failed with status ${response.status}`;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE_URL}${path}`, init);
  } catch {
    // fetch only rejects on network failure; locally that means uvicorn is down.
    throw new ApiError(
      "Could not reach the server. Is the backend running on port 8000?",
      0,
    );
  }

  if (!response.ok) {
    throw new ApiError(await extractErrorMessage(response), response.status);
  }

  // 204 has an empty body, which response.json() would choke on.
  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}

/** GET /api/trips */
export function fetchTrips(): Promise<Trip[]> {
  return request<Trip[]>("/trips");
}

/** GET /api/destinations/{id}/places */
export function fetchPlaces(destinationId: number): Promise<Place[]> {
  return request<Place[]>(`/destinations/${destinationId}/places`);
}

/** POST /api/destinations/{id}/places. returns the saved place with its id. */
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

/** DELETE /api/destinations/{id}/places/{placeId} */
export function deletePlace(
  destinationId: number,
  placeId: number,
): Promise<void> {
  return request<void>(`/destinations/${destinationId}/places/${placeId}`, {
    method: "DELETE",
  });
}

/** GET /api/destinations/{id}/nearby: places or cluster counts for a viewport. */
export function fetchNearby(
  destinationId: number,
  bounds: Bounds,
  categories: readonly Category[],
): Promise<NearbyResponse> {
  // repeated params (?categories=a&categories=b), which fastapi reads as a list.
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
 * POST /api/destinations/{id}/ask. takes a few seconds with a local model.
 * `viewport` is sent only while the map is open.
 */
export function askPina(
  destinationId: number,
  question: string,
  viewport: Bounds | null,
  /** the previous search, so a follow-up refines it. */
  previous: SearchIntent | null = null,
): Promise<AskResponse> {
  return request<AskResponse>(`/destinations/${destinationId}/ask`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, viewport, previous }),
  });
}

/** GET /api/geocode: candidate destinations for a typed name. */
export function geocode(query: string): Promise<GeocodeCandidate[]> {
  const params = new URLSearchParams({ q: query });
  return request<GeocodeCandidate[]>(`/geocode?${params.toString()}`);
}

/**
 * POST /api/trips/{id}/destinations. returns straight away as 'pending'; poll
 * getDestination to follow the sync. sends the picked candidate back in full.
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

/** GET /api/destinations/{id}: destination plus sync progress. */
export function getDestination(
  destinationId: number,
): Promise<DestinationDetail> {
  return request<DestinationDetail>(`/destinations/${destinationId}`);
}

/** DELETE /api/destinations/{id}. downloaded map data is kept. */
export function deleteDestination(destinationId: number): Promise<void> {
  return request<void>(`/destinations/${destinationId}`, { method: "DELETE" });
}

/** POST /api/destinations/{id}/sync: re-run or resume the sync. */
export function startSync(destinationId: number): Promise<DestinationDetail> {
  return request<DestinationDetail>(`/destinations/${destinationId}/sync`, {
    method: "POST",
  });
}
