// Gathering the files for a liner upload: from a file or folder picker, or from a drop.
// A file's path (its place in a dropped or picked folder) is kept apart from the File,
// because a File read from a dropped folder has no webkitRelativePath.

export type LinerCandidate = { file: File; path: string };

const byPath = (a: LinerCandidate, b: LinerCandidate) =>
  a.path < b.path ? -1 : a.path > b.path ? 1 : 0;

/** Folder files go in path order, subfolders included; loose files in name order. */
export function inPathOrder(candidates: LinerCandidate[]): LinerCandidate[] {
  return [...candidates].sort(byPath);
}

/** Files from a picker. A folder picker fills webkitRelativePath; a file picker leaves it "". */
export function fromPicker(files: File[]): LinerCandidate[] {
  return inPathOrder(files.map((file) => ({ file, path: file.webkitRelativePath || file.name })));
}

function readFile(entry: FileSystemFileEntry): Promise<File> {
  return new Promise((resolve, reject) => entry.file(resolve, reject));
}

// A directory reader hands out a few entries per call and [] at the end.
async function readAllEntries(directory: FileSystemDirectoryEntry): Promise<FileSystemEntry[]> {
  const reader = directory.createReader();
  const all: FileSystemEntry[] = [];
  for (;;) {
    const batch = await new Promise<FileSystemEntry[]>((resolve, reject) =>
      reader.readEntries(resolve, reject)
    );
    if (batch.length === 0) return all;
    all.push(...batch);
  }
}

async function walk(entry: FileSystemEntry, into: LinerCandidate[]): Promise<void> {
  if (entry.isFile) {
    const file = await readFile(entry as FileSystemFileEntry);
    into.push({ file, path: entry.fullPath.replace(/^\/+/, "") });
    return;
  }
  if (entry.isDirectory) {
    for (const child of await readAllEntries(entry as FileSystemDirectoryEntry)) {
      await walk(child, into);
    }
  }
}

/** What a drop carries, taken while the drop event is still being dispatched. */
export type DroppedThings = { entries: FileSystemEntry[]; loose: File[] };

// The browser empties dataTransfer once the event handler returns, so this must run
// synchronously inside the drop handler, before anything is awaited.
export function takeDropped(dataTransfer: DataTransfer): DroppedThings {
  const entries: FileSystemEntry[] = [];
  const loose: File[] = [];
  for (const item of Array.from(dataTransfer.items)) {
    if (item.kind !== "file") continue;
    const entry = item.webkitGetAsEntry();
    if (entry) {
      entries.push(entry);
      continue;
    }
    const file = item.getAsFile();
    if (file) loose.push(file);
  }
  return { entries, loose };
}

/** Every file in the dropped things, folders walked to the bottom, in path order. */
export async function readDropped(dropped: DroppedThings): Promise<LinerCandidate[]> {
  const found: LinerCandidate[] = dropped.loose.map((file) => ({ file, path: file.name }));
  for (const entry of dropped.entries) await walk(entry, found);
  return inPathOrder(found);
}
