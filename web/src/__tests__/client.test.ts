import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, api, currentSession, endSession, startSession } from "../api/client";
import type { Session } from "../api/types";

function session(access: string, refresh: string, expiresIn = 900): Session {
  return {
    org: { id: "o", name: "Org", slug: "org" },
    user: { id: "u", email: "u@example.com", roles: ["member"] },
    access_token: access,
    token_type: "Bearer",
    expires_in: expiresIn,
    refresh_token: refresh,
    refresh_expires_in: 3600,
  };
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("api client", () => {
  beforeEach(() => endSession());
  afterEach(() => vi.unstubAllGlobals());

  it("shares one refresh between concurrent calls (refresh tokens are single-use)", async () => {
    startSession(session("old-access", "refresh-1", 1)); // already inside the early-refresh window
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/v1/auth/refresh") return json(200, session("new-access", "refresh-2"));
      return json(200, { ok: true });
    });
    vi.stubGlobal("fetch", fetchMock);

    await Promise.all([api("GET", "/v1/me/usage"), api("GET", "/v1/admin/teams"), api("GET", "/v1/me/usage")]);

    const refreshes = fetchMock.mock.calls.filter(([input]) => String(input) === "/v1/auth/refresh");
    expect(refreshes).toHaveLength(1);
    expect(currentSession()?.access_token).toBe("new-access");
    expect(window.sessionStorage.getItem("finops.refresh")).toBe("refresh-2");
  });

  it("retries once after a 401 with a refreshed token, then surfaces API errors", async () => {
    startSession(session("stale", "refresh-1"));
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(json(401, { error: { code: "invalid_token", message: "expired" } }))
      .mockResolvedValueOnce(json(200, session("fresh", "refresh-2")))
      .mockResolvedValueOnce(
        json(429, { error: { code: "budget_exceeded", message: "team budget exhausted", param: null } }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const failure = await api("POST", "/v1/chat/completions", { model: "m" }).catch((e: unknown) => e);
    expect(failure).toBeInstanceOf(ApiError);
    expect(failure).toMatchObject({ status: 429, code: "budget_exceeded", message: "team budget exhausted" });
    const retried = fetchMock.mock.calls[2] as [string, RequestInit];
    expect((retried[1].headers as Record<string, string>).authorization).toBe("Bearer fresh");
  });

  it("signs out when the refresh token is refused", async () => {
    startSession(session("stale", "refresh-1"));
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(json(401, { error: { code: "invalid_token", message: "expired" } }))
        .mockResolvedValueOnce(json(401, { error: { code: "refresh_reused", message: "revoked" } }))
        .mockResolvedValueOnce(json(401, { error: { code: "missing_token", message: "missing" } })),
    );
    await expect(api("GET", "/v1/me/usage")).rejects.toBeInstanceOf(ApiError);
    expect(currentSession()).toBeNull();
    expect(window.sessionStorage.getItem("finops.refresh")).toBeNull();
  });
});
