/**
 * shows whether a destination's places have arrived, and polls while they
 * arrive. never blocks manual saves, which write to `places` independently.
 */

import { useEffect, useState } from "react";

import { getDestination, startSync } from "./api";
import type { DestinationDetail } from "./types";

interface SyncStatusProps {
  destination: DestinationDetail;
  /** called with fresh data whenever polling or a retry returns. */
  onUpdate: (destination: DestinationDetail) => void;
}

/** how often to re-check while a sync is running. */
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
        // the component may have unmounted (switched destination mid-sync).
        if (active) {
          onUpdate(fresh);
        }
      } catch {
        // best-effort; the next tick tries again.
      }
    }, POLL_MS);

    return () => {
      active = false;
      window.clearInterval(timer);
    };
    // re-subscribes when the running state flips, which stops the interval.
  }, [destination.id, isRunning, onUpdate]);

  async function handleRetry() {
    setRetrying(true);
    try {
      onUpdate(await startSync(destination.id));
    } catch {
      // keep the failed state visible; the button can be pressed again.
    } finally {
      setRetrying(false);
    }
  }

  if (destination.sync_state === "ready") {
    // quiet once synced.
    return null;
  }

  if (isRunning) {
    // the extract's state tells a long download from a short import.
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
      {/* no retry without a region: there's no map data to fetch. */}
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
