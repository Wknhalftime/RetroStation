// Module K: the per-browser resume key (plan-f2.md Task 2, design question 2; traceability-f2.md).
import { describe, expect, it } from "vitest";
import { KEY_NAME, listenerKey } from "../../backend/web/radio/listenerKey.js";
import { FakeStorage, bytes, type StorageMode } from "./fakes";

const FROM_BYTES_16 = "000102030405060708090a0b0c0d0e0f";
const A_KEY = /^[0-9a-f]{32}$/;

function recordingBytes() {
  const counts: number[] = [];
  const randomBytes = (count: number): Uint8Array => {
    counts.push(count);
    return bytes(count);
  };
  return { counts, randomBytes };
}

describe("the listener key", () => {
  // K1 — D74
  it("the first visit makes a key and saves it", () => {
    const storage = new FakeStorage();
    const random = recordingBytes();
    expect(listenerKey(storage, random.randomBytes)).toEqual({ key: FROM_BYTES_16, saved: true });
    expect(random.counts).toEqual([16]);
    expect(KEY_NAME).toBe("retrostation.listenerKey");
    expect(storage.values.get(KEY_NAME)).toBe(FROM_BYTES_16);
  });

  // K2 — D74; D11 (one key per browser: the bookmark survives a reload)
  it("a later visit reuses the saved key", () => {
    const storage = new FakeStorage();
    storage.setItem(KEY_NAME, "0123456789abcdef0123456789abcdef");
    const random = recordingBytes();
    expect(listenerKey(storage, random.randomBytes)).toEqual({
      key: "0123456789abcdef0123456789abcdef",
      saved: true,
    });
    expect(random.counts).toEqual([]);
  });

  // K3 — error handling (a stored value is not trusted)
  it.each([
    ["empty", ""],
    ["200 x", "x".repeat(200)],
    ["markup", "<b>x</b>"],
  ])("a saved value that is not a key is replaced [%s]", (_label, stored) => {
    const storage = new FakeStorage();
    storage.setItem(KEY_NAME, stored);
    const result = listenerKey(storage, bytes);
    expect(result).toEqual({ key: FROM_BYTES_16, saved: true });
    expect(storage.values.get(KEY_NAME)).toBe(FROM_BYTES_16);
  });

  // K4 — D28 (no key, no bookmark and no events: the page always has one); D74
  it.each<[string, StorageMode | null]>([
    ["failGet", "failGet"],
    ["failSet", "failSet"],
    ["no storage", null],
  ])("refused storage gives a key for this page only [%s]", (_label, mode) => {
    const storage = mode === null ? null : new FakeStorage(mode);
    const result = listenerKey(storage, bytes);
    expect(result.key).toMatch(A_KEY);
    expect(result.key).toBe(FROM_BYTES_16); // random per browser, never a shared constant (SF4)
    expect(result.saved).toBe(false);
  });
});
