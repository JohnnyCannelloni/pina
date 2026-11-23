/**
 * Ask Pina: a question in plain words, real places back.
 *
 * READ THIS AFTER backend/app/llm.py and app/ask.py. The model only turns the
 * question into a search, so everything shown here is a real OpenStreetMap
 * place. The chips under the box are the model's interpretation, shown
 * deliberately: a small local model will sometimes misread a question, and
 * when it does you should be able to see that, not just get odd results.
 *
 * The answer is owned by DestinationPage, not by this component, because the
 * map draws it too — the same lifting-state-up as `places`.
 */

import { useEffect, useState, type FormEvent } from "react";

import { askPina, createPlace } from "./api";
import { PlaceInfo } from "./PlaceInfo";
import {
  CATEGORY_ICONS,
  CATEGORY_LABELS,
  FEATURE_LABELS,
  formatCuisine,
  type AskResponse,
  type Bounds,
  type DestinationDetail,
  type Place,
  type Suggestion,
} from "./types";

/** Results shown at first, and added per "Show more". Five is what fits
 *  beside a map without scrolling, and the list is closest-first, so the top
 *  few are the ones that matter. */
export const ASK_PAGE_SIZE = 5;

interface AskPinaProps {
  destination: DestinationDetail;
  places: Place[];
  answer: AskResponse | null;
  onAnswer: (answer: AskResponse | null) => void;
  /** How many results to show; owned by DestinationPage, which gives the map
   *  the same slice. */
  shown: number;
  onShowMore: () => void;
  /** The map's current view, or null when the map is not open. */
  getViewport: () => Bounds | null;
  onSaved: (place: Place) => void;
  onShowOnMap: (result: Suggestion) => void;
}

/** One ask is a few seconds; these give the box something to suggest. */
const EXAMPLES = [
  "Italian with outdoor seating",
  "a vegan café",
  "museums",
];

