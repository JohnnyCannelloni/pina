/**
 * the osm suggestion markers and the popup you save from.
 *
 * saving uses the same createPlace as the form, plus source and source_ref.
 * one component per marker, so each popup keeps its own saving/error state.
 */

import { useState } from "react";
import type L from "leaflet";
import { Marker, Popup, Tooltip } from "react-leaflet";

import { createPlace } from "./api";
import { suggestionIcon } from "./leafletIcons";
import { PlaceInfo } from "./PlaceInfo";
import { CATEGORY_LABELS, type Place, type Suggestion } from "./types";

interface SuggestionMarkersProps {
  suggestions: Suggestion[];
  destinationId: number;
  destinationName: string;
  onSaved: (place: Place) => void;
  /** zoomed in far enough for labels not to overlap. */
  showLabels: boolean;
  /** ask pina results: bigger, highlighted and always labelled. */
  highlighted?: boolean;
  /** hands each marker up by source_ref so "show on map" can find it. called
   *  with null on unmount. */
  onMarker?: (sourceRef: string, marker: L.Marker | null) => void;
}

function SuggestionMarker({
  suggestion,
  destinationId,
  destinationName,
  onSaved,
  showLabels,
  highlighted,
  onMarker,
}: {
  suggestion: Suggestion;
  destinationId: number;
  destinationName: string;
  onSaved: (place: Place) => void;
  showLabels: boolean;
  highlighted: boolean;
  onMarker?: (sourceRef: string, marker: L.Marker | null) => void;
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
        // coordinates come from osm.
        latitude: suggestion.latitude,
        longitude: suggestion.longitude,
        // marks it as an osm place, so the map knows it's saved next time.
        source: "osm",
        source_ref: suggestion.source_ref,
      });

      // DestinationPage prepends it; MapView then drops this suggestion.
      onSaved(saved);
    } catch (caught) {
      // includes the 409 when it was saved elsewhere meanwhile. the popup
      // stays open, so the must-go choice isn't lost.
      setError(caught instanceof Error ? caught.message : "Could not save.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Marker
      position={[suggestion.latitude, suggestion.longitude]}
      icon={suggestionIcon(suggestion.category, highlighted)}
      // above ordinary suggestions.
      zIndexOffset={highlighted ? 500 : 0}
      ref={(instance) => onMarker?.(suggestion.source_ref, instance)}
    >
      {/* permanent name label when zoomed in (hover tooltips don't work on
          phones). non-interactive, so clicks reach the marker. */}
      {(showLabels || highlighted) && (
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
        <PlaceInfo
          name={suggestion.name}
          near={destinationName}
          latitude={suggestion.latitude}
          longitude={suggestion.longitude}
          details={suggestion.details}
        />

        {/* the category is shown because the osm mapping is sometimes off. */}
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
  highlighted = false,
  onMarker,
}: SuggestionMarkersProps) {
  return (
    <>
      {suggestions.map((suggestion) => (
        // keyed by source_ref, so per-marker state follows the right place.
        <SuggestionMarker
          key={suggestion.source_ref}
          suggestion={suggestion}
          destinationId={destinationId}
          destinationName={destinationName}
          onSaved={onSaved}
          showLabels={showLabels}
          highlighted={highlighted}
          onMarker={onMarker}
        />
      ))}
    </>
  );
}
