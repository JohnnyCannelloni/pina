/**
 * Restaurants / Cafés / Sights toggle chips for the map.
 *
 * Filters both your saved pins and the suggestions. Once a search drops fifty
 * markers onto central Venice, being able to say "just the sights" is the
 * difference between a map and a mess.
 *
 * The filter is map-only: the Places list always shows everything. That is
 * deliberate — the list is the complete record, and so it is always the answer
 * to "where did my place go?". A hidden row in a filtered list is far more
 * confusing than a hidden marker, because a list gives no visual hint that
 * something is missing.
 */

import {
  ALL_CATEGORIES,
  CATEGORY_ICONS,
  CATEGORY_LABELS,
  type Category,
} from "./types";

interface CategoryFilterProps {
  /** Which categories are currently shown. */
  selected: ReadonlySet<Category>;
  onChange: (selected: Set<Category>) => void;
}

const PLURAL_LABELS: Record<Category, string> = {
  restaurant: "Restaurants",
  cafe: "Cafés",
  sight: "Sights",
};

export function CategoryFilter({ selected, onChange }: CategoryFilterProps) {
  function toggle(category: Category) {
    // A new Set rather than mutating: React compares by reference, so adding to
    // the existing Set would change its contents without re-rendering anything.
    const next = new Set(selected);
    if (next.has(category)) {
      next.delete(category);
    } else {
      next.add(category);
    }
    onChange(next);
  }

  return (
    <div
      className="chips"
      role="group"
      aria-label="Filter the map by category"
    >
      {ALL_CATEGORIES.map((category) => {
        const isOn = selected.has(category);
        return (
          <button
            key={category}
            type="button"
            className={`chip${isOn ? " chip--on" : ""}`}
            onClick={() => toggle(category)}
            // aria-pressed is what tells a screen reader this is a toggle and
            // which way it is set. Without it both states announce identically.
            aria-pressed={isOn}
          >
            <span aria-hidden="true">{CATEGORY_ICONS[category]}</span>
            {/* Plural on the chips ("Restaurants"), singular in a popup
                ("Restaurant"). A filter names a group; a label names one thing. */}
            <span className="chip__label">{PLURAL_LABELS[category]}</span>
            <span className="sr-only">
              {isOn ? " (shown)" : " (hidden)"} — {CATEGORY_LABELS[category]}
            </span>
          </button>
        );
      })}
    </div>
  );
}
