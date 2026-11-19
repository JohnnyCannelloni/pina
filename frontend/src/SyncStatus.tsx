/**
 * Shows whether a destination's places have arrived yet, and polls while they
 * are arriving.
 *
 * This component exists because of how the previous version failed. Discovery
 * depended on a service that was usually busy, and said nothing about it — so
 * an empty map was indistinguishable from a broken one. A feature built on a
 * flaky dependency has to narrate itself: what it is doing, how far it got, and
 * what you can do about it.
 *
 * Note what it never does: block anything. Saving places by hand works
 * throughout, because the form writes to `places` and the sync only fills
 * `osm_places`.
 */

import { useEffect, useState } from "react";

import { getDestination, startSync } from "./api";
import type { DestinationDetail } from "./types";

interface SyncStatusProps {
  destination: DestinationDetail;
  /** Called with fresh data whenever polling or a retry returns. */
  onUpdate: (destination: DestinationDetail) => void;
}

/** How often to re-check while a sync is running. */
const POLL_MS = 2000;

export function SyncStatus({ destination, onUpdate }: SyncStatusProps) {
  const [retrying, setRetrying] = useState(false);

  const isRunning =
    destination.sync_state === "syncing" || destination.sync_state === "pending";

  useEffect(() => {
    if (!isRunning) {
      return;
    }

    let active = true;
    const timer = window.setInterval(async () => {
      try {
        const fresh = await getDestination(destination.id);
        // The guard matters: an interval callback can fire after the component
        // unmounts (switching destination mid-sync), and setting state then is
        // both pointless and a warning in the console.
        if (active) {
          onUpdate(fresh);
        }
      } catch {
        // Polling is best-effort. A failed poll is not worth an error banner —
        // the next tick will try again, and the user is not waiting on it.
      }
    }, POLL_MS);

    return () => {
      active = false;
      window.clearInterval(timer);
    };
    // Re-subscribes when the running state flips, which is what stops the
    // interval once the sync reaches ready or failed.
  }, [destination.id, isRunning, onUpdate]);

  async function handleRetry() {
    setRetrying(true);
    try {
      onUpdate(await startSync(destination.id));
    } catch {
      // Leave the failed state visible; the button can simply be pressed again.
    } finally {
      setRetrying(false);
    }
  }

  if (destination.sync_state === "ready") {
    // Deliberately quiet on success. A permanent green banner saying "synced"
    // is noise once it is the normal state.
    return null;
  }

  if (isRunning) {
    // The extract's own state is more informative than the destination's,
    // because downloading and importing are very different waits: a country's
    // map data is a few hundred megabytes, and parsing it afterwards takes
    // seconds. One "loading" message for both would leave the user unable to
    // tell a slow download from a stuck parse.
    const detail =
      destination.extract_state === "downloading"
        ? "downloading map data for the country — this happens once"
        : destination.extract_state === "importing"
          ? "reading the map data"
          : "getting ready";

    return (
      <p className="sync sync--running" role="status">
        <span className="sync__spinner" aria-hidden="true" />
        Finding places in {destination.name} — {detail}…
        <span className="sync__hint">
          You can keep adding places by hand while this runs.
          {destination.region && ` (${destination.region})`}
        </span>
      </p>
    );
  }

  // failed
  return (
    <p className="sync sync--failed" role="alert">
      <strong>Some places could not be loaded.</strong>{" "}
      {destination.sync_error ?? "OpenStreetMap may be busy."}
      <span className="sync__hint">
        {destination.osm_place_count > 0
          ? `${destination.osm_place_count} places did load, and the map works with those.`
          : "Your own saved places are unaffected."}
      </span>
      {/* No Retry when there is no region: the problem is that Pina has no map
          data for this country, which pressing a button cannot fix. Offering
          one would just fail again. */}
      {destination.region !== null && (
        <button
          type="button"
          className="linkbutton"
          onClick={handleRetry}
          disabled={retrying}
        >
          {retrying ? "Retrying…" : "Retry"}
        </button>
      )}
    </p>
  );
}
