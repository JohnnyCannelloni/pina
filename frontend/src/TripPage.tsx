/**
 * The trip: its destinations, a switcher between them, and the add form.
 *
 * This sits above DestinationPage and owns "which destination am I looking
 * at?". It exists because destinations are now things you create rather than a
 * single seeded row — Venice is just the first one.
 *
 * It also owns the *selected* destination's detail (bounds and sync progress),
 * because two children need it: SyncStatus renders it, and DestinationPage
 * passes its bounds to the map.
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
  // The destination list, kept locally so adding one does not need a trip
  // refetch. Seeded from the trip the App already loaded.
  const [destinations, setDestinations] = useState(trip.destinations);
  const [selectedId, setSelectedId] = useState<number | null>(
    trip.destinations[0]?.id ?? null,
  );

  // Full detail for the selected destination. The trip list only carries the
  // summary, and the map needs bounds plus sync state.
  const [detail, setDetail] = useState<DestinationDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (selectedId === null) {
      setDetail(null);
      return;
    }

    let active = true;
    // Clear immediately, so switching destinations cannot briefly show the
    // previous city's map bounds and sync banner.
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

  // useCallback because SyncStatus takes this in a useEffect dependency list;
  // a fresh function each render would restart its polling interval every tick.
  const handleDetailUpdate = useCallback((fresh: DestinationDetail) => {
    setDetail(fresh);
    // Keep the switcher's copy in step, so its label reflects sync state too.
    setDestinations((current) =>
      current.map((destination) =>
        destination.id === fresh.id ? { ...destination, ...fresh } : destination,
      ),
    );
  }, []);

  function handleCreated(created: DestinationDetail) {
    setDestinations((current) => [...current, created]);
    // Jump straight to the new city — adding it is a statement of intent to
    // look at it.
    setSelectedId(created.id);
    setDetail(created);
  }

  /**
   * Called by DestinationPage once the user has confirmed.
   *
   * Lives here, not in DestinationPage, because this component owns the
   * destination list and the selection — both change when one is deleted.
   * State is updated only after the API succeeds, so a failed delete cannot
   * hide a destination that still exists.
   */
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
    // Show the next destination, or the "No destinations yet" state when that
    // was the last one. Changing selectedId re-runs the detail effect above.
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
