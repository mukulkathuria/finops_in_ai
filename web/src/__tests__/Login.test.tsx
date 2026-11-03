import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { currentSession, endSession } from "../api/client";
import { Login } from "../pages/Login";

describe("sign in", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    endSession();
  });

  it("starts a session with the returned tokens", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          org: { id: "o", name: "Org", slug: "org" },
          user: { id: "u", email: "a@example.com", roles: ["org_admin"] },
          access_token: "access",
          token_type: "Bearer",
          expires_in: 900,
          refresh_token: "refresh-token-value-1234",
          refresh_expires_in: 3600,
        }),
        { status: 200 },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<Login />);
    await userEvent.type(screen.getByLabelText("Email"), "a@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "Correct-Horse-Battery-9!");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/auth/login");
    expect(JSON.parse(String(init.body))).toEqual({ email: "a@example.com", password: "Correct-Horse-Battery-9!" });
    expect(currentSession()?.user.email).toBe("a@example.com");
  });

  it("shows the server's message when sign-in fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({ error: { code: "invalid_credentials", message: "email or password is wrong" } }),
          { status: 401 },
        ),
      ),
    );
    render(<Login />);
    await userEvent.type(screen.getByLabelText("Email"), "a@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "nope");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("email or password is wrong");
    expect(currentSession()).toBeNull();
  });
});
