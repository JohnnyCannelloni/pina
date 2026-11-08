/**
 * The two marker icons: one for ordinary places, one for must-go places.
 *
 * Why these are hand-built `divIcon`s rather than Leaflet's default marker:
 * Leaflet's built-in icon resolves its PNGs through a relative URL computed at
 * runtime, which bundlers rewrite — so under Vite the images 404 and your
 * markers silently vanish with no console error. The usual fix is to import the
 * PNGs explicitly and reassign L.Icon.Default. A divIcon sidesteps the problem
 * entirely (it is just HTML), ships no image assets, and scales crisply.
 *
 * It also lets must-go markers differ by **shape** — a star inside a larger pin
 * — and not only by colour. Colour alone fails for a colour-blind user, and
 * fails again in a screenshot printed in greyscale.
 */

import L from "leaflet";

/** An SVG teardrop pin. `glyph` is drawn in the circular head. */
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

/**
 * iconAnchor is the subtle one: it says which pixel of the image sits on the
 * coordinate. For a teardrop pin that is the bottom tip — [width/2, height] —
 * not the centre. Get it wrong and every marker appears offset from where it
 * was actually dropped, by half its own height.
 */
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
  // Deliberately larger, so must-go pins read as different at a glance and sit
  // visually on top of the ordinary ones.
  iconSize: [30, 42],
  iconAnchor: [15, 42],
  popupAnchor: [0, -40],
});

/** The pin being placed in the form — distinct again, so it is clearly a draft. */
export const draftIcon = L.divIcon({
  html: pinSvg("#f43f5e", "#9f1239", DOT_GLYPH),
  className: "pina-marker pina-marker--draft",
  iconSize: [24, 34],
  iconAnchor: [12, 34],
  popupAnchor: [0, -32],
});
