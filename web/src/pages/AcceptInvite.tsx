import { type FormEvent, useEffect, useState } from "react";

import { post, startSession } from "../api/client";
import type { Session } from "../api/types";
import { ErrorBanner, Field } from "../components/ui";
import { navigate } from "../router";

/** The invitation token travels in the URL fragment (never sent to servers or referrers); it is
 * read once and removed from the address bar. */
function takeToken(): string {
  const token = new URLSearchParams(window.location.hash.slice(1)).get("token") ?? "";
  if (window.location.hash) window.history.replaceState(null, "", window.location.pathname);
  return token;
}

export function AcceptInvite() {
  const [token] = useState(takeToken);
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [mismatch, setMismatch] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => setMismatch(confirm !== "" && confirm !== password), [password, confirm]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (password !== confirm) return;
    setBusy(true);
    setError(null);
    try {
      startSession(await post<Session>("/v1/auth/invitations/accept", { token, password }));
      navigate("/");
    } catch (err) {
      setError(err);
      setBusy(false);
    }
  };

  if (!token) {
    return (
      <main className="narrow">
        <h1>Invitation link incomplete</h1>
        <p>Open the full link from your invitation e-mail, or ask your admin for a new one.</p>
      </main>
    );
  }
  return (
    <main className="narrow">
      <h1>Join your organisation</h1>
      <form onSubmit={(e) => void submit(e)}>
        <Field label="Choose a password">
          <input
            type="password"
            name="new-password"
            autoComplete="new-password"
            minLength={12}
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </Field>
        <Field label="Repeat the password">
          <input
            type="password"
            name="confirm-password"
            autoComplete="new-password"
            required
            aria-invalid={mismatch}
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
          />
        </Field>
        {mismatch ? <p className="error">The passwords differ.</p> : null}
        <ErrorBanner error={error} />
        <button type="submit" disabled={busy || mismatch}>
          {busy ? "Joining…" : "Join"}
        </button>
      </form>
    </main>
  );
}
