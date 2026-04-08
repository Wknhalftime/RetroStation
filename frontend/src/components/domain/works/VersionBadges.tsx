/** Display label mapping for VersionType enum values. */
const VERSION_LABELS: Record<string, string> = {
  ORIGINAL: "Studio",
  LIVE: "Live",
  REMASTER: "Remaster",
  REMIX: "Remix",
  RADIO_EDIT: "Radio Edit",
  DEMO: "Demo",
  ACOUSTIC: "Acoustic",
  EXTENDED: "Extended",
  INSTRUMENTAL: "Instrumental",
  EXPLICIT: "Explicit",
  COVER: "Cover",
  EDITION: "Edition",
  ALTERNATE: "Alt",
  FORMAT: "Format",
};

/** Sort order: ORIGINAL first, then alphabetical by label. */
function sortVersionTypes(types: string[]): string[] {
  return [...types].sort((a, b) => {
    if (a === "ORIGINAL") return -1;
    if (b === "ORIGINAL") return 1;
    const la = VERSION_LABELS[a] ?? a;
    const lb = VERSION_LABELS[b] ?? b;
    return la.localeCompare(lb);
  });
}

const MAX_VISIBLE = 5;

interface VersionBadgesProps {
  versionTypes: string[];
}

export function VersionBadges({ versionTypes }: VersionBadgesProps) {
  // Filter out UNKNOWN and OTHER — they indicate bad data
  const displayable = versionTypes.filter(
    (v) => v !== "UNKNOWN" && v !== "OTHER" && VERSION_LABELS[v] != null,
  );

  if (displayable.length === 0) return null;

  const sorted = sortVersionTypes(displayable);
  const visible = sorted.slice(0, MAX_VISIBLE);
  const overflow = sorted.length - MAX_VISIBLE;

  return (
    <span className="ml-1.5 inline-flex flex-wrap gap-1">
      {visible.map((vt) => (
        <span
          key={vt}
          className="inline-flex items-center rounded-full bg-gray-100 px-2 py-0.5 text-[10px] font-medium text-gray-500"
        >
          {VERSION_LABELS[vt]}
        </span>
      ))}
      {overflow > 0 && (
        <span className="inline-flex items-center text-[10px] text-gray-400">
          +{overflow} more
        </span>
      )}
    </span>
  );
}
