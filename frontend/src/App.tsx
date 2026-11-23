/**
 * loads the trips and renders the first one. ids and coordinates come from the
 * api, so nothing is hardcoded here.
 */

import { useEffect, useState } from "react";

import { fetchTrips } from "./api";
import { TripPage } from "./TripPage";
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
        // possibly undefined (noUncheckedIndexedAccess): an unseeded database
        // gets a message below instead of a crash.
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

  // migrated but not seeded.
  if (!trip) {
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

  // a trip with no destinations is handled by TripPage.
  return <TripPage trip={trip} />;
}
