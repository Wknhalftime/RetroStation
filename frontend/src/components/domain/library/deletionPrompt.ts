/** The confirmation shown before deleting missing rows: how many, and what it releases. */
export function deletionPrompt(files: number, matches: number): string {
  const rows = files === 1 ? "1 missing file" : `${files} missing files`;
  return `Delete ${rows} from the library? ${releaseNote(matches)}`;
}

function releaseNote(matches: number): string {
  if (matches === 0) return "No matches are released.";
  if (matches === 1) return "1 match is released: that broadcast song goes back to review.";
  return `${matches} matches are released: those broadcast songs go back to review.`;
}
