// The per-browser resume key (module K; design question 2). One key serves every
// station-year, shared by every tab (D74).

export const KEY_NAME = "retrostation.listenerKey";

/**
 * @typedef {{
 *   getItem(name: string): string | null,
 *   setItem(name: string, value: string): void
 * }} KeyStorage
 */

const KEY_BYTE_COUNT = 16;
const KEY_PATTERN = /^[0-9a-f]{32}$/;

/**
 * @param {Uint8Array} keyBytes
 * @returns {string}
 */
function toHex(keyBytes) {
  let hex = "";
  for (const byte of keyBytes) hex += byte.toString(16).padStart(2, "0");
  return hex;
}

/**
 * Reads the saved key from `storage`, naming the `DOMException` a refused read raises. A
 * refused read is told apart from the "nothing saved yet" case, so the caller never then
 * tries a write against storage that has already shown itself to be unusable.
 *
 * @param {KeyStorage} storage
 * @returns {{refused: boolean, key: string | null}}
 */
function readKey(storage) {
  let stored;
  try {
    stored = storage.getItem(KEY_NAME);
  } catch (error) {
    if (error instanceof DOMException) return { refused: true, key: null };
    throw error;
  }
  const key = stored !== null && KEY_PATTERN.test(stored) ? stored : null;
  return { refused: false, key };
}

/**
 * Saves `key` to `storage`, naming the `DOMException` a refused write raises.
 *
 * @param {KeyStorage} storage
 * @param {string} key
 * @returns {boolean}
 */
function writeKey(storage, key) {
  try {
    storage.setItem(KEY_NAME, key);
    return true;
  } catch (error) {
    if (error instanceof DOMException) return false;
    throw error;
  }
}

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
  if (storage === null) return { key: toHex(randomBytes(KEY_BYTE_COUNT)), saved: false };

  const read = readKey(storage);
  if (read.key !== null) return { key: read.key, saved: true };

  const key = toHex(randomBytes(KEY_BYTE_COUNT));
  const saved = !read.refused && writeKey(storage, key);
  return { key, saved };
}
