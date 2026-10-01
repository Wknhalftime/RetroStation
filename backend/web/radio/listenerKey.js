// The per-browser resume key (module K; design question 2). One key serves every
// station-year, shared by every tab (D74). Stub: typed surface only, filled in later.

export const KEY_NAME = "retrostation.listenerKey";

/**
 * @typedef {{
 *   getItem(name: string): string | null,
 *   setItem(name: string, value: string): void
 * }} KeyStorage
 */

/**
 * Reads the saved key, or makes and saves one when it is missing or not a key (32 lowercase
 * hex characters). When `storage` is `null`, or a read or write is refused, a key is made for
 * this page load only and `saved` is `false`.
 *
 * @param {KeyStorage | null} storage
 * @param {(count: number) => Uint8Array} randomBytes
 * @returns {{key: string, saved: boolean}}
 */
export function listenerKey(storage, randomBytes) {
  void storage;
  void randomBytes;
  return { key: "", saved: false };
}
