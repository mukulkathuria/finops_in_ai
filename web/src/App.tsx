import { useEffect, useState } from "react";

import { currentSession, hasStoredSession, onSessionChange, refresh, signOut } from "./api/client";
import type { Session } from "./api/types";
import { Loading } from "./components/ui";
import { AcceptInvite } from "./pages/AcceptInvite";
import { Audit } from "./pages/Audit";
import { Budgets } from "./pages/Budgets";
import { Login } from "./pages/Login";
import { MyUsagePage } from "./pages/MyUsage";
import { Requests } from "./pages/Requests";
import { SavingsPage } from "./pages/Savings";
import { SsoComplete } from "./pages/SsoComplete";
import { Teams } from "./pages/Teams";
import { Link, navigate, usePath } from "./router";

function useSession(): { session: Session | null; restoring: boolean } {
  const [session, setSession] = useState(currentSession());
  const [restoring, setRestoring] = useState(session === null && hasStoredSession());
  useEffect(() => onSessionChange(setSession), []);
  useEffect(() => {
    if (!restoring) return;
    refresh()
      .catch(() => null)
      .finally(() => setRestoring(false));
  }, [restoring]);
  return { session, restoring };
}

export function App() {
  const { session, restoring } = useSession();
  const path = usePath();
  if (path === "/accept-invite") return <AcceptInvite />;
  if (path === "/login/sso") return <SsoComplete />;
  if (restoring) return <Loading />;
  if (session === null) return <Login />;

  const roles = new Set(session.user.roles);
  const admin = roles.has("org_admin");
  const lead = roles.has("team_lead");
  const pages = [
    { to: "/", label: "My usage", show: true, page: <MyUsagePage /> },
    {
      to: "/budgets",
      label: "Budgets",
      show: admin || lead || roles.has("finance"),
      page: <Budgets admin={admin} />,
    },
    { to: "/requests", label: "Budget requests", show: true, page: <Requests /> },
    {
      to: "/savings",
      label: "Savings",
      show: admin || roles.has("finance"),
      page: <SavingsPage />,
    },
    { to: "/teams", label: "Teams and keys", show: admin || lead, page: <Teams admin={admin} /> },
    { to: "/audit", label: "Audit log", show: admin || roles.has("auditor"), page: <Audit /> },
  ].filter((p) => p.show);
  const active = pages.find((p) => p.to === path) ?? pages[0];

  return (
    <div className="shell">
      <header>
        <strong>AI FinOps Gateway</strong>
        <span className="muted">{session.org.name}</span>
        <nav aria-label="Main">
          {pages.map((p) => (
            <Link key={p.to} to={p.to}>
              {p.label}
            </Link>
          ))}
        </nav>
        <span className="muted">{session.user.email}</span>
        <button
          type="button"
          onClick={() => {
            void signOut().then(() => navigate("/"));
          }}
        >
          Sign out
        </button>
      </header>
      <main>{active?.page}</main>
    </div>
  );
}
