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
    scanMutation.mutate({ root_path: localPath });
  }

  // Determine tooltip for disabled states
  let tooltip: string | undefined;
  if (!hasPath) {
    tooltip = "Configure your library path in Settings first";
  } else if (hasRunningScan) {
    tooltip = "Scan in progress";
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
