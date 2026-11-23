/**
 * ask pina as a conversation: ask, get real places, follow up.
 *
 * each message becomes a search on the server; results are always real osm
 * places. follow-ups send the previous search so it's refined, not restarted.
 * the reply sentence comes from the server (ask.describe).
 *
 * the latest answer lives in DestinationPage, because the map draws it too.
 * the conversation lives here.
 */

import { useEffect, useRef, useState, type FormEvent } from "react";

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
  type SearchIntent,
  type Suggestion,
} from "./types";

/** results shown at first and per "show more". */
export const ASK_PAGE_SIZE = 5;

interface AskPinaProps {
  destination: DestinationDetail;
  places: Place[];
  /** the latest answer, whose results are listed and pinned. */
  answer: AskResponse | null;
  onAnswer: (answer: AskResponse | null) => void;
  /** how many results to show; the map gets the same slice. */
  shown: number;
  onShowMore: () => void;
  /** the map's current view, or null when the map isn't open. */
  getViewport: () => Bounds | null;
  onSaved: (place: Place) => void;
  onShowOnMap: (result: Suggestion) => void;
}

/** one exchange. `answer` and `error` are both null while pina is thinking. */
interface Turn {
  id: number;
  question: string;
  answer: AskResponse | null;
  error: string | null;
}

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
  const [turns, setTurns] = useState<Turn[]>([]);
  const [question, setQuestion] = useState("");
  const nextId = useRef(1);
  const log = useRef<HTMLOListElement>(null);

  const asking = turns.some((turn) => turn.answer === null && turn.error === null);

  // a different destination is a different conversation.
  useEffect(() => {
    setTurns([]);
  }, [destination.id]);

  // keep the newest message in view by scrolling the log, not the page.
  useEffect(() => {
    const element = log.current;
    if (element) {
      element.scrollTop = element.scrollHeight;
    }
  }, [turns]);

  // derived from `places`, so a save shows up here with no extra state.
  const savedRefs = new Set(
    places.map((p) => p.source_ref).filter((ref): ref is string => ref !== null),
  );

  /** the search a follow-up refines: the latest one with an answer. */
  function previousSearch(): SearchIntent | null {
    for (let i = turns.length - 1; i >= 0; i--) {
      const turnAnswer = turns[i]?.answer;
      if (turnAnswer) {
        return turnAnswer.interpretation;
      }
    }
    return null;
  }

  async function ask(text: string) {
    const trimmed = text.trim();
    if (!trimmed || asking) {
      return;
    }
    const id = nextId.current++;
    const previous = previousSearch();
    setTurns((current) => [
      ...current,
      { id, question: trimmed, answer: null, error: null },
    ]);
    setQuestion("");

    const settle = (update: Partial<Turn>) =>
      setTurns((current) =>
        current.map((turn) => (turn.id === id ? { ...turn, ...update } : turn)),
      );

    try {
      const response = await askPina(
        destination.id,
        trimmed,
        getViewport(),
        previous,
      );
      settle({ answer: response });
      onAnswer(response);
    } catch (caught) {
      // the 503 message includes the fix, so show it as-is.
      settle({
        error: caught instanceof Error ? caught.message : "Could not ask.",
      });
    }
  }

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    void ask(question);
  }

  function handleNewChat() {
    setTurns([]);
    onAnswer(null);
  }

  return (
    <div className="card chat">
      <div className="chat__header">
        <h2 className="chat__title">Ask Pina</h2>
        {turns.length > 0 && (
          <button
            type="button"
            className="linkbutton"
            onClick={handleNewChat}
            disabled={asking}
          >
            New chat
          </button>
        )}
      </div>

      {turns.length === 0 ? (
        <div className="chat__intro">
          <p className="muted">
            Ask for places in {destination.name} in your own words, then
            follow up to narrow it down — “only ones with a terrace”, “what
            about another area?”
          </p>
          <p className="ask__examples">
            Try:{" "}
            {EXAMPLES.map((example) => (
              <button
                key={example}
                type="button"
                className="chip"
                onClick={() => void ask(example)}
              >
                {example}
              </button>
            ))}
          </p>
        </div>
      ) : (
        // aria-live: replies are announced as they arrive.
        <ol className="chat__log" ref={log} aria-live="polite">
          {turns.map((turn) => (
            <li key={turn.id} className="chat__turn">
              <p className="chat__bubble chat__bubble--user">{turn.question}</p>
              <PinaMessage
                turn={turn}
                // only the latest answer lists results; they're the ones on the map.
                current={turn.answer !== null && turn.answer === answer}
                destinationId={destination.id}
                destinationName={destination.name}
                savedRefs={savedRefs}
                shown={shown}
                onShowMore={onShowMore}
                onSaved={onSaved}
                onShowOnMap={onShowOnMap}
              />
            </li>
          ))}
        </ol>
      )}

      <form className="ask__form chat__composer" onSubmit={handleSubmit}>
        <label className="sr-only" htmlFor="ask-question">
          Message Pina
        </label>
        <input
          id="ask-question"
          className="field__input ask__input"
          type="text"
          value={question}
          maxLength={300}
          placeholder={
            previousSearch()
              ? "Follow up — e.g. “only vegan ones”"
              : "e.g. “pizza with a terrace near the centre”"
          }
          onChange={(event) => setQuestion(event.target.value)}
          disabled={asking}
        />
        <button
          type="submit"
          className="button"
          disabled={asking || !question.trim()}
        >
          Send
        </button>
      </form>
    </div>
  );
}

