/**
 * restaurants / cafés / sights toggle chips for the map.
 *
 * map only: the places list always shows everything, so it stays the complete
 * record.
 */

import {
  ALL_CATEGORIES,
  CATEGORY_ICONS,
  CATEGORY_LABELS,
  type Category,
} from "./types";

interface CategoryFilterProps {
  /** categories currently shown. */
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
    // a new set, so react sees the change.
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
            // tells screen readers this is a toggle and its state.
            aria-pressed={isOn}
          >
            <span aria-hidden="true">{CATEGORY_ICONS[category]}</span>
            {/* plural on the chips, singular in popups. */}
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
