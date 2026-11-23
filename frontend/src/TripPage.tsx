/**
 * a trip: its destinations, a switcher, and the add form. owns which
 * destination is selected, and that destination's detail (bounds and sync
 * state), which both SyncStatus and DestinationPage need.
 */

import { useCallback, useEffect, useState } from "react";

import { AddDestination } from "./AddDestination";
import { deleteDestination, getDestination } from "./api";
import { DestinationPage } from "./DestinationPage";
import { SyncStatus } from "./SyncStatus";
import type { DestinationDetail, Trip } from "./types";

interface TripPageProps {
  trip: Trip;
}

export function TripPage({ trip }: TripPageProps) {
  // kept locally so adding a destination doesn't refetch the trip.
  const [destinations, setDestinations] = useState(trip.destinations);
  const [selectedId, setSelectedId] = useState<number | null>(
    trip.destinations[0]?.id ?? null,
  );

  // full detail for the selected destination; the trip list only has a summary.
  const [detail, setDetail] = useState<DestinationDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (selectedId === null) {
      setDetail(null);
      return;
    }

    let active = true;
    // clear first, so the previous city's map and banner don't flash.
    setDetail(null);
    setError(null);

    getDestination(selectedId)
      .then((fresh) => {
        if (active) {
          setDetail(fresh);
        }
      })
      .catch((caught: unknown) => {
        if (active) {
          setError(
            caught instanceof Error
              ? caught.message
              : "Could not load this destination.",
          );
        }
      });

    return () => {
      active = false;
    };
  }, [selectedId]);

  // stable, since SyncStatus uses it as an effect dependency.
  const handleDetailUpdate = useCallback((fresh: DestinationDetail) => {
    setDetail(fresh);
    // keep the switcher's copy in step.
    setDestinations((current) =>
      current.map((destination) =>
        destination.id === fresh.id ? { ...destination, ...fresh } : destination,
      ),
    );
  }, []);

  function handleCreated(created: DestinationDetail) {
    setDestinations((current) => [...current, created]);
    // jump to the new city.
    setSelectedId(created.id);
    setDetail(created);
  }

  /** delete after the user confirmed. state changes only once the api succeeds. */
  async function handleDeleteDestination(destinationId: number) {
    try {
      await deleteDestination(destinationId);
    } catch (caught) {
      window.alert(
        caught instanceof Error
          ? caught.message
          : "Could not delete the destination.",
      );
      return;
    }

    const remaining = destinations.filter((d) => d.id !== destinationId);
    setDestinations(remaining);
    // show the next destination, or the empty state.
    setSelectedId(remaining[0]?.id ?? null);
  }

  return (
    <main className="page">
      <header className="page__header">
        {destinations.length > 1 && (
          <nav className="destinations" aria-label="Destinations">
            {destinations.map((destination) => (
              <button
                key={destination.id}
                type="button"
                className={`destchip${
                  destination.id === selectedId ? " destchip--active" : ""
                }`}
                onClick={() => setSelectedId(destination.id)}
                aria-current={destination.id === selectedId}
              >
                {destination.name}
              </button>
            ))}
          </nav>
        )}
      </header>

      {detail !== null && (
        <SyncStatus destination={detail} onUpdate={handleDetailUpdate} />
      )}

      {error !== null && (
        <p className="error" role="alert">
          <strong>Could not load this destination.</strong> {error}
        </p>
      )}

      {detail !== null ? (
        <DestinationPage
          destination={detail}
          onDelete={handleDeleteDestination}
        />
      ) : (
        error === null &&
        selectedId !== null && (
          <p className="muted" role="status">
            Loading {destinations.find((d) => d.id === selectedId)?.name ?? ""}…
          </p>
        )
      )}

      {destinations.length === 0 && (
        <div className="empty" role="status">
          <p className="empty__title">No destinations yet.</p>
          <p className="empty__hint">
            Add a city and Pina will find its restaurants, cafés and sights for
            you.
          </p>
        </div>
      )}

      <div className="page__actions">
        <AddDestination
          tripId={trip.id}
          tripName={trip.name}
          onCreated={handleCreated}
        />
      </div>
    </main>
  );
}
