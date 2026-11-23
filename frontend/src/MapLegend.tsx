/**
 * a key for the three marker styles: saved, must-go and suggested.
 *
 * inline svg rather than the leaflet icons, so keep the two in step by hand.
 */

export function MapLegend() {
  return (
    <ul className="legend" aria-label="Map key">
      <li className="legend__item">
        <svg
          className="legend__swatch"
          viewBox="0 0 24 34"
          aria-hidden="true"
          focusable="false"
        >
          <path
            d="M12 0C5.4 0 0 5.4 0 12c0 8.4 12 22 12 22s12-13.6 12-22C24 5.4 18.6 0 12 0z"
            fill="#14b8a6"
            stroke="#0f766e"
            strokeWidth="1.5"
          />
          <circle cx="12" cy="12" r="7.5" fill="#fff" fillOpacity="0.92" />
          <circle cx="12" cy="12" r="3.2" fill="#0f766e" />
        </svg>
        Saved
      </li>

      <li className="legend__item">
        <svg
          className="legend__swatch legend__swatch--large"
          viewBox="0 0 24 34"
          aria-hidden="true"
          focusable="false"
        >
          <path
            d="M12 0C5.4 0 0 5.4 0 12c0 8.4 12 22 12 22s12-13.6 12-22C24 5.4 18.6 0 12 0z"
            fill="#fbbf24"
            stroke="#b45309"
            strokeWidth="1.5"
          />
          <circle cx="12" cy="12" r="7.5" fill="#fff" fillOpacity="0.92" />
          <path
            d="M12 6.6l1.68 3.4 3.75.55-2.71 2.64.64 3.74L12 15.17l-3.36 1.76.64-3.74-2.71-2.64 3.75-.55z"
            fill="#b45309"
          />
        </svg>
        Must-go
      </li>

      <li className="legend__item">
        <span className="legend__suggestion" aria-hidden="true" />
        Suggested — tap to save
      </li>
    </ul>
  );
}
