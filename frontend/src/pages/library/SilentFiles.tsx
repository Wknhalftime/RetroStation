import { PageHeader } from "@/components/ui/PageHeader";
import { Spinner } from "@/components/ui/Spinner";
import { EmptyState } from "@/components/ui/EmptyState";
import { useSilentFiles } from "@/api/silentFiles";
import type { SilenceReason, SilentFile } from "@/lib/schemas/silentFiles";
import { formatDuration } from "@/lib/utils";

const CELL = "px-3 py-2 text-gray-700";
const HEADINGS = ["Artist", "Title", "Album", "Path", "Duration", "Why listed"];

const REASON_TEXT: Record<SilenceReason, (file: SilentFile) => string> = {
  silent: () => "Silent: no audio (skipped on air)",
  low_bitrate: (file) => `Low bitrate: ${file.bitrate_kbps ?? "?"} kb/s`,
  short_cued_span: (file) =>
    `Audible ${formatDuration(file.cued_span_ms)} of ${formatDuration(file.duration_ms)}`,
};

function Row({ file }: { file: SilentFile }) {
  return (
    <tr className="border-b border-gray-100 last:border-0">
      <td className={CELL}>{file.artist ?? "—"}</td>
      <td className={CELL}>{file.title ?? "—"}</td>
      <td className={CELL}>{file.album ?? "—"}</td>
      <td className={`${CELL} break-all font-mono text-xs`}>{file.path}</td>
      <td className={CELL}>{formatDuration(file.duration_ms)}</td>
      <td className={CELL}>
        <ul>
          {file.reasons.map((reason) => (
            <li key={reason}>{REASON_TEXT[reason](file)}</li>
          ))}
        </ul>
      </td>
    </tr>
  );
}

export function SilentFiles() {
  const { data, isLoading, isError } = useSilentFiles();

  if (isLoading) return <Spinner className="mx-auto my-16 h-8 w-8 text-indigo-500" />;
  if (isError || !data)
    return <p className="rounded-md bg-red-50 p-4 text-sm text-red-700">Failed to load.</p>;

  return (
    <div>
      <PageHeader
        title="Silent Files"
        description={
          `${data.silent} silent, ${data.nearly_silent} nearly silent. Silent files are ` +
          "skipped on air and left out of M3U exports; nearly silent ones still play."
        }
      />
      {data.files.length === 0 ? (
        <EmptyState title="No silent or nearly silent files found." />
      ) : (
        <div className="overflow-x-auto rounded-xl border border-gray-200 bg-white shadow-sm">
          <table className="w-full text-left text-sm">
            <thead className="border-b border-gray-200 bg-gray-50">
              <tr>
                {HEADINGS.map((heading) => (
                  <th key={heading} className="px-3 py-2 font-medium text-gray-500">
                    {heading}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.files.map((file) => (
                <Row key={file.file_id} file={file} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
