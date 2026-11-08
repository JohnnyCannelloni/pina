/**
 * The save form. READ THIS THIRD — `handleSubmit` is where a save begins.
 *
 * The whole trace, end to end:
 *
 *   handleSubmit (below)
 *     -> createPlace()                      api.ts
 *     -> POST /api/destinations/1/places    proxied by Vite to port 8000
 *     -> create_place()                     backend/app/routes.py
 *     -> PlaceCreate validates              backend/app/schemas.py
 *     -> Place(...) + db.commit()            backend/app/models.py, db.py
 *     -> INSERT INTO places ...              the table from 0001_initial.py
 *     -> PlaceRead serialises the saved row
 *     -> onSaved(place) hands it to DestinationPage, which prepends it to state
 *
 * The pin is not special in any of that. It is two more fields in the same form
 * state, in the same request body, written by the same INSERT.
 */

import { useState } from "react";

import { createPlace } from "./api";
import { LocationPicker, type Coordinates } from "./LocationPicker";
import {
  CATEGORY_LABELS,
  type Category,
  type Destination,
  type Place,
} from "./types";

interface AddPlaceFormProps {
  destination: Destination;
  /** Called with the saved place, so the parent can add it to its list. */
  onSaved: (place: Place) => void;
}

const CATEGORIES: Category[] = ["restaurant", "cafe", "sight"];

export function AddPlaceForm({ destination, onSaved }: AddPlaceFormProps) {
  // One piece of state per field. No form library — with four fields it would
  // be more to learn than to write.
  const [name, setName] = useState("");
  const [category, setCategory] = useState<Category>("restaurant");
  const [mustGo, setMustGo] = useState(false);
  // null means "no pin", which is a valid place. Not undefined, and not
  // {lat: 0, lng: 0} — a real coordinate off the coast of Africa is exactly the
  // kind of placeholder that ends up in the database.
  const [pin, setPin] = useState<Coordinates | null>(null);

  const [showPicker, setShowPicker] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Mirrors the backend's rule (a name is required after trimming) so the
  // button is disabled rather than producing a 422 the user has to read.
  const canSave = name.trim().length > 0 && !saving;

  async function handleSubmit(event: React.FormEvent) {
    // Without this the browser does a full page navigation on submit — the form
    // appears to clear itself and the save never happens.
    event.preventDefault();

    if (!canSave) {
      return;
    }

    // Clear any previous failure, and lock the button so a double-click or an
    // Enter keypress cannot save the same place twice.
    setError(null);
    setSaving(true);

    try {
      const saved = await createPlace(destination.id, {
        name: name.trim(),
        category,
        must_go: mustGo,
        // Both or neither — the same invariant the database enforces with
        // ck_places_coords_both_or_neither. Spreading from one nullable object
        // makes it impossible to send half a pin from here.
        latitude: pin?.latitude ?? null,
        longitude: pin?.longitude ?? null,
      });

      // Only reached if api.ts did not throw, i.e. the row is committed. The
      // response is the saved record with its real database id, so state can be
      // updated without refetching.
      onSaved(saved);

      // Reset only on success. On failure the fields keep their values (see
      // below), so a retry does not mean retyping.
      setName("");
      setCategory("restaurant");
      setMustGo(false);
      setPin(null);
      setShowPicker(false);
    } catch (caught) {
      // api.ts throws on any non-2xx *and* on an unreachable server, so this
      // one branch covers a validation error, a 404, a 500, and uvicorn being
      // stopped. Nothing above ran, so nothing was saved — and crucially the
      // form state is untouched, so the user's typing and their pin survive.
      setError(caught instanceof Error ? caught.message : "Could not save.");
    } finally {
      // finally, not at the end of try: on the error path the button must be
      // re-enabled too, or a failed save locks the form forever.
      setSaving(false);
    }
  }

  return (
    <form className="card form" onSubmit={handleSubmit} noValidate>
      <h2 className="form__title">Add a place</h2>

      <div className="form__row">
        <label className="field field--grow">
          <span className="field__label">Name</span>
          <input
            className="field__input"
            type="text"
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="Trattoria Rosa"
            maxLength={200}
            autoComplete="off"
          />
        </label>

        <label className="field">
          <span className="field__label">Category</span>
          <select
            className="field__input"
            value={category}
            onChange={(event) => setCategory(event.target.value as Category)}
          >
            {CATEGORIES.map((value) => (
              <option key={value} value={value}>
                {CATEGORY_LABELS[value]}
              </option>
            ))}
          </select>
        </label>
      </div>

      <label className="checkbox">
        <input
          type="checkbox"
          checked={mustGo}
          onChange={(event) => setMustGo(event.target.checked)}
        />
        <span>Must-go</span>
      </label>

      {/* Collapsed by default: a pin is optional, and an always-open map would
          push the Save button off a phone screen. */}
      <div className="picker__wrap">
        <button
          type="button"
          className="disclosure"
          onClick={() => setShowPicker((open) => !open)}
          aria-expanded={showPicker}
        >
          <span aria-hidden="true">{showPicker ? "▾" : "▸"}</span>
          {pin === null ? "Drop a pin (optional)" : "Pin set"}
        </button>

        {showPicker && (
          <>
            <LocationPicker
              center={destination}
              value={pin}
              onChange={setPin}
            />
            <div className="picker__status">
              {pin === null ? (
                <span className="picker__hint">
                  Tap the map to place a pin. Drag it to adjust.
                </span>
              ) : (
                <>
                  {/* Rounded for display only — the full precision value stays
                      in state and is what gets sent and stored. */}
                  <span className="picker__coords">
                    {pin.latitude.toFixed(5)}, {pin.longitude.toFixed(5)}
                  </span>
                  <button
                    type="button"
                    className="linkbutton"
                    onClick={() => setPin(null)}
                  >
                    Clear pin
                  </button>
                </>
              )}
            </div>
          </>
        )}
      </div>

      {/* role="alert" makes a screen reader announce the failure immediately,
          rather than leaving it as text the user has to go looking for. */}
      {error !== null && (
        <p className="error" role="alert">
          <strong>Could not save.</strong> {error}
          <span className="error__hint">
            Your details are still here — press Save to try again.
          </span>
        </p>
      )}

      <button type="submit" className="button" disabled={!canSave}>
        {saving ? "Saving…" : "Save place"}
      </button>
    </form>
  );
}
