/**
 * Loads the trip, then renders its destination page.
 *
 * This indirection exists so that no destination id and no coordinate is
 * hardcoded in the frontend. The app asks the API what trips exist, and the API
 * answers with Italy, Venice, and Venice's position. Adding a second city
 * becomes a change to seed.py, not to this file.
 *
 * There is no router: one trip, one destination, one page. When there are two
 * destinations this grows a list and react-router earns its place — not before.
 */

import { useEffect, useState } from "react";

import { fetchTrips } from "./api";
import { DestinationPage } from "./DestinationPage";
import type { Trip } from "./types";

export function App() {
  const [trip, setTrip] = useState<Trip | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;

    async function load() {
      try {
        const trips = await fetchTrips();
        if (!active) {
          return;
        }
        // trips[0] is `Trip | undefined` because of noUncheckedIndexedAccess in
        // tsconfig — which is the point of that flag. An unseeded database is a
        // real state, and it gets a real message below instead of a crash.
        setTrip(trips[0] ?? null);
      } catch (caught) {
        if (active) {
          setError(
            caught instanceof Error ? caught.message : "Could not load trips.",
          );
        }
      } finally {
        if (active) {
          setLoading(false);
        }
      }
    }

    void load();
    return () => {
      active = false;
    };
  }, []);

  if (loading) {
    return (
      <main className="page">
        <p className="muted" role="status">
          Loading…
        </p>
      </main>
    );
  }

  if (error !== null) {
    return (
      <main className="page">
        <p className="error" role="alert">
          <strong>Could not load your trip.</strong> {error}
        </p>
      </main>
    );
  }

  const destination = trip?.destinations[0];

  // Reached when the migration has run but `python -m app.seed` has not. Naming
  // the exact command is more useful than "no data found".
  if (!trip || !destination) {
    return (
      <main className="page">
        <div className="empty" role="status">
          <p className="empty__title">No trips yet.</p>
          <p className="empty__hint">
            Seed the database from the backend directory:{" "}
            <code>python -m app.seed</code>
          </p>
        </div>
      </main>
    );
  }

  return <DestinationPage destination={destination} tripName={trip.name} />;
}
