import { RefreshCw } from "lucide-react";
import { Spinner } from "@/components/ui/Spinner";
import { useScanLibrary } from "@/api/library";
import { useSettings } from "@/api/settings";
import { useProgressStore } from "@/store/progressStore";

export function ScanLibraryButton() {
  const { data: settings } = useSettings();
  const scanMutation = useScanLibrary();
  const hasRunningScan = useProgressStore((s) => s.hasRunningType("scan"));

  const localPath = settings?.["local_path_prefix"];
  const hasPath = !!localPath;
  const isDisabled = !hasPath || hasRunningScan || scanMutation.isPending;

  function handleClick() {
    if (!localPath) return;
    // A full scan reads every byte of every file to rebuild the index. The
    // watcher already picks up new and changed folders every few minutes,
    // so this is rarely what someone wants on an existing library.
    const proceed = window.confirm(
      `Scan Library re-reads every audio file under ${localPath} to rebuild ` +
        "the index. On a large library that takes hours.\n\n" +
        "New and changed folders are picked up automatically every few " +
        "minutes without this.\n\nRun a full scan anyway?"
    );
    if (!proceed) return;
    scanMutation.mutate({ root_path: localPath });
  }

  let tooltip: string | undefined;
  if (!hasPath) {
    tooltip = "Configure your library path in Settings first";
  } else if (hasRunningScan) {
    tooltip = "Scan in progress";
  } else {
    tooltip = "Full re-index: reads every file. Changes are picked up automatically.";
  }

  const button = (
    <button
      type="button"
      onClick={handleClick}
      disabled={isDisabled}
      className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400 disabled:opacity-60"
    >
      {scanMutation.isPending ? (
        <>
          <Spinner className="h-4 w-4" />
          Starting scan...
        </>
      ) : (
        <>
          <RefreshCw className="h-4 w-4" />
          Scan Library
        </>
      )}
    </button>
  );

  // Wrap in span for tooltip on disabled buttons (cross-browser safe)
  if (tooltip) {
    return <span title={tooltip}>{button}</span>;
  }

  return button;
}
