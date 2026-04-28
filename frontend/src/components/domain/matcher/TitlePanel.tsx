import { useEffect, useState } from "react";
import {
  AlertTriangle,
  Ban,
  CheckCircle2,
  ChevronRight,
  Circle,
  CircleCheck,
  CircleSlash,
  Clock,
  X,
} from "lucide-react";
import { useResolveIdentity } from "@/api/matcher";
import { cn } from "@/lib/utils";
import type { QueueArtist, QueueIdentity, TriageBucket } from "@/lib/schemas/matcher";

interface TitlePanelProps {
  artist: QueueArtist | null;
  onFileSearch: (identityId: string) => void;
}

// ---------------------------------------------------------------------------
// Pure helpers (colocated — small enough not to warrant their own files)
// ---------------------------------------------------------------------------

const REVIEW_STATUSES = new Set(["pending", "needs_review"]);

const RESOLVED_BUCKETS = {
  auto_matched: 0,
  manual_matched: 0,
  rejected: 0,
} as const;
type ResolvedBuckets = { auto_matched: number; manual_matched: number; rejected: number };

function partitionByReviewState(
  identities: QueueIdentity[]
): { review: QueueIdentity[]; resolved: QueueIdentity[] } {
  const review: QueueIdentity[] = [];
  const resolved: QueueIdentity[] = [];
  for (const id of identities) {
    if (REVIEW_STATUSES.has(id.match_status)) review.push(id);
    else resolved.push(id);
  }
  return { review, resolved };
}

const BUCKET_PRIORITY: Record<TriageBucket, number> = {
  quick_review: 0,
  needs_attention: 1,
  blocked: 2,
};

function sortReviewIdentities(items: QueueIdentity[]): QueueIdentity[] {
  // pending items have no score yet; they sort after needs_review within
  // their bucket so curators see actionable rows first.
  return [...items].sort((a, b) => {
    const ba = BUCKET_PRIORITY[a.triage_bucket];
    const bb = BUCKET_PRIORITY[b.triage_bucket];
    if (ba !== bb) return ba - bb;
    const isPendingA = a.match_status === "pending" ? 1 : 0;
    const isPendingB = b.match_status === "pending" ? 1 : 0;
    if (isPendingA !== isPendingB) return isPendingA - isPendingB;
    const sa = a.confidence_score ?? -Infinity;
    const sb = b.confidence_score ?? -Infinity;
    return sb - sa;
  });
}

function summariseResolved(items: QueueIdentity[]): ResolvedBuckets {
  const out: ResolvedBuckets = { ...RESOLVED_BUCKETS };
  for (const id of items) {
    if (id.match_status === "auto_matched") out.auto_matched += 1;
    else if (id.match_status === "manual_matched") out.manual_matched += 1;
    else if (id.match_status === "auto_rejected" || id.match_status === "manual_rejected") {
      out.rejected += 1;
    }
  }
  return out;
}

// Trim a posix/win path down to its filename so the muted file_path line
// doesn't dominate the card. Also serves as the title fallback when the
// library_files row has no track_title.
function basename(p: string): string {
  const slash = Math.max(p.lastIndexOf("/"), p.lastIndexOf("\\"));
  return slash >= 0 ? p.slice(slash + 1) : p;
}

// Persist disclosure state across reloads. Global key (not per-artist) so
// switching artists doesn't reset the curator's chosen mode.
const RESOLVED_COLLAPSED_KEY = "matcher.resolved.collapsed";

function readCollapsedPref(): boolean {
  if (typeof window === "undefined") return true;
  try {
    const raw = window.localStorage.getItem(RESOLVED_COLLAPSED_KEY);
    if (raw === null) return true; // default collapsed
    return raw === "true";
  } catch {
    return true;
  }
}

function useLocalStorageCollapsed(): [boolean, (next: boolean) => void] {
  const [collapsed, setCollapsed] = useState<boolean>(readCollapsedPref);
  useEffect(() => {
    try {
      window.localStorage.setItem(RESOLVED_COLLAPSED_KEY, collapsed ? "true" : "false");
    } catch {
      // ignore quota / privacy-mode errors; UI still works in-memory
    }
  }, [collapsed]);
  return [collapsed, setCollapsed];
}

// ---------------------------------------------------------------------------
// Bucket / status presentation
// ---------------------------------------------------------------------------

interface ReviewChip {
  border: string;
  chipBg: string;
  chipText: string;
  Icon: typeof CircleCheck;
  label: string;
}

