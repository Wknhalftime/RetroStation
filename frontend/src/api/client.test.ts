// @vitest-environment jsdom
import { describe, it, expect, beforeEach, vi } from "vitest";
import {
  apiFetch,
  EmptyResponseError,
  ApiError,
} from "./client";

function mockResponse(
  status: number,
  body: string,
  headers: Record<string, string> = {},
): Response {
  // The Response constructor forbids a non-null body for 204/205 per the
  // Fetch spec. Build a Response with null body, then stub .text() so the
  // 204-whitespace proxy-safety test can still observe "what if a body
  // arrived anyway?" behaviour.
  if (status === 204 || status === 205) {
    const res = new Response(null, { status, headers });
    vi.spyOn(res, "text").mockResolvedValue(body);
    return res;
  }
  return new Response(body, { status, headers });
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("apiFetch — parseJsonBody", () => {
  it("returns parsed JSON for a 200 with a JSON body", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      mockResponse(200, JSON.stringify({ id: "abc" }), {
        "content-type": "application/json",
      }),
    );
    const data = await apiFetch<{ id: string }>("/x");
    expect(data).toEqual({ id: "abc" });
  });

  it("returns undefined for a 204 No Content (DELETE/PUT mutation endpoints)", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(mockResponse(204, ""));
    const data = await apiFetch<void>("/x", { method: "DELETE" });
    expect(data).toBeUndefined();
  });

  it("returns undefined for a 205 Reset Content", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(mockResponse(205, ""));
    const data = await apiFetch<void>("/x");
    expect(data).toBeUndefined();
  });

  it("does not read the body on 204 even if whitespace arrives (proxy safety)", async () => {
    // 204 short-circuits before .text(), so whitespace-body proxies can't
    // re-introduce the JSON.parse error this handling was added to fix.
    vi.spyOn(globalThis, "fetch").mockResolvedValue(mockResponse(204, "\n"));
    await expect(apiFetch<void>("/x", { method: "DELETE" })).resolves.toBeUndefined();
  });

  it("throws EmptyResponseError when a 200 returns an empty body (backend contract violation)", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(mockResponse(200, ""));
    await expect(apiFetch<{ id: string }>("/x")).rejects.toBeInstanceOf(
      EmptyResponseError,
    );
  });

  it("throws EmptyResponseError when a 200 returns whitespace only", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(mockResponse(200, "\n  \t"));
    await expect(apiFetch<{ id: string }>("/x")).rejects.toBeInstanceOf(
      EmptyResponseError,
    );
  });

  it("EmptyResponseError is an ApiError with the originating status", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(mockResponse(200, ""));
    try {
      await apiFetch<{ id: string }>("/x");
      expect.fail("expected EmptyResponseError");
    } catch (err) {
      expect(err).toBeInstanceOf(ApiError);
      expect((err as EmptyResponseError).status).toBe(200);
      expect((err as EmptyResponseError).name).toBe("EmptyResponseError");
    }
  });

  it("ignores a Content-Length: 0 header on a 200 and defers to the body", async () => {
    // Proxies can emit Content-Length: 0 incorrectly; the body text is
    // authoritative. A 200 with a real JSON body must still parse.
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      mockResponse(200, JSON.stringify({ ok: true }), {
        "content-length": "0",
      }),
    );
    const data = await apiFetch<{ ok: boolean }>("/x");
    expect(data).toEqual({ ok: true });
  });
});
