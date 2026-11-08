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

import type { NewPlace, Place, Trip } from "./types";

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
