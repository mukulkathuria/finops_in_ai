import { type FormEvent, useState } from "react";

import { post, startSession } from "../api/client";
import type { Session } from "../api/types";
import { ErrorBanner, Field } from "../components/ui";

export function Login() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      startSession(await post<Session>("/v1/auth/login", { email, password }));
    } catch (err) {
      setError(err);
      setBusy(false);
    }
  };

  return (
    <main className="narrow">
      <h1>Sign in</h1>
      <form onSubmit={(e) => void submit(e)}>
        <Field label="Email">
          <input
            type="email"
            name="email"
            autoComplete="username"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </Field>
        <Field label="Password">
          <input
            type="password"
            name="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </Field>
        <ErrorBanner error={error} />
        <button type="submit" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}
