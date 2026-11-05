import { render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { currentSession, endSession } from "../api/client";
import { ssoStartUrl } from "../pages/Login";
import { SsoComplete } from "../pages/SsoComplete";

const SESSION = {
  org: { id: "o", name: "Org", slug: "org" },
  user: { id: "u", email: "a@example.com", roles: ["member"] },
  access_token: "access",
  token_type: "Bearer",
  expires_in: 900,
  refresh_token: "refresh-token-value-1234",
  refresh_expires_in: 3600,
};

describe("single sign-on (US-27)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    endSession();
    window.history.replaceState(null, "", "/");
  });

  it("starts at the gateway with the organisation's slug", () => {
    expect(ssoStartUrl(" Acme-Corp ")).toBe("/v1/auth/sso/start?org=acme-corp");
  });

  it("trades the one-time code for a session once, and drops it from the address bar", async () => {
    window.history.replaceState(null, "", "/login/sso?code=one-time-code-value-123456");
    const fetchMock = vi
      .fn()
      .mockImplementation(() => Promise.resolve(new Response(JSON.stringify(SESSION), { status: 200 })));
    vi.stubGlobal("fetch", fetchMock);
    render(
      <StrictMode>
        <SsoComplete />
      </StrictMode>,
    );
    await waitFor(() => expect(currentSession()?.user.email).toBe("a@example.com"));
    expect(fetchMock).toHaveBeenCalledTimes(1); // a code works once: StrictMode must not spend it twice
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/auth/sso/exchange");
    expect(JSON.parse(String(init.body))).toEqual({ code: "one-time-code-value-123456" });
    expect(window.location.search).not.toContain("one-time-code");
  });

  it("explains a sign-in the gateway refused", async () => {
    window.history.replaceState(null, "", "/login/sso?error=sso_no_account");
    render(<SsoComplete />);
    expect(await screen.findByRole("alert")).toHaveTextContent("no account for you");
    expect(currentSession()).toBeNull();
  });
});
