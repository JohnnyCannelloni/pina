/**
 * Count bubbles for a zoomed-out view.
 *
 * When a viewport holds more places than can be usefully drawn, the server
 * returns grid-aggregated counts instead of individual places, and this renders
 * them. Clicking one zooms the map into that cell, which is how you drill from
 * "4,383 places in Lisbon" down to the five cafés on one street.
 *
 * Why this is not a clustering *library* (react-leaflet-cluster and friends):
 * those group markers in the browser, which means the browser must first
 * receive every marker. The whole point here is that it never does — the
 * grouping happens in SQL and only ~60 numbers come down the wire. A client-side
 * clusterer would reintroduce the problem it appears to solve.
 */

import { Marker, useMap } from "react-leaflet";

import { clusterIcon } from "./leafletIcons";
import type { Cluster } from "./types";

interface ClusterMarkersProps {
  clusters: Cluster[];
}

export function ClusterMarkers({ clusters }: ClusterMarkersProps) {
  const map = useMap();

  return (
    <>
      {clusters.map((cluster) => (
        <Marker
          // Position is a fine key here: the server derives it from the grid
          // cell, so it is stable for a given viewport, and clusters have no
          // identity of their own to key on.
          key={`${cluster.latitude},${cluster.longitude}`}
          position={[cluster.latitude, cluster.longitude]}
          icon={clusterIcon(cluster.count)}
          eventHandlers={{
            click: () => {
              // Zoom in two steps rather than one: a single step often leaves
              // the cell still too dense to break apart, so the bubble appears
              // not to respond to the click.
              map.flyTo(
                [cluster.latitude, cluster.longitude],
                Math.min(map.getZoom() + 2, map.getMaxZoom()),
                { duration: 0.5 },
              );
            },
          }}
        />
      ))}
    </>
  );
}
