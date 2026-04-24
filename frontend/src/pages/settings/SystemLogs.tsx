import { useState } from "react";
import { PageHeader } from "@/components/ui/PageHeader";
import { Spinner } from "@/components/ui/Spinner";
import { EmptyState } from "@/components/ui/EmptyState";
import { useSystemLogs, type SystemLogsParams } from "@/api/system_logs";
import type { SystemLogEntry } from "@/lib/schemas/system_logs";

// ---------------------------------------------------------------------------
// Level badge colours
// ---------------------------------------------------------------------------

const levelColour: Record<string, string> = {
  DEBUG: "bg-gray-100 text-gray-600",
  INFO: "bg-blue-100 text-blue-700",
  WARNING: "bg-yellow-100 text-yellow-700",
  ERROR: "bg-red-100 text-red-700",
};

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function LogRow({ entry }: { entry: SystemLogEntry }) {
  const [expanded, setExpanded] = useState(false);
  const colour = levelColour[entry.level] ?? "bg-gray-100 text-gray-600";

  return (
    <>
      <tr
        className="border-b border-gray-100 hover:bg-gray-50 cursor-pointer select-none"
        onClick={() => setExpanded((v) => !v)}
      >
        <td className="px-3 py-2 text-xs text-gray-400 whitespace-nowrap font-mono">
          {new Date(entry.created_at).toLocaleString()}
        </td>
        <td className="px-3 py-2">
          <span className={`rounded px-1.5 py-0.5 text-xs font-semibold ${colour}`}>
            {entry.level}
          </span>
        </td>
        <td className="px-3 py-2 text-xs text-gray-500 whitespace-nowrap">{entry.category}</td>
        <td className="px-3 py-2 text-sm text-gray-800">{entry.message}</td>
        <td className="px-3 py-2 text-xs text-gray-400 font-mono truncate max-w-[120px]">
          {entry.trace_id ?? "—"}
        </td>
      </tr>
      {expanded && entry.details && (
        <tr className="border-b border-gray-100 bg-gray-50">
          <td colSpan={5} className="px-4 pb-3 pt-1">
            <pre className="text-xs text-gray-600 whitespace-pre-wrap break-all">
              {JSON.stringify(entry.details, null, 2)}
            </pre>
          </td>
        </tr>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

const PAGE_SIZE = 100;

const CATEGORIES = [
  "scan",
  "enrichment",
  "ingestion",
  "matching",
  "rules_apply",
  "m3u_export",
  "system",
];
const LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"];

export function SystemLogs() {
  const [level, setLevel] = useState<string>("");
  const [category, setCategory] = useState<string>("");
  const [page, setPage] = useState(0);

  const params: SystemLogsParams = {
    level: level || undefined,
    category: category || undefined,
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
  };

  const { data, isLoading, isError } = useSystemLogs(params);

  const totalPages = data ? Math.ceil(data.total / PAGE_SIZE) : 0;

  return (
    <div className="space-y-6">
      <PageHeader title="System Logs" description="Operational log of background task activity" />

      {/* Filters */}
      <div className="flex gap-3 flex-wrap">
        <select
          value={level}
          onChange={(e) => {
            setLevel(e.target.value);
            setPage(0);
          }}
          className="rounded-md border border-gray-300 px-3 py-1.5 text-sm text-gray-700 focus:outline-none focus:ring-2 focus:ring-indigo-400"
        >
          <option value="">All levels</option>
          {LEVELS.map((l) => (
            <option key={l} value={l}>
              {l}
            </option>
          ))}
        </select>

        <select
          value={category}
          onChange={(e) => {
            setCategory(e.target.value);
            setPage(0);
          }}
          className="rounded-md border border-gray-300 px-3 py-1.5 text-sm text-gray-700 focus:outline-none focus:ring-2 focus:ring-indigo-400"
        >
          <option value="">All categories</option>
          {CATEGORIES.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
      </div>

      {/* Table */}
      {isLoading && (
        <div className="flex justify-center py-12">
          <Spinner className="h-6 w-6 text-indigo-500" />
        </div>
      )}

      {isError && (
        <p className="rounded-md bg-red-50 p-4 text-sm text-red-700">Failed to load system logs.</p>
      )}

      {!isLoading && !isError && data?.items.length === 0 && (
        <EmptyState
          title="No log entries"
          description="Run a scan or enrichment task to generate logs."
        />
      )}

      {!isLoading && !isError && data && data.items.length > 0 && (
        <div className="overflow-x-auto rounded-xl border border-gray-200 bg-white shadow-sm">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-100 bg-gray-50 text-left">
                <th className="px-3 py-2.5 font-semibold text-gray-600 whitespace-nowrap">
                  Timestamp
                </th>
                <th className="px-3 py-2.5 font-semibold text-gray-600">Level</th>
                <th className="px-3 py-2.5 font-semibold text-gray-600">Category</th>
                <th className="px-3 py-2.5 font-semibold text-gray-600">Message</th>
                <th className="px-3 py-2.5 font-semibold text-gray-600">Trace ID</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((entry) => (
                <LogRow key={entry.id} entry={entry} />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center gap-3 justify-end text-sm text-gray-600">
          <button
            onClick={() => setPage((p) => Math.max(0, p - 1))}
            disabled={page === 0}
            className="rounded border border-gray-300 px-3 py-1 disabled:opacity-40"
          >
            Previous
          </button>
          <span>
            Page {page + 1} of {totalPages}
          </span>
          <button
            onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
            disabled={page >= totalPages - 1}
            className="rounded border border-gray-300 px-3 py-1 disabled:opacity-40"
          >
            Next
          </button>
        </div>
      )}
    </div>
  );
}
