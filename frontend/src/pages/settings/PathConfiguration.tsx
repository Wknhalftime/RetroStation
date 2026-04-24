import { useState, useEffect } from "react";
import { Link } from "react-router-dom";
import { ArrowLeft, Save } from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { Spinner } from "@/components/ui/Spinner";
import { useSettings, useUpdateSetting } from "@/api/settings";
import { ScanLibraryButton } from "@/components/domain/library/ScanLibraryButton";

// ---------------------------------------------------------------------------
// Constant keys
// ---------------------------------------------------------------------------

const LOCAL_PATH_KEY = "local_path_prefix";
const NAVIDROME_PATH_KEY = "navidrome_path_prefix";

// ---------------------------------------------------------------------------
// Toast — inline success notification
// ---------------------------------------------------------------------------

function InlineToast({ message, onDismiss }: { message: string; onDismiss: () => void }) {
  useEffect(() => {
    const id = setTimeout(onDismiss, 3000);
    return () => clearTimeout(id);
  }, [onDismiss]);

  return (
    <div className="rounded-md bg-green-50 border border-green-200 px-4 py-2.5 text-sm text-green-800">
      {message}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function PathConfiguration() {
  const { data: settings, isLoading, isError } = useSettings();
  const updateSetting = useUpdateSetting();

  const [localPath, setLocalPath] = useState("");
  const [navidromePath, setNavidromePath] = useState("");
  const [toast, setToast] = useState<string | null>(null);

  // Initialize form values from loaded settings
  useEffect(() => {
    if (settings) {
      setLocalPath(settings[LOCAL_PATH_KEY] ?? "");
      setNavidromePath(settings[NAVIDROME_PATH_KEY] ?? "");
    }
  }, [settings]);

  async function handleSave() {
    const original = {
      [LOCAL_PATH_KEY]: settings?.[LOCAL_PATH_KEY] ?? "",
      [NAVIDROME_PATH_KEY]: settings?.[NAVIDROME_PATH_KEY] ?? "",
    };

    const toSave: Array<{ key: string; value: string }> = [];

    if (localPath !== original[LOCAL_PATH_KEY]) {
      toSave.push({ key: LOCAL_PATH_KEY, value: localPath });
    }
    if (navidromePath !== original[NAVIDROME_PATH_KEY]) {
      toSave.push({ key: NAVIDROME_PATH_KEY, value: navidromePath });
    }

    if (toSave.length === 0) {
      setToast("No changes to save.");
      return;
    }

    await Promise.all(toSave.map((entry) => updateSetting.mutateAsync(entry)));
    setToast("Path settings saved successfully.");
  }

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------

  return (
    <div className="space-y-6">
      {/* Back link */}
      <div>
        <Link
          to="/settings"
          className="inline-flex items-center gap-1.5 text-sm text-gray-500 hover:text-gray-700"
        >
          <ArrowLeft className="h-4 w-4" />
          Settings
        </Link>
      </div>

      <PageHeader
        title="Path Configuration"
        description="Map local library paths to Navidrome server paths for M3U export."
      />

      {isLoading && (
        <div className="flex justify-center py-12">
          <Spinner className="h-6 w-6 text-indigo-500" />
        </div>
      )}

      {isError && (
        <p className="rounded-md bg-red-50 p-4 text-sm text-red-700">
          Failed to load settings. Please try again.
        </p>
      )}

      {!isLoading && !isError && (
        <div className="overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm">
          <div className="divide-y divide-gray-100">
            {/* Local Library Path */}
            <div className="px-4 py-4">
              <label
                htmlFor="local-path"
                className="mb-1.5 block text-sm font-medium text-gray-700"
              >
                Local Library Path
              </label>
              <p className="mb-2 text-xs text-gray-400">
                The root directory of your local music library (e.g.{" "}
                <span className="font-mono">/music</span>).
              </p>
              <input
                id="local-path"
                type="text"
                value={localPath}
                onChange={(e) => setLocalPath(e.target.value)}
                placeholder="/music"
                className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm font-mono focus:border-indigo-400 focus:outline-none focus:ring-1 focus:ring-indigo-400"
              />
            </div>

            {/* Navidrome Path Prefix */}
            <div className="px-4 py-4">
              <label
                htmlFor="navidrome-path"
                className="mb-1.5 block text-sm font-medium text-gray-700"
              >
                Navidrome Path Prefix
              </label>
              <p className="mb-2 text-xs text-gray-400">
                The path prefix used by Navidrome to serve the same library (e.g.{" "}
                <span className="font-mono">/srv/music</span>).
              </p>
              <input
                id="navidrome-path"
                type="text"
                value={navidromePath}
                onChange={(e) => setNavidromePath(e.target.value)}
                placeholder="/srv/music"
                className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm font-mono focus:border-indigo-400 focus:outline-none focus:ring-1 focus:ring-indigo-400"
              />
            </div>
          </div>

          {/* Save button */}
          <div className="flex items-center justify-between border-t border-gray-100 bg-gray-50 px-4 py-3">
            <div className="flex items-center gap-3">
              <ScanLibraryButton />
              {toast && <InlineToast message={toast} onDismiss={() => setToast(null)} />}
            </div>
            <button
              type="button"
              onClick={() => void handleSave()}
              disabled={updateSetting.isPending}
              className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400 disabled:opacity-60"
            >
              <Save className="h-4 w-4" />
              {updateSetting.isPending ? "Saving..." : "Save"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
