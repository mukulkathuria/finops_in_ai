import { type FormEvent, useState } from "react";

import { api } from "../api/client";
import type {
  Budget,
  BudgetAlert,
  BudgetSettings,
  ListOf,
  Member,
  Period,
  Scope,
  Team,
  Unit,
} from "../api/types";
import { ErrorBanner, Field, Loading, Usage, useLoad } from "../components/ui";
import { amount, parseAmount, shortId, when } from "../format";

function SetLimit({ admin, teams, onSaved }: { admin: boolean; teams: Team[]; onSaved: () => void }) {
  const [scope, setScope] = useState<Scope>(admin ? "team" : "member");
  const [team, setTeam] = useState(teams[0]?.id ?? "");
  const [user, setUser] = useState("");
  const [period, setPeriod] = useState<Period>("month");
  const [unit, setUnit] = useState<Unit>("usd");
  const [limit, setLimit] = useState("");
  const [softPct, setSoftPct] = useState("80");
  const [reason, setReason] = useState("");
  const [error, setError] = useState<unknown>(null);
  const members = useLoad(
    () =>
      scope === "member" && team
        ? api<ListOf<Member>>("GET", `/v1/admin/teams/${team}/members`)
        : Promise.resolve({ data: [] }),
    [scope, team],
  );
  const parsed = parseAmount(limit, unit);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (parsed === null) return;
    setError(null);
    try {
      await api("PUT", "/v1/admin/budgets", {
        scope_type: scope,
        team_id: scope === "org" ? null : team,
        user_id: scope === "member" ? user : null,
        period,
        unit,
        limit_amount: parsed,
        soft_pct: Number(softPct),
        reason: reason || null,
      });
      setLimit("");
      setReason("");
      onSaved();
    } catch (err) {
      setError(err);
    }
  };

  return (
    <form className="panel" onSubmit={(e) => void submit(e)}>
      <h2>Set a limit</h2>
      <div className="row">
        <Field label="Scope">
          <select value={scope} onChange={(e) => setScope(e.target.value as Scope)}>
            {admin ? <option value="org">Organisation</option> : null}
            {admin ? <option value="team">Team</option> : null}
            <option value="member">Member</option>
          </select>
        </Field>
        {scope !== "org" ? (
          <Field label="Team">
            <select value={team} onChange={(e) => setTeam(e.target.value)} required>
              {teams.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </select>
          </Field>
        ) : null}
        {scope === "member" ? (
          <Field label="Member">
            <select value={user} onChange={(e) => setUser(e.target.value)} required>
              <option value="">Choose…</option>
              {(members.data?.data ?? []).map((m) => (
                <option key={m.user_id} value={m.user_id}>
                  {m.email}
                </option>
              ))}
            </select>
          </Field>
        ) : null}
        <Field label="Period">
          <select value={period} onChange={(e) => setPeriod(e.target.value as Period)}>
            <option value="day">Day</option>
            <option value="week">Week</option>
            <option value="month">Month</option>
          </select>
        </Field>
        <Field label="Unit">
          <select value={unit} onChange={(e) => setUnit(e.target.value as Unit)}>
            <option value="usd">USD</option>
            <option value="tokens">Tokens</option>
            <option value="images">Images</option>
          </select>
        </Field>
        <Field label={unit === "usd" ? "Limit (USD)" : "Limit"}>
          <input value={limit} onChange={(e) => setLimit(e.target.value)} required inputMode="decimal" aria-invalid={limit !== "" && parsed === null} />
        </Field>
        <Field label="Alert at (%)">
          <input type="number" min={1} max={100} value={softPct} onChange={(e) => setSoftPct(e.target.value)} />
        </Field>
        <Field label="Reason">
          <input value={reason} onChange={(e) => setReason(e.target.value)} maxLength={500} />
        </Field>
      </div>
      <ErrorBanner error={error} />
      <button type="submit" disabled={parsed === null}>
        Save limit
      </button>
    </form>
  );
}

