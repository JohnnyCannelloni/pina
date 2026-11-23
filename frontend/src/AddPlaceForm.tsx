/**
 * the save form. the pin is just two more fields in the same request.
 *
 *   handleSubmit -> createPlace() (api.ts) -> POST /api/destinations/{id}/places
 *   -> onSaved(place), which DestinationPage prepends to its state
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
  /** called with the saved place, so the parent can add it. */
  onSaved: (place: Place) => void;
}

const CATEGORIES: Category[] = ["restaurant", "cafe", "sight"];

export function AddPlaceForm({ destination, onSaved }: AddPlaceFormProps) {
  // one piece of state per field; four fields don't need a form library.
  const [name, setName] = useState("");
  const [category, setCategory] = useState<Category>("restaurant");
  const [mustGo, setMustGo] = useState(false);
  // null means no pin (not {0, 0}, which is a real coordinate).
  const [pin, setPin] = useState<Coordinates | null>(null);

  const [showPicker, setShowPicker] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // matches the backend rule, so the button is disabled instead of a 422.
  const canSave = name.trim().length > 0 && !saving;

  async function handleSubmit(event: React.FormEvent) {
    // stop the browser's own form submit.
    event.preventDefault();

    if (!canSave) {
      return;
    }

    // lock the button so a double click can't save twice.
    setError(null);
    setSaving(true);

    try {
      const saved = await createPlace(destination.id, {
        name: name.trim(),
        category,
        must_go: mustGo,
        // both coordinates or neither.
        latitude: pin?.latitude ?? null,
        longitude: pin?.longitude ?? null,
      });

      // the response is the saved row with its real id.
      onSaved(saved);

      // reset only on success, so a failed save keeps what was typed.
      setName("");
      setCategory("restaurant");
      setMustGo(false);
      setPin(null);
      setShowPicker(false);
    } catch (caught) {
      // covers validation errors, 404/500 and an unreachable server. the form
      // state is untouched, so the user's input survives.
      setError(caught instanceof Error ? caught.message : "Could not save.");
    } finally {
      // in finally, so a failed save doesn't lock the form.
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

      {/* collapsed by default so the save button stays on screen on a phone. */}
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
                  {/* rounded for display only; full precision is sent. */}
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

      {/* role="alert" so screen readers announce it. */}
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