function reviewChipFor(identity: QueueIdentity): ReviewChip {
  const score = identity.confidence_score;
  const scoreSuffix = score != null ? ` · ${score.toFixed(0)}%` : "";

  if (identity.match_status === "pending") {
    return {
      border: "border-l-blue-400",
      chipBg: "bg-blue-50",
      chipText: "text-blue-700",
      Icon: Clock,
      label: "Queued for matching",
    };
  }
  switch (identity.triage_bucket) {
    case "quick_review":
      return {
        border: "border-l-emerald-500",
        chipBg: "bg-emerald-50",
        chipText: "text-emerald-700",
        Icon: CircleCheck,
        label: `Quick review${scoreSuffix}`,
      };
    case "needs_attention":
      return {
        border: "border-l-amber-500",
        chipBg: "bg-amber-50",
        chipText: "text-amber-700",
        Icon: AlertTriangle,
        label: `Needs attention${scoreSuffix}`,
      };
    case "blocked":
      return {
        border: "border-l-rose-500",
        chipBg: "bg-rose-50",
        chipText: "text-rose-700",
        Icon: Ban,
        label: identity.reason_code ? `Blocked · ${identity.reason_code}` : "Blocked",
      };
  }
}

// ---------------------------------------------------------------------------
// TitlePanel
// ---------------------------------------------------------------------------

