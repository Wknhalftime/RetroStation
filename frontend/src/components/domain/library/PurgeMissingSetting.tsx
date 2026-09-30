import { useSettings, useUpdateSetting } from "@/api/settings";

const KEY = "library.purge_missing";
const HELP_ID = "purge-missing-help";

/** Setting library.purge_missing: whether a full scan deletes missing files nothing replaces. */
export function PurgeMissingSetting() {
  const { data } = useSettings();
  const update = useUpdateSetting();
  const value = data?.[KEY] === "after_scan" ? "after_scan" : "never";
  return (
    <div>
      <label className="flex items-center gap-2 text-sm text-gray-700">
        After a full scan
        <select
          aria-label="After a full scan"
          aria-describedby={HELP_ID}
          value={value}
          disabled={update.isPending}
          onChange={(e) => update.mutate({ key: KEY, value: e.target.value })}
          className="rounded-md border border-gray-300 px-2 py-1 text-sm"
        >
          <option value="never">keep missing files</option>
          <option value="after_scan">
            permanently delete missing files no copy on disk replaces
          </option>
        </select>
      </label>
      <p id={HELP_ID} className="mt-1 text-xs text-gray-500">
        Deleting runs after every full scan and cannot be undone. The plays matched to a deleted
        file go back to review.
      </p>
    </div>
  );
}
