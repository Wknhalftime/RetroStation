import type { Act } from "@/lib/schemas/works";

// 3b PR 5a (D41 f; design C61, C65): on a work whose performances credit two or more acts, which
// act's lines an override or version entry reaches. The default act's lines on the default path
// are reached; another act's lines keep their own performance, which plays first.

function lines(n: number): string {
  if (n === 0) return "no lines";
  return n === 1 ? "1 line" : `${n} lines`;
}

interface TwoActsNoticeProps {
  acts: Act[];
  /** The format an override is for, when one is typed. */
  format?: string;
}

export function TwoActsNotice({ acts, format }: TwoActsNoticeProps) {
  const reaching = acts.find((a) => a.default);
  const keeping = acts.filter((a) => !a.default);
  return (
    <div
      role="note"
      aria-label="Two acts"
      className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900"
    >
      <p>This work holds {acts.length} acts.</p>
      {reaching && (
        <p>
          An override or version entry here reaches {lines(reaching.lines)} of {reaching.credit}{" "}
          (the default).
          {format
            ? ` Of these, at ${format} stations: ${lines(reaching.reached[format] ?? 0)}.`
            : ""}
        </p>
      )}
      {keeping.map((act) => (
        <p key={act.credit}>
          {lines(act.lines)} of {act.credit} {act.lines === 1 ? "keeps its" : "keep their"} own
          performance.
        </p>
      ))}
    </div>
  );
}