function Settings() {
  const settings = useLoad(() => api<BudgetSettings>("GET", "/v1/admin/budget-settings"), []);
  const [error, setError] = useState<unknown>(null);
  const update = async (body: Partial<BudgetSettings>) => {
    setError(null);
    try {
      await api("PUT", "/v1/admin/budget-settings", body);
      settings.reload();
    } catch (err) {
      setError(err);
    }
  };
  if (!settings.data) return <ErrorBanner error={settings.error} />;
  return (
    <div className="panel">
      <h2>Settings</h2>
      <p>
        Periods use <strong>{settings.data.timezone}</strong>. Member limits may{" "}
        {settings.data.allocation === "strict" ? "not " : ""}sum above the team cap (
        {settings.data.allocation} allocation).
      </p>
      <button
        type="button"
        onClick={() =>
          void update({ allocation: settings.data?.allocation === "strict" ? "overcommit" : "strict" })
        }
      >
        Switch to {settings.data.allocation === "strict" ? "overcommit" : "strict"} allocation
      </button>
      <ErrorBanner error={error} />
    </div>
  );
}

export function Budgets({ admin }: { admin: boolean }) {
  const budgets = useLoad(() => api<ListOf<Budget>>("GET", "/v1/admin/budgets"), []);
  const alerts = useLoad(() => api<ListOf<BudgetAlert>>("GET", "/v1/admin/budget-alerts"), []);
  const teams = useLoad(() => api<ListOf<Team>>("GET", "/v1/admin/teams"), []);
  const [error, setError] = useState<unknown>(null);
  const teamName = (id: string | null) =>
    teams.data?.data.find((t) => t.id === id)?.name ?? shortId(id);

  const remove = async (budget: Budget) => {
    setError(null);
    try {
      await api("DELETE", `/v1/admin/budgets/${budget.id}`);
      budgets.reload();
    } catch (err) {
      setError(err);
    }
  };

  if (budgets.loading && budgets.data === null) return <Loading />;
  return (
    <section>
      <h1>Budgets</h1>
      <ErrorBanner error={budgets.error ?? error} />
      <table>
        <thead>
          <tr>
            <th scope="col">Scope</th>
            <th scope="col">Period</th>
            <th scope="col">Used</th>
            <th scope="col">Limit</th>
            <th scope="col">Remaining</th>
            <th scope="col" />
          </tr>
        </thead>
        <tbody>
          {(budgets.data?.data ?? []).map((b) => (
            <tr key={b.id}>
              <td>
                {b.scope_type === "org" ? "Organisation" : teamName(b.team_id)}
                {b.scope_type === "member" ? ` · member ${shortId(b.user_id)}` : ""}
              </td>
              <td>{b.period}</td>
              <td>
                <Usage used={b.committed + b.reserved} limit={b.limit_amount + b.granted} label="budget used" />{" "}
                {amount(b.committed, b.unit)}
              </td>
              <td>{amount(b.limit_amount, b.unit)}</td>
              <td>{amount(b.remaining, b.unit)}</td>
              <td>
                {admin || b.scope_type === "member" ? (
                  <button type="button" onClick={() => void remove(b)}>
                    Remove
                  </button>
                ) : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {teams.data ? <SetLimit admin={admin} teams={teams.data.data} onSaved={budgets.reload} /> : null}
      {admin ? <Settings /> : null}
      <h2>Alerts</h2>
      {(alerts.data?.data ?? []).length === 0 ? <p>No soft-threshold alerts.</p> : null}
      <ul>
        {(alerts.data?.data ?? []).map((a) => (
          <li key={a.id}>
            {when(a.created_at)}: {a.scope_type === "org" ? "Organisation" : teamName(a.team_id)} {a.period}{" "}
            budget reached {amount(a.committed, a.unit)} of {amount(a.limit_amount, a.unit)}
          </li>
        ))}
      </ul>
    </section>
  );
}
