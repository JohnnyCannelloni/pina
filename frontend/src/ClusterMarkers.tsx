/**
 * count bubbles for a zoomed-out view. clicking one zooms into its cell.
 *
 * the grouping happens in sql, so only ~60 numbers reach the browser. a
 * client-side clustering library would need every marker first.
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
          // position is stable per viewport, and clusters have no id.
          key={`${cluster.latitude},${cluster.longitude}`}
          position={[cluster.latitude, cluster.longitude]}
          icon={clusterIcon(cluster.count)}
          eventHandlers={{
            click: () => {
              // two zoom steps; one often leaves the cell too dense to split.
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
