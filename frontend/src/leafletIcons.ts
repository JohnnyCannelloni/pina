/**
 * map marker icons, as divIcons.
 *
 * leaflet's default icon loads pngs from a runtime-relative url that vite
 * breaks, so these are plain html/svg instead. marker kinds differ by shape
 * and size, not just colour.
 */

import L from "leaflet";

/** an svg teardrop pin. `glyph` is drawn in the head. */
function pinSvg(fill: string, stroke: string, glyph: string): string {
  return `
    <svg width="100%" height="100%" viewBox="0 0 24 34" aria-hidden="true">
      <path d="M12 0C5.4 0 0 5.4 0 12c0 8.4 12 22 12 22s12-13.6 12-22C24 5.4 18.6 0 12 0z"
            fill="${fill}" stroke="${stroke}" stroke-width="1.5"/>
      <circle cx="12" cy="12" r="7.5" fill="#ffffff" fill-opacity="0.92"/>
      ${glyph}
    </svg>`;
}

const STAR_GLYPH = `
  <path d="M12 6.6l1.68 3.4 3.75.55-2.71 2.64.64 3.74L12 15.17l-3.36 1.76.64-3.74-2.71-2.64 3.75-.55z"
        fill="#b45309"/>`;

const DOT_GLYPH = `<circle cx="12" cy="12" r="3.2" fill="#0f766e"/>`;

/** iconAnchor is the pin's bottom tip, [width / 2, height], not its centre. */
export const placeIcon = L.divIcon({
  html: pinSvg("#14b8a6", "#0f766e", DOT_GLYPH),
  className: "pina-marker",
  iconSize: [24, 34],
  iconAnchor: [12, 34],
  popupAnchor: [0, -32],
});

export const mustGoIcon = L.divIcon({
  html: pinSvg("#fbbf24", "#b45309", STAR_GLYPH),
  className: "pina-marker pina-marker--must-go",
  // larger, so must-go pins stand out and sit on top.
  iconSize: [30, 42],
  iconAnchor: [15, 42],
  popupAnchor: [0, -40],
});

/** the draft pin in the save form. */
export const draftIcon = L.divIcon({
  html: pinSvg("#f43f5e", "#9f1239", DOT_GLYPH),
  className: "pina-marker pina-marker--draft",
  iconSize: [24, 34],
  iconAnchor: [12, 34],
  popupAnchor: [0, -32],
});

/**
 * osm suggestions: a small hollow circle with the category glyph, lighter
 * than a saved pin and still readable when dozens load at once.
 */
const SUGGESTION_GLYPHS: Record<string, string> = {
  restaurant: "🍝",
  cafe: "☕",
  sight: "🏛",
};

/** a bubble with a grid cell's count, sized by order of magnitude. */
export function clusterIcon(count: number): L.DivIcon {
  const size = count >= 1000 ? 56 : count >= 100 ? 46 : count >= 10 ? 38 : 30;
  // "1.2k" rather than 1204, so it fits.
  const label = count >= 1000 ? `${(count / 1000).toFixed(1)}k` : String(count);

  return L.divIcon({
    html: `<span class="pina-cluster__count">${label}</span>`,
    className: "pina-marker pina-cluster",
    iconSize: [size, size],
    // centre-anchored: a circle's coordinate is its middle.
    iconAnchor: [size / 2, size / 2],
  });
}

/** `highlighted` is an ask pina result: the same circle, bigger, with a thicker ring. */
export function suggestionIcon(
  category: string,
  highlighted = false,
): L.DivIcon {
  const glyph = SUGGESTION_GLYPHS[category] ?? "•";
  const size = highlighted ? 34 : 26;
  return L.divIcon({
    html: `<span class="pina-suggestion__glyph">${glyph}</span>`,
    className: `pina-marker pina-suggestion${
      highlighted ? " pina-suggestion--ask" : ""
    }`,
    iconSize: [size, size],
    // centre-anchored, unlike the teardrop pins.
    iconAnchor: [size / 2, size / 2],
    popupAnchor: [0, -(size / 2 + 1)],
  });
}
