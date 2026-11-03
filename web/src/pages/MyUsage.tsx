import { type FormEvent, useState } from "react";

import { api } from "../api/client";
import type { MyBudget, MyUsage } from "../api/types";
import { ErrorBanner, Field, Loading, Notice, Usage, useLoad } from "../components/ui";
import { amount, parseAmount, usd } from "../format";

function AskForMore({ budget, onDone }: { budget: MyBudget; onDone: () => void }) {
  const [value, setValue] = useState("");
  const [reason, setReason] = useState("");
  const [error, setError] = useState<unknown>(null);
  const parsed = parseAmount(value, budget.unit);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (parsed === null || parsed <= 0) return;
    try {
      await api("POST", "/v1/budget-requests", { budget_id: budget.id, amount: parsed, reason });
      onDone();
    } catch (err) {
      setError(err);
    }
  };

  return (
    <form className="inline" onSubmit={(e) => void submit(e)}>
      <Field label={budget.unit === "usd" ? "Extra (USD)" : `Extra ${budget.unit}`}>
        <input value={value} onChange={(e) => setValue(e.target.value)} required inputMode="decimal" />
      </Field>
      <Field label="Reason">
        <input value={reason} onChange={(e) => setReason(e.target.value)} required maxLength={1000} />
      </Field>
      <button type="submit" disabled={parsed === null || parsed <= 0}>
        Ask
      </button>
      <ErrorBanner error={error} />
    </form>
  );
}

export function MyUsagePage() {
  const usage = useLoad(() => api<MyUsage>("GET", "/v1/me/usage"), []);
  const [asking, setAsking] = useState<string | null>(null);
  const [sent, setSent] = useState(false);

  if (usage.loading && usage.data === null) return <Loading />;
  const report = usage.data;
  return (
    <section>
      <h1>My usage</h1>
      <ErrorBanner error={usage.error} />
      {report ? (
        <>
          <p className="muted">Periods follow the {report.timezone} timezone.</p>
          <div className="cards">
            {report.usage.map((p) => (
              <article key={p.period} className="card">
                <h2>This {p.period}</h2>
                <p className="big">{usd(p.cost_micro_usd)}</p>
                <p>
                  {p.requests.toLocaleString("en-US")} requests ·{" "}
                  {(p.input_tokens + p.output_tokens).toLocaleString("en-US")} tokens
                </p>
              </article>
            ))}
          </div>
          <h2>My budgets</h2>
          {sent ? <Notice>Request sent. Your team lead will decide.</Notice> : null}
          {report.budgets.length === 0 ? (
            <p>No personal limit is set; your team and organisation budgets still apply.</p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th scope="col">Period</th>
                  <th scope="col">Used</th>
                  <th scope="col">Limit</th>
                  <th scope="col">Remaining</th>
                  <th scope="col" />
                </tr>
              </thead>
              <tbody>
                {report.budgets.map((b) => (
                  <tr key={b.id}>
                    <td>{b.period}</td>
                    <td>
                      <Usage used={b.committed + b.reserved} limit={b.limit_amount + b.granted} label={`${b.period} budget used`} />{" "}
                      {amount(b.committed, b.unit)}
                    </td>
                    <td>
                      {amount(b.limit_amount, b.unit)}
                      {b.granted ? ` + ${amount(b.granted, b.unit)} this ${b.period}` : ""}
                    </td>
                    <td>{amount(b.remaining, b.unit)}</td>
                    <td>
                      {asking === b.id ? (
                        <AskForMore
                          budget={b}
                          onDone={() => {
                            setAsking(null);
                            setSent(true);
                          }}
                        />
                      ) : (
                        <button type="button" onClick={() => setAsking(b.id)}>
                          Ask for more
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      ) : null}
    </section>
  );
}
