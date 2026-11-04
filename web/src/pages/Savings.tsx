import { useState } from "react";

import { api } from "../api/client";
import type { SavingsReport, SavingsRow } from "../api/types";
import { ErrorBanner, Field, Loading, useLoad } from "../components/ui";
import { localDay, share, usd } from "../format";

type GroupBy = "team" | "model" | "day";

const DAY_MS = 86_400_000;
const HEADINGS: Record<GroupBy, string> = { team: "Team", model: "Model", day: "Day" };

function Totals({ totals }: { totals: SavingsRow }) {
  return (
    <div className="cards">
      <article className="card">
        <h2>Without optimizations</h2>
        <p className="big">{usd(totals.baseline_micro_usd)}</p>
        <p>{totals.requests.toLocaleString("en-US")} requests at the baseline model</p>
      </article>
      <article className="card">
        <h2>Actual cost</h2>
        <p className="big">{usd(totals.cost_micro_usd + totals.overhead_micro_usd)}</p>
        <p>
          {usd(totals.cost_micro_usd)} providers + {usd(totals.overhead_micro_usd)} gateway work
        </p>
      </article>
      <article className="card">
        <h2>Saved</h2>
        <p className="big">{usd(totals.savings_micro_usd)}</p>
        <p>{share(totals.savings_micro_usd, totals.baseline_micro_usd)} of the baseline</p>
      </article>
    </div>
  );
}

export function SavingsPage() {
  const today = new Date();
  const [start, setStart] = useState(localDay(new Date(today.getFullYear(), today.getMonth(), 1)));
  const [end, setEnd] = useState(localDay(new Date(today.getTime() + DAY_MS)));
  const [groupBy, setGroupBy] = useState<GroupBy>("team");
  const query = new URLSearchParams({ start, end, group_by: groupBy });
  const report = useLoad(
    () => api<SavingsReport>("GET", `/v1/reports/savings?${query.toString()}`),
    [start, end, groupBy],
  );

  return (
    <section>
      <h1>Savings</h1>
      <p className="muted">
        What requests would have cost as they were sent, at the baseline model, against what they
        cost after the optimizations, including the gateway work itself (OCR, classifiers,
        embeddings).
      </p>
      <form className="inline" onSubmit={(e) => e.preventDefault()}>
        <Field label="From">
          <input type="date" value={start} onChange={(e) => setStart(e.target.value)} required />
        </Field>
        <Field label="To (not included)">
          <input type="date" value={end} onChange={(e) => setEnd(e.target.value)} required />
        </Field>
        <Field label="Group by">
          <select value={groupBy} onChange={(e) => setGroupBy(e.target.value as GroupBy)}>
            <option value="team">Team</option>
            <option value="model">Model</option>
            <option value="day">Day</option>
          </select>
        </Field>
      </form>
      <ErrorBanner error={report.error} />
      {report.loading && report.data === null ? <Loading /> : null}
      {report.data ? (
        <>
          <Totals totals={report.data.totals} />
          <h2>By {groupBy}</h2>
          <table>
            <thead>
              <tr>
                <th scope="col">{HEADINGS[groupBy]}</th>
                <th scope="col">Requests</th>
                <th scope="col">Baseline</th>
                <th scope="col">Cost</th>
                <th scope="col">Overhead</th>
                <th scope="col">Saved</th>
              </tr>
            </thead>
            <tbody>
              {report.data.rows.map((r) => (
                <tr key={r.key}>
                  <td>{r.label ?? r.key}</td>
                  <td>{r.requests.toLocaleString("en-US")}</td>
                  <td>{usd(r.baseline_micro_usd)}</td>
                  <td>{usd(r.cost_micro_usd)}</td>
                  <td>{usd(r.overhead_micro_usd)}</td>
                  <td>{usd(r.savings_micro_usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <h2>By technique</h2>
          {report.data.techniques.length === 0 ? (
            <p>No optimization ran in this period.</p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th scope="col">Technique</th>
                  <th scope="col">Applied</th>
                  <th scope="col">Tokens saved</th>
                  <th scope="col">Saved</th>
                  <th scope="col">Shadow runs</th>
                  <th scope="col">Would save</th>
                </tr>
              </thead>
              <tbody>
                {report.data.techniques.map((t) => (
                  <tr key={t.technique}>
                    <td>{t.technique}</td>
                    <td>{t.applied.toLocaleString("en-US")}</td>
                    <td>{t.tokens_saved.toLocaleString("en-US")}</td>
                    <td>{usd(t.saved_micro_usd)}</td>
                    <td>{t.shadow_runs.toLocaleString("en-US")}</td>
                    <td>{usd(t.shadow_saved_micro_usd)}</td>
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
