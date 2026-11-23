/**
 * add a city to a trip: search, then pick from the matches. several are
 * shown with their full label, since names like "venice" are ambiguous.
 */

import { useState } from "react";

import { createDestination, geocode } from "./api";
import type { DestinationDetail, GeocodeCandidate } from "./types";

interface AddDestinationProps {
  tripId: number;
  tripName: string;
  onCreated: (destination: DestinationDetail) => void;
}

export function AddDestination({
  tripId,
  tripName,
  onCreated,
}: AddDestinationProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [candidates, setCandidates] = useState<GeocodeCandidate[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [creating, setCreating] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function handleSearch(event: React.FormEvent) {
    event.preventDefault();
    if (query.trim().length === 0 || searching) {
      return;
    }

    setError(null);
    setSearching(true);
    setCandidates(null);
    try {
      setCandidates(await geocode(query.trim()));
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Could not look that up.",
      );
    } finally {
      setSearching(false);
    }
  }

  async function handleChoose(candidate: GeocodeCandidate) {
    setError(null);
    // which row is saving, so only that button shows progress.
    setCreating(candidate.osm_ref);
    try {
      // display_name is only the picker's label; the rest goes back as returned.
      const { display_name: _label, ...newDestination } = candidate;
      const created = await createDestination(tripId, newDestination);

      onCreated(created);
      setOpen(false);
      setQuery("");
      setCandidates(null);
    } catch (caught) {
      // includes the 409 when the city is already on the trip.
      setError(
        caught instanceof Error ? caught.message : "Could not add that.",
      );
    } finally {
      setCreating(null);
    }
  }

  if (!open) {
    return (
      <button
        type="button"
        className="button button--secondary"
        onClick={() => setOpen(true)}
      >
        + Add a destination
      </button>
    );
  }

  return (
    <div className="card adddest">
      <h2 className="form__title">Add a destination to {tripName}</h2>

      <form className="adddest__search" onSubmit={handleSearch}>
        <label className="field field--grow">
          <span className="field__label">City or place</span>
          <input
            className="field__input"
            type="text"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Lisbon"
            autoComplete="off"
            // the panel only opens when the user wants to type.
            autoFocus
          />
        </label>
        <button
          type="submit"
          className="button"
          disabled={query.trim().length === 0 || searching}
        >
          {searching ? "Searching…" : "Search"}
        </button>
      </form>

      {error !== null && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {candidates !== null && candidates.length === 0 && (
        <p className="muted" role="status">
          Nothing found for “{query}”. Try a different spelling, or add the
          country — “Venice, Italy”.
        </p>
      )}

      {candidates !== null && candidates.length > 0 && (
        <ul className="candidates">
          {candidates.map((candidate) => (
            <li key={candidate.osm_ref}>
              <button
                type="button"
                className="candidate"
                onClick={() => handleChoose(candidate)}
                disabled={creating !== null}
              >
                <span className="candidate__name">{candidate.name}</span>
                {/* the full label tells venice, italy from venice, florida. */}
                <span className="candidate__where">
                  {candidate.display_name}
                </span>
                {creating === candidate.osm_ref && (
                  <span className="candidate__adding">Adding…</span>
                )}
              </button>
            </li>
          ))}
        </ul>
      )}

      <button
        type="button"
        className="linkbutton"
        onClick={() => {
          setOpen(false);
          setError(null);
          setCandidates(null);
        }}
      >
        Cancel
      </button>
    </div>
  );
}
