/**
 * a destination page: ask pina, the save form, and the places/map toggle.
 *
 * owns the `places` array that both views read, so they can't disagree and
 * switching tabs refetches nothing.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { AddPlaceForm } from "./AddPlaceForm";
import { deletePlace, fetchPlaces } from "./api";
import { ASK_PAGE_SIZE, AskPina } from "./AskPina";
import { MapView } from "./MapView";
import { PlaceList } from "./PlaceList";
import type {
  AskResponse,
  Bounds,
  DestinationDetail,
  Place,
  Suggestion,
} from "./types";

interface DestinationPageProps {
  /** full detail: the map needs the bounds and sync state. */
  destination: DestinationDetail;
  /** delete this destination. only called after the user confirms. */
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

  // which place the map should fly to. set by the list, used by the map.
  const [focusedPlaceId, setFocusedPlaceId] = useState<number | null>(null);

  // ask pina's latest answer lives here because the map draws it too.
  // `focusedRef` is the result "show on map" should fly to.
  const [askAnswer, setAskAnswer] = useState<AskResponse | null>(null);
  const [focusedRef, setFocusedRef] = useState<string | null>(null);
  // how many results are shown. the map draws the same slice.
  const [askShown, setAskShown] = useState(ASK_PAGE_SIZE);

  // the map's visible area, for questions that name no area. a ref, since it
  // changes on every pan and nothing needs to re-render.
  const viewport = useRef<Bounds | null>(null);
  const handleViewportChange = useCallback((bounds: Bounds) => {
    viewport.current = bounds;
  }, []);

  // "show on map" also scrolls to the map, which is often below the chat. a
  // counter so asking twice scrolls twice.
  const viewCard = useRef<HTMLDivElement>(null);
  const [scrollRequest, setScrollRequest] = useState(0);
  useEffect(() => {
    if (scrollRequest === 0) {
      return;
    }
    const reduceMotion = window.matchMedia(
      "(prefers-reduced-motion: reduce)",
    ).matches;
    viewCard.current?.scrollIntoView({
      behavior: reduceMotion ? "auto" : "smooth",
      block: "start",
    });
  }, [scrollRequest]);

  /** switch to the map and scroll it into view. */
  function goToMap() {
    setView("map");
    setScrollRequest((count) => count + 1);
  }

  // a different destination starts with no answer.
  useEffect(() => {
    setAskAnswer(null);
    setFocusedRef(null);
  }, [destination.id]);

  // load from the database on mount; nothing is cached in the browser.
  useEffect(() => {
    let active = true;

    async function load() {
      setLoading(true);
      setLoadError(null);
      try {
        const loaded = await fetchPlaces(destination.id);
        // ignore the result if the destination changed or we unmounted.
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

  /** add the saved row returned by the api. */
  function handleSaved(place: Place) {
    setPlaces((current) => [place, ...current]);
  }

  /** delete a place after the user confirmed. state changes only once the api succeeds. */
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

  /** switch to the map and fly to this place, in one render. */
  function handleFocusPlace(placeId: number) {
    setFocusedPlaceId(placeId);
    goToMap();
  }

  function handleAnswer(answer: AskResponse | null) {
    setAskAnswer(answer);
    setFocusedRef(null);
    setAskShown(ASK_PAGE_SIZE);
  }

  /** "show on map" for an ask result. a result saved since then is a saved
   *  pin now, so it's focused like a row in the places list. */
  function handleShowResult(result: Suggestion) {
    const saved = places.find((place) => place.source_ref === result.source_ref);
    if (saved) {
      handleFocusPlace(saved.id);
      return;
    }
    setFocusedRef(result.source_ref);
    goToMap();
  }

  return (
    <section>
      <div className="page__titlerow">
        <h1 className="page__title">{destination.name}</h1>
        <button
          type="button"
          className="linkbutton destination__delete"
          onClick={() => {
            // confirm here, where the number of affected places is known.
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

      <AskPina
        destination={destination}
        places={places}
        answer={askAnswer}
        onAnswer={handleAnswer}
        shown={askShown}
        onShowMore={() => setAskShown((count) => count + ASK_PAGE_SIZE)}
        // only while the map is open; otherwise the whole destination is searched.
        getViewport={() => (view === "map" ? viewport.current : null)}
        onSaved={handleSaved}
        onShowOnMap={handleShowResult}
      />

      <AddPlaceForm destination={destination} onSaved={handleSaved} />

      <div className="card" ref={viewCard}>
        {/* tablist + aria-selected: announced as a view switcher. */}
        <div className="toggle" role="tablist" aria-label="View">
          <button
            type="button"
            role="tab"
            aria-selected={view === "places"}
            className={`toggle__tab${view === "places" ? " toggle__tab--active" : ""}`}
            onClick={() => {
              setView("places");
              setFocusedRef(null);
            }}
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

        {/* a failed load offers a reload; there's no form state to keep. */}
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
              // map saves go into the same state as manual ones.
              onSaved={handleSaved}
              focusedPlaceId={focusedPlaceId}
              askResults={askAnswer?.results.slice(0, askShown) ?? []}
              focusedRef={focusedRef}
              onViewportChange={handleViewportChange}
            />
          ))}
      </div>
    </section>
  );
}