function PinaMessage({
  turn,
  current,
  destinationId,
  destinationName,
  savedRefs,
  shown,
  onShowMore,
  onSaved,
  onShowOnMap,
}: {
  turn: Turn;
  current: boolean;
  destinationId: number;
  destinationName: string;
  savedRefs: Set<string>;
  shown: number;
  onShowMore: () => void;
  onSaved: (place: Place) => void;
  onShowOnMap: (result: Suggestion) => void;
}) {
  if (turn.error !== null) {
    return (
      <p className="chat__bubble chat__bubble--pina chat__bubble--error" role="alert">
        {turn.error}
      </p>
    );
  }

  if (turn.answer === null) {
    return (
      <p className="chat__bubble chat__bubble--pina chat__thinking" role="status">
        <span className="chat__dots" aria-hidden="true">
          <span />
          <span />
          <span />
        </span>
        Thinking… (a few seconds — the model runs on this computer)
      </p>
    );
  }

  const answer = turn.answer;
  return (
    <div className="chat__bubble chat__bubble--pina">
      <p className="chat__reply">{answer.reply}</p>
      <Understood answer={answer} />
      {current && answer.results.length > 0 && (
        <ResultList
          answer={answer}
          destinationId={destinationId}
          destinationName={destinationName}
          savedRefs={savedRefs}
          shown={shown}
          onShowMore={onShowMore}
          onSaved={onSaved}
          onShowOnMap={onShowOnMap}
        />
      )}
    </div>
  );
}

/** what the model understood, as chips, so a wrong guess is easy to spot. */
function Understood({ answer }: { answer: AskResponse }) {
  const intent = answer.interpretation;
  return (
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
    </div>
  );
}

function ResultList({
  answer,
  destinationId,
  destinationName,
  savedRefs,
  shown,
  onShowMore,
  onSaved,
  onShowOnMap,
}: {
  answer: AskResponse;
  destinationId: number;
  destinationName: string;
  savedRefs: Set<string>;
  shown: number;
  onShowMore: () => void;
  onSaved: (place: Place) => void;
  onShowOnMap: (result: Suggestion) => void;
}) {
  // one row open at a time.
  const [expandedRef, setExpandedRef] = useState<string | null>(null);
  useEffect(() => setExpandedRef(null), [answer]);

  const visible = answer.results.slice(0, shown);
  const more = Math.min(ASK_PAGE_SIZE, answer.results.length - visible.length);

  return (
    <div className="ask__answer">
      <p className="muted">
        {visible.length < answer.total
          ? `Showing the ${visible.length} closest of ${answer.total.toLocaleString()}.`
          : "Closest first."}
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
      {/* only up to the 25 already fetched; past that, narrow the search. */}
      {more > 0 && (
        <button
          type="button"
          className="button button--secondary ask__more"
          onClick={onShowMore}
        >
          Show {more} more
        </button>
      )}
    </div>
  );
}

/** the summary line under a result: category, cuisine, a couple of features. */
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

/** one result row, compact until clicked. keeps its own saving state. */
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
      // the same save as a map popup's.
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
        {/* save sits beside the toggle; a button can't contain a button. */}
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
