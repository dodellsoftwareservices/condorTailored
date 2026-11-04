/**
 * @vitest-environment jsdom
 *
 * apiFetch's error-unwrapping, the two shapes `detail` actually arrives in.
 *
 * Most routes send a bare string `detail`; the equities routes (and the
 * rollout gate in front of them) send `{code, message}` instead, so a caller
 * can branch on a stable code rather than parsing prose out of a sentence.
 * Before this file existed, an object `detail` produced the literal message
 * text "[object Object]" — `new Error(detail)` stringifying the object
 * rather than reading its `message` field — which nobody had a test for
 * because every existing caller only ever sent the string shape.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { api, errorCode, errorStatus } from "./api";

function serveOnce(status: number, body: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({ ok: false, status, json: async () => body }) as unknown as Response),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("apiFetch error unwrapping", () => {
  it("reads a string detail as the message, same as before this fix", async () => {
    serveOnce(503, { detail: "server offline" });

    await expect(api.getServers()).rejects.toThrow("server offline");
    try {
      await api.getServers();
    } catch (e) {
      expect(errorStatus(e)).toBe(503);
      expect(errorCode(e)).toBeUndefined(); // the old shape carries no code
    }
  });

  it("reads {code, message} as the real message, not '[object Object]'", async () => {
    serveOnce(503, {
      detail: { code: "equity_feature_disabled", message: "Paper Markets is not enabled." },
    });

    await expect(api.getServers()).rejects.toThrow("Paper Markets is not enabled.");
    try {
      await api.getServers();
    } catch (e) {
      expect((e as Error).message).not.toContain("object Object");
      expect(errorStatus(e)).toBe(503);
      expect(errorCode(e)).toBe("equity_feature_disabled");
    }
  });

  it("falls back to a generic message when detail is missing or empty", async () => {
    serveOnce(500, {});

    await expect(api.getServers()).rejects.toThrow("Request failed: 500");
    try {
      await api.getServers();
    } catch (e) {
      expect(errorCode(e)).toBeUndefined();
    }
  });

  it("falls back to a generic message when an object detail has no message field", async () => {
    serveOnce(502, { detail: { code: "equity_order_too_large" } });

    try {
      await api.getServers();
      throw new Error("expected apiFetch to reject");
    } catch (e) {
      expect((e as Error).message).toBe("Request failed: 502");
      expect(errorCode(e)).toBe("equity_order_too_large"); // the code still reads, even with no message
    }
  });
});