export function AskPina({
  destination,
  places,
  answer,
  onAnswer,
  shown,
  onShowMore,
  getViewport,
  onSaved,
  onShowOnMap,
}: AskPinaProps) {
  const [question, setQuestion] = useState("");
  const [asking, setAsking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Derived from `places`, like the map's: saving a result marks it saved
  // here with no extra state.
  const savedRefs = new Set(
    places.map((p) => p.source_ref).filter((ref): ref is string => ref !== null),
  );

  async function ask(text: string) {
    const trimmed = text.trim();
    if (!trimmed || asking) {
      return;
    }
    setAsking(true);
    setError(null);
    try {
      onAnswer(await askPina(destination.id, trimmed, getViewport()));
    } catch (caught) {
      // The 503 carries the command that fixes it ("brew services start
      // ollama"), so the server's message is shown as-is.
      setError(caught instanceof Error ? caught.message : "Could not ask.");
    } finally {
      setAsking(false);
    }
  }

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    void ask(question);
  }

  return (
    <div className="card ask">
      <form className="ask__form" onSubmit={handleSubmit}>
        <label className="sr-only" htmlFor="ask-question">
          Ask Pina
        </label>
        <input
          id="ask-question"
          className="field__input ask__input"
          type="text"
          value={question}
          maxLength={300}
          placeholder={`Ask Pina — e.g. "pizza with a terrace near the centre"`}
          onChange={(event) => setQuestion(event.target.value)}
          disabled={asking}
        />
        <button
          type="submit"
          className="button"
          disabled={asking || !question.trim()}
        >
          {asking ? "Thinking…" : "Ask"}
        </button>
      </form>

      {!answer && !asking && error === null && (
        <p className="ask__examples">
          Try:{" "}
          {EXAMPLES.map((example) => (
            <button
              key={example}
              type="button"
              className="chip"
              onClick={() => {
                setQuestion(example);
                void ask(example);
              }}
            >
              {example}
            </button>
          ))}
        </p>
      )}

      {asking && (
        <p className="muted" role="status">
          Reading your question… The model runs on this computer, so this takes
          a few seconds — a bit longer for the first question.
        </p>
      )}

      {error !== null && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {answer && !asking && (
        <AskAnswer
          answer={answer}
          destinationId={destination.id}
          destinationName={destination.name}
          savedRefs={savedRefs}
          shown={shown}
          onShowMore={onShowMore}
          onSaved={onSaved}
          onShowOnMap={onShowOnMap}
          onClear={() => onAnswer(null)}
        />
      )}
    </div>
  );
}

function AskAnswer({
  answer,
  destinationId,
  destinationName,
  savedRefs,
  shown,
  onShowMore,
  onSaved,
  onShowOnMap,
  onClear,
}: {
  answer: AskResponse;
  destinationId: number;
  destinationName: string;
  savedRefs: Set<string>;
  shown: number;
  onShowMore: () => void;
  onSaved: (place: Place) => void;
  onShowOnMap: (result: Suggestion) => void;
  onClear: () => void;
}) {
  const { interpretation: intent } = answer;
  // One row open at a time: details for one place are useful, details for
  // five at once are the wall of text this replaced.
  const [expandedRef, setExpandedRef] = useState<string | null>(null);
  useEffect(() => setExpandedRef(null), [answer]);

  const visible = answer.results.slice(0, shown);
  const more = Math.min(ASK_PAGE_SIZE, answer.results.length - visible.length);

  return (
    <div className="ask__answer">
      {/* What the model understood. Chips, not prose, so a wrong guess is
          spotted at a glance. */}
      <div className="ask__chips" aria-label="What Pina understood">
        {intent.category && (
          <span className="chip chip--on">
            {CATEGORY_ICONS[intent.category]} {CATEGORY_LABELS[intent.category]}
          </span>
        )}
        {intent.cuisines.map((cuisine) => (
          <span key={cuisine} className="chip chip--on">
            {formatCuisine(cuisine)}
          </span>
        ))}
        {intent.features.map((feature) => (
          <span
            key={feature}
            className={`chip ${
              answer.dropped_features.includes(feature)
                ? "ask__chip--dropped"
                : "chip--on"
            }`}
          >
            {FEATURE_LABELS[feature]}
          </span>
        ))}
        <span className="chip chip--on">📍 {answer.area_label}</span>
        <button type="button" className="linkbutton" onClick={onClear}>
          Clear
        </button>
      </div>

      {intent.name_words.length > 0 && (
        <p className="ask__note">
          Also looking for names containing: {intent.name_words.join(", ")}
        </p>
      )}
      {!answer.area_found && intent.area && (
        <p className="ask__note">
          Couldn't find “{intent.area}” on the map, so searched{" "}
          {answer.area_label} instead.
        </p>
      )}
      {answer.dropped_features.length > 0 && (
        <p className="ask__note">
          Nothing here is recorded with{" "}
          {answer.dropped_features.map((f) => FEATURE_LABELS[f]).join(" + ")},
          so these are shown without it. OpenStreetMap only records that for
          some places — check the Photos.
        </p>
      )}

      {answer.results.length === 0 ? (
        <p className="empty__hint">
          No places match that {answer.area_label}. Try asking for less, or a
          different area.
        </p>
      ) : (
        <>
          <p className="muted">
            {visible.length < answer.total
              ? `Showing ${visible.length} of ${answer.total} matches`
              : `${answer.total} match${answer.total === 1 ? "" : "es"}`}
            , closest first.
          </p>
          <ul className="places">
            {visible.map((result) => (
              <AskResult
                key={result.source_ref}
                result={result}
                destinationId={destinationId}
                destinationName={destinationName}
                saved={savedRefs.has(result.source_ref)}
                expanded={expandedRef === result.source_ref}
                onToggle={() =>
                  setExpandedRef((current) =>
                    current === result.source_ref ? null : result.source_ref,
                  )
                }
                onSaved={onSaved}
                onShowOnMap={onShowOnMap}
              />
            ))}
          </ul>
          {/* Only as far as the results already fetched — no new request.
              The server sends the 25 closest; past that, a narrower question
              beats a longer list. */}
          {more > 0 && (
            <button
              type="button"
              className="button button--secondary ask__more"
              onClick={onShowMore}
            >
              Show {more} more
            </button>
          )}
        </>
      )}
    </div>
  );
}

/** The one-line summary under a result's name: what it is, its cuisine, and
 *  a couple of features. The rest waits until the row is opened. */
function summary(result: Suggestion): string {
  const parts: string[] = [CATEGORY_LABELS[result.category]];
  const details = result.details;
  if (details) {
    if (details.cuisine.length > 0) {
      parts.push(details.cuisine.slice(0, 2).map(formatCuisine).join(", "));
    }
    parts.push(...details.features.slice(0, 2).map((f) => FEATURE_LABELS[f]));
  }
  return parts.join(" · ");
}

/** One result row: compact until clicked. Owns its own saving state, like a
 *  map popup does, so a failed save on one row cannot leave an error on
 *  another. */
function AskResult({
  result,
  destinationId,
  destinationName,
  saved,
  expanded,
  onToggle,
  onSaved,
  onShowOnMap,
}: {
  result: Suggestion;
  destinationId: number;
  destinationName: string;
  saved: boolean;
  expanded: boolean;
  onToggle: () => void;
  onSaved: (place: Place) => void;
  onShowOnMap: (result: Suggestion) => void;
}) {
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSave() {
    setSaving(true);
    setError(null);
    try {
      // The same save as the map popup's — an ordinary save carrying a
      // source_ref. There is no separate "save from Ask" path.
      onSaved(
        await createPlace(destinationId, {
          name: result.name,
          category: result.category,
          must_go: false,
          latitude: result.latitude,
          longitude: result.longitude,
          source: "osm",
          source_ref: result.source_ref,
        }),
      );
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not save.");
    } finally {
      setSaving(false);
    }
  }

  const detailsId = `ask-details-${result.source_ref.replace("/", "-")}`;

  return (
    <li className={`ask__row${expanded ? " ask__row--open" : ""}`}>
      <div className="ask__rowhead">
        {/* The head is the toggle; Save is its sibling, not inside it — a
            button inside a button is invalid HTML. */}
        <button
          type="button"
          className="ask__toggle"
          aria-expanded={expanded}
          aria-controls={detailsId}
          onClick={onToggle}
        >
          <span className="place__icon" aria-hidden="true">
            {CATEGORY_ICONS[result.category]}
          </span>
          <span className="place__body">
            <span className="place__name">{result.name}</span>
            <span className="place__meta">{summary(result)}</span>
          </span>
          <span className="ask__chevron" aria-hidden="true">
            {expanded ? "▴" : "▾"}
          </span>
        </button>
        {saved ? (
          <span className="ask__saved">✓ Saved</span>
        ) : (
          <button
            type="button"
            className="linkbutton ask__save"
            onClick={handleSave}
            disabled={saving}
          >
            {saving ? "Saving…" : "Save"}
          </button>
        )}
      </div>

      {error !== null && (
        <span className="popup__error" role="alert">
          {error}
        </span>
      )}

      {expanded && (
        <div className="ask__details" id={detailsId}>
          <PlaceInfo
            name={result.name}
            near={destinationName}
            latitude={result.latitude}
            longitude={result.longitude}
            details={result.details}
            withoutSummary
          />
          <button
            type="button"
            className="linkbutton"
            onClick={() => onShowOnMap(result)}
          >
            Show on map
          </button>
        </div>
      )}
    </li>
  );
}
