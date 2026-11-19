/**
 * The OpenStreetMap suggestion layer, and the popup you save from.
 *
 * READ THIS LAST, after the backend. The thing worth noticing is how little is
 * here: `handleSave` calls the *same* `createPlace` as the main form, with two
 * extra fields. There is no second save path, no separate endpoint, and no
 * "import from OSM" machinery — saving a suggestion is an ordinary save that
 * happens to carry a `source_ref`.
 *
 * Each popup owns its own saving/error state, which is why this is a component
 * per marker rather than one shared popup: two popups can never be open at
 * once in Leaflet, but a failed save on one must not leave an error attached to
 * the next one you click.
 */

import { useState } from "react";
import { Marker, Popup, Tooltip } from "react-leaflet";

import { createPlace } from "./api";
import { suggestionIcon } from "./leafletIcons";
import { CATEGORY_LABELS, type Place, type Suggestion } from "./types";

interface SuggestionMarkersProps {
  suggestions: Suggestion[];
  destinationId: number;
  destinationName: string;
  onSaved: (place: Place) => void;
  /** True when zoomed in far enough that names will not overlap into mush. */
  showLabels: boolean;
}

function SuggestionMarker({
  suggestion,
  destinationId,
  destinationName,
  onSaved,
  showLabels,
}: {
  suggestion: Suggestion;
  destinationId: number;
  destinationName: string;
  onSaved: (place: Place) => void;
  showLabels: boolean;
}) {
  const [mustGo, setMustGo] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSave() {
    setError(null);
    setSaving(true);
    try {
      const saved = await createPlace(destinationId, {
        name: suggestion.name,
        category: suggestion.category,
        must_go: mustGo,
        // The coordinates come from OpenStreetMap, so there is no pin to place
        // — which is the whole point of suggestions.
        latitude: suggestion.latitude,
        longitude: suggestion.longitude,
        // These two are the only difference from a manual save. The backend
        // requires them together (ck_places_source_ref_present) and uses
        // source_ref to recognise this place next time you search.
        source: "osm",
        source_ref: suggestion.source_ref,
      });

      // Hands the saved row up to DestinationPage, which prepends it to the
      // shared `places` array. This marker then disappears on the next render,
      // because MapView filters out suggestions whose source_ref is saved —
      // derived, not synced.
      onSaved(saved);
    } catch (caught) {
      // Includes the 409 when this place is already saved, which can happen if
      // it was saved in another tab since this search ran. The popup stays
      // open with the message, so the Must-go choice is not lost.
      setError(caught instanceof Error ? caught.message : "Could not save.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Marker
      position={[suggestion.latitude, suggestion.longitude]}
      icon={suggestionIcon(suggestion.category)}
    >
      {/* The name, drawn on the map itself when zoomed in. This is most of what
          made the map feel uninformative before: a field of identical circles
          tells you nothing until you have clicked every one.

          `permanent` means it shows without hovering — a hover tooltip is no
          use on a phone. `interactive={false}` lets clicks fall through the
          label to the marker underneath, so a label never blocks its own
          popup. */}
      {showLabels && (
        <Tooltip
          permanent
          direction="right"
          offset={[12, 0]}
          className="marker-label"
          interactive={false}
        >
          {suggestion.name}
        </Tooltip>
      )}

      <Popup>
        <span className="popup__name">{suggestion.name}</span>
        <span className="popup__meta">
          {CATEGORY_LABELS[suggestion.category]} · from OpenStreetMap
        </span>

        {/* The detected category is shown before you save, rather than being
            applied silently, because the OSM tag we mapped from is sometimes
            approximate — a gelateria arrives tagged as a café. */}
        <label className="popup__checkbox">
          <input
            type="checkbox"
            checked={mustGo}
            onChange={(event) => setMustGo(event.target.checked)}
            disabled={saving}
          />
          <span>Must-go</span>
        </label>

        {error !== null && (
          <span className="popup__error" role="alert">
            {error}
          </span>
        )}

        <button
          type="button"
          className="popup__save"
          onClick={handleSave}
          disabled={saving}
        >
          {saving ? "Saving…" : `Save to ${destinationName}`}
        </button>
      </Popup>
    </Marker>
  );
}

export function SuggestionMarkers({
  suggestions,
  destinationId,
  destinationName,
  onSaved,
  showLabels,
}: SuggestionMarkersProps) {
  return (
    <>
      {suggestions.map((suggestion) => (
        // Keyed by source_ref, the only stable identity a suggestion has —
        // suggestions have no database id. Keying by array index would make
        // React reuse one marker's saving/error state for a different place
        // after a new search reorders the list.
        <SuggestionMarker
          key={suggestion.source_ref}
          suggestion={suggestion}
          destinationId={destinationId}
          destinationName={destinationName}
          onSaved={onSaved}
          showLabels={showLabels}
        />
      ))}
    </>
  );
}
