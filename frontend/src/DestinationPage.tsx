/**
 * The destination page: the form, the Places/Map toggle, and whichever view is
 * selected.
 *
 * This component owns the `places` array, and that is the important structural
 * decision. Both views are handed the *same* array, so they cannot disagree,
 * and switching tabs refetches nothing. Had each view fetched its own copy, the
 * list and the map could show different data after a save — the classic bug
 * this shape avoids.
 */

import { useEffect, useState } from "react";

import { AddPlaceForm } from "./AddPlaceForm";
import { fetchPlaces } from "./api";
import { MapView } from "./MapView";
import { PlaceList } from "./PlaceList";
import type { Destination, Place } from "./types";

interface DestinationPageProps {
  destination: Destination;
  tripName: string;
}

type View = "places" | "map";

export function DestinationPage({
  destination,
  tripName,
}: DestinationPageProps) {
  const [places, setPlaces] = useState<Place[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [view, setView] = useState<View>("places");

  // This effect is why a refresh keeps your places: on mount, state starts
  // empty and is filled from the database. Nothing is cached in the browser —
  // what you see after F5 is what Postgres holds.
  useEffect(() => {
    let active = true;

    async function load() {
      setLoading(true);
      setLoadError(null);
      try {
        const loaded = await fetchPlaces(destination.id);
        // The guard: if the component unmounted (or destination changed) while
        // this request was in flight, dropping the result avoids setting state
        // on a dead component and avoids a slow response overwriting a fast one.
        if (active) {
          setPlaces(loaded);
        }
      } catch (caught) {
        if (active) {
          setLoadError(
            caught instanceof Error ? caught.message : "Could not load places.",
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
  }, [destination.id]);

  /**
   * Called by AddPlaceForm with the row the API just returned.
   *
   * Prepending the real saved record — rather than optimistically inserting a
   * guess and reconciling later — means the id, created_at, and coordinates in
   * state are exactly what is in the database. The map can therefore key off
   * `place.id` and trust the position.
   */
  function handleSaved(place: Place) {
    setPlaces((current) => [place, ...current]);
  }

  return (
    <main className="page">
      <header className="page__header">
        <p className="page__trip">{tripName}</p>
        <h1 className="page__title">{destination.name}</h1>
      </header>

      <AddPlaceForm destination={destination} onSaved={handleSaved} />

      <div className="card">
        {/* role="tablist" + aria-selected tells a screen reader this is a view
            switcher, not two unrelated buttons. */}
        <div className="toggle" role="tablist" aria-label="View">
          <button
            type="button"
            role="tab"
            aria-selected={view === "places"}
            className={`toggle__tab${view === "places" ? " toggle__tab--active" : ""}`}
            onClick={() => setView("places")}
          >
            Places{places.length > 0 && ` (${places.length})`}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={view === "map"}
            className={`toggle__tab${view === "map" ? " toggle__tab--active" : ""}`}
            onClick={() => setView("map")}
          >
            Map
          </button>
        </div>

        {loading && (
          <p className="muted" role="status">
            Loading places…
          </p>
        )}

        {/* A failed *load* is reported separately from a failed *save*: this one
            is not the user's fault and has no form state to preserve, so it
            offers a reload instead. */}
        {!loading && loadError !== null && (
          <p className="error" role="alert">
            <strong>Could not load places.</strong> {loadError}
            <button
              type="button"
              className="linkbutton"
              onClick={() => window.location.reload()}
            >
              Try again
            </button>
          </p>
        )}

        {!loading &&
          loadError === null &&
          (view === "places" ? (
            <PlaceList places={places} destinationName={destination.name} />
          ) : (
            <MapView
              places={places}
              destination={destination}
              onShowList={() => setView("places")}
            />
          ))}
      </div>
    </main>
  );
}