export function TitlePanel({ artist, onFileSearch }: TitlePanelProps) {
  const resolveIdentity = useResolveIdentity();
  const [resolvedCollapsed, setResolvedCollapsed] = useLocalStorageCollapsed();

  // Mirrors the isResolved flag in ArtistPanel — must include auto_matched
  // so the queue's broadened CTE (which now surfaces auto_matched parents
  // with review-needing children) doesn't strand the curator on the
  // "Resolve the artist first" placeholder. Excludes auto_rejected /
  // manual_rejected: rejected parents have all non-protected children
  // cascaded to AUTO_REJECTED by resolve_artist, so a rejected parent with
  // pending/needs_review children is unreachable in steady state.
  const artistResolved =
    artist !== null &&
    (artist.match_status === "auto_matched" || artist.match_status === "manual_matched");

  if (!artist) {
    return (
      <div className="rounded-lg border border-gray-200 bg-white p-5">
        <p className="text-sm text-gray-400">Select an artist to view titles.</p>
      </div>
    );
  }

  if (!artistResolved) {
    return (
      <div className="rounded-lg border border-gray-200 bg-white p-5">
        <p className="text-sm text-gray-500">Resolve the artist first before managing titles.</p>
      </div>
    );
  }

  const identities: QueueIdentity[] = artist.identities ?? [];
  const isPending = resolveIdentity.isPending;
  const { review: reviewIdentities, resolved: resolvedIdentities } =
    partitionByReviewState(identities);
  const sortedReview = sortReviewIdentities(reviewIdentities);
  const resolvedSummary = summariseResolved(resolvedIdentities);

  function handleApprove(identity: QueueIdentity) {
    if (!identity.proposed_match) return;
    resolveIdentity.mutate({
      id: identity.id,
      resolution: {
        match_status: "manual_matched",
        library_file_id: identity.proposed_match.library_file_id,
      },
    });
  }

  function handleReject(identity: QueueIdentity) {
    resolveIdentity.mutate({
      id: identity.id,
      resolution: { match_status: "manual_rejected", library_file_id: null },
    });
  }

  if (identities.length === 0) {
    return (
      <div className="rounded-lg border border-gray-200 bg-white p-5">
        <p className="mb-3 text-xs font-medium uppercase tracking-wide text-gray-400">Titles (0)</p>
        <p className="text-sm text-gray-400">No identities for this artist.</p>
      </div>
    );
  }

  return (
    <div className="flex max-h-[70vh] min-h-0 flex-col rounded-lg border border-gray-200 bg-white">
      <div className="flex-1 overflow-y-auto px-5 py-4">
        <ReviewSection
          items={sortedReview}
          isPending={isPending}
          onApprove={handleApprove}
          onReject={handleReject}
          onFileSearch={onFileSearch}
          hasResolved={resolvedIdentities.length > 0}
        />

        {resolvedIdentities.length > 0 && (
          <ResolvedSection
            items={resolvedIdentities}
            summary={resolvedSummary}
            collapsed={resolvedCollapsed}
            onToggle={() => setResolvedCollapsed(!resolvedCollapsed)}
            isPending={isPending}
            onReject={handleReject}
            onFileSearch={onFileSearch}
          />
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Needs Review section
// ---------------------------------------------------------------------------

interface ReviewSectionProps {
  items: QueueIdentity[];
  isPending: boolean;
  onApprove: (id: QueueIdentity) => void;
  onReject: (id: QueueIdentity) => void;
  onFileSearch: (identityId: string) => void;
  hasResolved: boolean;
}

function ReviewSection({
  items,
  isPending,
  onApprove,
  onReject,
  onFileSearch,
  hasResolved,
}: ReviewSectionProps) {
  return (
    <section aria-labelledby="needs-review-heading">
      <header
        className={cn(
          "sticky top-0 z-10 -mx-5 -mt-4 mb-2 flex items-baseline justify-between border-b",
          "border-gray-100 bg-white/95 px-5 pb-2 pt-4 backdrop-blur"
        )}
      >
        <h3
          id="needs-review-heading"
          className="text-xs font-semibold uppercase tracking-wide text-gray-700"
        >
          Needs Review ({items.length})
        </h3>
        {items.length > 0 && (
          <span className="text-[11px] text-gray-400">Sorted by triage · confidence</span>
        )}
      </header>

      {items.length === 0 ? (
        <p className="py-4 text-sm text-gray-500">
          {hasResolved
            ? "Nothing left to review for this artist. ✓"
            : "No identities for this artist."}
        </p>
      ) : (
        <div className="space-y-2">
          {items.map((identity) => (
            <ReviewCard
              key={identity.id}
              identity={identity}
              isPending={isPending}
              onApprove={onApprove}
              onReject={onReject}
              onFileSearch={onFileSearch}
            />
          ))}
        </div>
      )}
    </section>
  );
}

interface ReviewCardProps {
  identity: QueueIdentity;
  isPending: boolean;
  onApprove: (id: QueueIdentity) => void;
  onReject: (id: QueueIdentity) => void;
  onFileSearch: (identityId: string) => void;
}

function ReviewCard({
  identity,
  isPending,
  onApprove,
  onReject,
  onFileSearch,
}: ReviewCardProps) {
  const chip = reviewChipFor(identity);
  const Icon = chip.Icon;
  // The bucket chip is the dominant signal; avoid double-encoding the same
  // info with the legacy MatchStatusBadge in the corner.
  const showApprove =
    identity.match_status === "needs_review" &&
    identity.triage_bucket !== "blocked" &&
    identity.proposed_match != null;

  // Build segments so "· Score" never appears as the first visible token
  // when match_tier is missing (regression guard from prior fix).
  const tierScoreSegments: string[] = [];
  if (identity.match_tier) tierScoreSegments.push(`Tier: ${identity.match_tier}`);
  if (identity.confidence_score != null) {
    tierScoreSegments.push(`Score: ${identity.confidence_score.toFixed(0)}%`);
  }
  const reasonText = identity.reason_detail || identity.reason_code;

  return (
    <div
      className={cn(
        "rounded-md border-l-4 border border-gray-200 bg-white px-3 py-2 shadow-sm",
        chip.border
      )}
    >
      <div className="mb-2 flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate text-sm font-medium text-gray-800">
            {identity.original_title}
          </p>
          {tierScoreSegments.length > 0 && (
            <p className="text-xs text-gray-400">{tierScoreSegments.join(" · ")}</p>
          )}
          {reasonText && (
            <p className="mt-1 text-xs text-amber-600">⚠ {reasonText}</p>
          )}
        </div>
        <span
          className={cn(
            "inline-flex shrink-0 items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium",
            chip.chipBg,
            chip.chipText
          )}
        >
          <Icon className="h-3 w-3" aria-hidden="true" />
          {chip.label}
        </span>
      </div>

      {identity.proposed_match && (
        <div
          data-testid="proposed-match"
          className="mb-2 rounded border border-gray-200 bg-gray-50 px-2.5 py-1.5"
        >
          <p className="text-xs font-medium uppercase tracking-wide text-gray-400">
            Proposed match
          </p>
          <p className="truncate text-sm text-gray-800">
            {identity.proposed_match.track_title || basename(identity.proposed_match.file_path)}
          </p>
          {identity.proposed_match.release_title && (
            <p className="truncate text-xs text-gray-500">
              {identity.proposed_match.release_title}
            </p>
          )}
          <p className="truncate text-xs text-gray-400">
            via {identity.proposed_match.candidate_match_tier} ·{" "}
            {identity.proposed_match.file_path}
          </p>
        </div>
      )}

      <div className="flex gap-2">
        {showApprove && (
          <button
            onClick={() => onApprove(identity)}
            disabled={isPending}
            className="rounded bg-green-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-green-700 disabled:opacity-50"
          >
            Approve
          </button>
        )}
        <button
          onClick={() => onFileSearch(identity.id)}
          disabled={isPending}
          className="rounded bg-blue-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-blue-700 disabled:opacity-50"
        >
          Find File
        </button>
        <button
          onClick={() => onReject(identity)}
          disabled={isPending}
          className="rounded border border-red-200 px-2.5 py-1 text-xs font-medium text-red-600 hover:bg-red-50 disabled:opacity-50"
        >
          Reject
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Resolved section (collapsible)
// ---------------------------------------------------------------------------

interface ResolvedSectionProps {
  items: QueueIdentity[];
  summary: ResolvedBuckets;
  collapsed: boolean;
  onToggle: () => void;
  isPending: boolean;
  onReject: (id: QueueIdentity) => void;
  onFileSearch: (identityId: string) => void;
}

function ResolvedSection({
  items,
  summary,
  collapsed,
  onToggle,
  isPending,
  onReject,
  onFileSearch,
}: ResolvedSectionProps) {
  // Build a muted breakdown shown in the header even when collapsed.
  const breakdownParts: string[] = [];
  if (summary.auto_matched) breakdownParts.push(`${summary.auto_matched} auto`);
  if (summary.manual_matched) breakdownParts.push(`${summary.manual_matched} manual`);
  if (summary.rejected) breakdownParts.push(`${summary.rejected} rejected`);
  const breakdown = breakdownParts.join(" · ");

  const panelId = "resolved-section-panel";

  return (
    <section className="mt-4" aria-labelledby="resolved-heading">
      <header
        className={cn(
          "sticky top-0 z-10 -mx-5 border-y border-gray-100",
          "bg-white/95 backdrop-blur"
        )}
      >
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={!collapsed}
          aria-controls={panelId}
          className="flex w-full items-baseline justify-between px-5 py-2 text-left hover:bg-gray-50"
        >
          <span className="flex items-baseline gap-2">
            <ChevronRight
              className={cn(
                "h-3.5 w-3.5 self-center text-gray-500 transition-transform motion-reduce:transition-none",
                !collapsed && "rotate-90"
              )}
              aria-hidden="true"
            />
            <h3
              id="resolved-heading"
              className="text-xs font-semibold uppercase tracking-wide text-gray-700"
            >
              Resolved ({items.length})
            </h3>
          </span>
          {breakdown && <span className="text-[11px] text-gray-400">{breakdown}</span>}
        </button>
      </header>

      {!collapsed && (
        <div id={panelId} className="divide-y divide-gray-100">
          {items.map((identity) => (
            <ResolvedRow
              key={identity.id}
              identity={identity}
              isPending={isPending}
              onReject={onReject}
              onFileSearch={onFileSearch}
            />
          ))}
        </div>
      )}
    </section>
  );
}

interface ResolvedRowProps {
  identity: QueueIdentity;
  isPending: boolean;
  onReject: (id: QueueIdentity) => void;
  onFileSearch: (identityId: string) => void;
}

function ResolvedRow({ identity, isPending, onReject, onFileSearch }: ResolvedRowProps) {
  const isMatched =
    identity.match_status === "auto_matched" || identity.match_status === "manual_matched";
  const isRejected =
    identity.match_status === "auto_rejected" || identity.match_status === "manual_rejected";

  let LeadingIcon: typeof Circle = Circle;
  let leadingClass = "text-gray-400";
  let label = identity.match_status;
  if (identity.match_status === "auto_matched") {
    LeadingIcon = Circle;
    leadingClass = "text-emerald-500 fill-emerald-500";
    label = "auto";
  } else if (identity.match_status === "manual_matched") {
    LeadingIcon = CheckCircle2;
    leadingClass = "text-emerald-600";
    label = "manual";
  } else if (identity.match_status === "auto_rejected") {
    LeadingIcon = CircleSlash;
    leadingClass = "text-gray-400";
    label = "auto-rejected";
  } else if (identity.match_status === "manual_rejected") {
    LeadingIcon = X;
    leadingClass = "text-gray-400";
    label = "rejected";
  }

  return (
    <div className="group flex items-center gap-2 px-3 py-1.5 text-sm hover:bg-gray-50 focus-within:bg-gray-50">
      <LeadingIcon
        className={cn("h-3.5 w-3.5 shrink-0", leadingClass)}
        aria-hidden="true"
      />
      <span className="w-16 shrink-0 text-[11px] uppercase tracking-wide text-gray-400">
        {label}
      </span>
      <span
        className={cn(
          "min-w-0 flex-1 truncate text-gray-700",
          isRejected && "text-gray-400 line-through"
        )}
      >
        {identity.original_title}
      </span>
      <span className="ml-auto flex shrink-0 gap-1 opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100">
        {isMatched && (
          <button
            onClick={() => onReject(identity)}
            disabled={isPending}
            className="rounded border border-red-200 px-2 py-0.5 text-[11px] font-medium text-red-600 hover:bg-red-50 disabled:opacity-50"
            aria-label={`Reject ${identity.original_title}`}
          >
            Reject
          </button>
        )}
        {isRejected && (
          <button
            onClick={() => onFileSearch(identity.id)}
            disabled={isPending}
            className="rounded bg-blue-600 px-2 py-0.5 text-[11px] font-medium text-white hover:bg-blue-700 disabled:opacity-50"
            aria-label={`Find file for ${identity.original_title}`}
          >
            Find File
          </button>
        )}
      </span>
    </div>
  );
}
