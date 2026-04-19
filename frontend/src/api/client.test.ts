// @vitest-environment jsdom
import { describe, it, expect, beforeEach, vi } from "vitest";
import {
  apiFetch,
  EmptyResponseError,
  ApiError,
} from "./client";

interface MockResponse {
  response: Response;
  textSpy: ReturnType<typeof vi.spyOn>;
}

function mockResponse(
  status: number,
  body: string,
  headers: Record<string, string> = {},
): MockResponse {
  // The Response constructor forbids a non-null body for 204/205 per the
  // Fetch spec. For those statuses, build a Response with null body then
  // stub .text() so callers can still assert on read-or-not behaviour.
  const response =
    status === 204 || status === 205
      ? new Response(null, { status, headers })
      : new Response(body, { status, headers });
  const textSpy =
    status === 204 || status === 205
      ? vi.spyOn(response, "text").mockResolvedValue(body)
      : vi.spyOn(response, "text");
  return { response, textSpy };
}

function mockFetch(mock: MockResponse): void {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(mock.response);
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("apiFetch — parseJsonBody", () => {
  it("returns parsed JSON for a 200 with a JSON body", async () => {
    mockFetch(mockResponse(200, JSON.stringify({ id: "abc" }), {
      "content-type": "application/json",
    }));
    const data = await apiFetch<{ id: string }>("/x");
    expect(data).toEqual({ id: "abc" });
  });

  it("returns undefined for a 204 No Content (DELETE/PUT mutation endpoints)", async () => {
    mockFetch(mockResponse(204, ""));
    const data = await apiFetch<void>("/x", { method: "DELETE" });
    expect(data).toBeUndefined();
  });

  it("returns undefined for a 205 Reset Content", async () => {
    mockFetch(mockResponse(205, ""));
    const data = await apiFetch<void>("/x");
    expect(data).toBeUndefined();
  });

  it("does not read the body on 204 even if whitespace arrives (proxy safety)", async () => {
    // Guards the regression from commit 5a7b85f: proxies emitting "\n" on
    // 204 must not re-enter JSON.parse. Short-circuit happens BEFORE
    // .text() is called, so asserting the spy was never invoked is the
    // load-bearing check.
    const mock = mockResponse(204, "\n");
    mockFetch(mock);
    await expect(
      apiFetch<void>("/x", { method: "DELETE" }),
    ).resolves.toBeUndefined();
    expect(mock.textSpy).not.toHaveBeenCalled();
  });

  it("throws EmptyResponseError when a 200 returns an empty body (backend contract violation)", async () => {
    mockFetch(mockResponse(200, ""));
    await expect(apiFetch<{ id: string }>("/x")).rejects.toBeInstanceOf(
      EmptyResponseError,
    );
  });

  it("throws EmptyResponseError when a 200 returns whitespace only", async () => {
    mockFetch(mockResponse(200, "\n  \t"));
    await expect(apiFetch<{ id: string }>("/x")).rejects.toBeInstanceOf(
      EmptyResponseError,
    );
  });

  it("EmptyResponseError is an ApiError with the originating status", async () => {
    mockFetch(mockResponse(200, ""));
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
    mockFetch(mockResponse(200, JSON.stringify({ ok: true }), {
      "content-length": "0",
    }));
    const data = await apiFetch<{ ok: boolean }>("/x");
    expect(data).toEqual({ ok: true });
  });
});
