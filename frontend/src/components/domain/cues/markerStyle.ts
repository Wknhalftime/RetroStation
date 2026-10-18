import type { MarkerKey } from "./cueMath";

// How each marker is named and coloured, everywhere the editor shows it (waveform and table).

export const MARKER_ORDER: readonly MarkerKey[] = ["start", "eom", "fadeAt", "stop"];

export const MARKER_LABEL: Record<MarkerKey, string> = {
  start: "Start",
  eom: "EOM",
  fadeAt: "Fade-out",
  stop: "Stop",
};

export const MARKER_COLOR: Record<MarkerKey, string> = {
  start: "#4caf50",
  eom: "#ffb300",
  fadeAt: "#42a5f5",
  stop: "#ef5350",
};
