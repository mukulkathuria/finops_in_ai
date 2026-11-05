import { useEffect, useRef, useState } from "react";

import { post, startSession } from "../api/client";
import type { Session } from "../api/types";
import { ErrorBanner, Loading } from "../components/ui";
import { navigate } from "../router";

const REASONS: Record<string, string> = {
  sso_expired: "The sign-in took too long, or was used already. Start it again.",
  sso_cancelled: "The sign-in was cancelled at your organisation's provider.",
  sso_failed: "Your organisation's provider could not confirm who you are.",
  sso_no_account: "There is no account for you in this organisation. Ask an admin to invite you.",
  sso_account_inactive: "Your account is not active.",
};

/** Where the provider's sign-in comes back to (US-27): the gateway's one-time code becomes the
 * session, once, and leaves the address bar at once; a refused sign-in shows its reason. */
export function SsoComplete() {
  const [params] = useState(() => new URLSearchParams(window.location.search));
  const [error, setError] = useState<unknown>(null);
  const exchanged = useRef(false);
  const code = params.get("code");
  const reason = params.get("error");

  useEffect(() => {
    window.history.replaceState(null, "", "/login/sso");
    if (!code || exchanged.current) return;
    exchanged.current = true; // the code works once: never trade it twice
    post<Session>("/v1/auth/sso/exchange", { code })
      .then((session) => {
        startSession(session);
        navigate("/");
      })
      .catch(setError);
  }, [code]);

  if (code && error === null) return <Loading />;
  return (
    <main className="narrow">
      <h1>Single sign-on</h1>
      {reason ? <p role="alert">{REASONS[reason] ?? "The sign-in did not complete."}</p> : null}
      <ErrorBanner error={error} />
      <p>
        <a href="/">Back to sign in</a>
      </p>
    </main>
  );
}
