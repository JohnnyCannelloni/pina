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
import { deletePlace, fetchPlaces } from "./api";
import { MapView } from "./MapView";
import { PlaceList } from "./PlaceList";
import type { DestinationDetail, Place } from "./types";

interface DestinationPageProps {
  /** The full detail, not the summary: the map needs the stored bounds and the
   *  Map view needs sync_state to word its empty message correctly. */
  destination: DestinationDetail;
  /** Deletes this destination. Called only after the user has confirmed. */
  onDelete: (destinationId: number) => void;
}

type View = "places" | "map";

export function DestinationPage({
  destination,
  onDelete,
}: DestinationPageProps) {
  const [places, setPlaces] = useState<Place[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [view, setView] = useState<View>("places");

  // Which place the map should fly to. Lives here, not in MapView, because it
  // is set by the *list* and consumed by the *map* — the nearest common parent
  // is the only place both can reach.
  const [focusedPlaceId, setFocusedPlaceId] = useState<number | null>(null);

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

  /**
   * Called by PlaceList once the user has confirmed a delete.
   *
   * Removed from state only after the API succeeds, so a failed delete cannot
   * make a place vanish from the screen while it is still in the database.
   * Both views read this one array, so the list row and the map pin go
   * together.
   */
  async function handleDeletePlace(placeId: number) {
    try {
      await deletePlace(destination.id, placeId);
      setPlaces((current) => current.filter((place) => place.id !== placeId));
      if (focusedPlaceId === placeId) {
        setFocusedPlaceId(null);
      }
    } catch (caught) {
      window.alert(
        caught instanceof Error ? caught.message : "Could not delete the place.",
      );
    }
  }

  /**
   * Called by PlaceList when a pinned place is clicked.
   *
   * Switching view and setting the target are one action, so they are one
   * function. React batches both updates into a single render, so the map
   * mounts already knowing where to fly.
   */
  function handleFocusPlace(placeId: number) {
    setFocusedPlaceId(placeId);
    setView("map");
  }

  return (
    <section>
      <div className="page__titlerow">
        <h1 className="page__title">{destination.name}</h1>
        <button
          type="button"
          className="linkbutton destination__delete"
          onClick={() => {
            // The confirm is here because this component knows how many
            // places would go with it — the number worth warning about.
            const count = places.length;
            const also =
              count > 0
                ? ` This also deletes ${count} saved place${count === 1 ? "" : "s"}.`
                : "";
            if (window.confirm(`Delete ${destination.name}?${also}`)) {
              onDelete(destination.id);
            }
          }}
        >
          Delete destination
        </button>
      </div>

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
            <PlaceList
              places={places}
              destinationName={destination.name}
              onFocusPlace={handleFocusPlace}
              onDeletePlace={handleDeletePlace}
            />
          ) : (
            <MapView
              places={places}
              destination={destination}
              onShowList={() => setView("places")}
              // Suggestions saved from a map popup go into the same state as
              // a manual save, which is what makes the saved marker replace
              // the suggestion with no extra wiring.
              onSaved={handleSaved}
              focusedPlaceId={focusedPlaceId}
            />
          ))}
      </div>
    </section>
  );
}
