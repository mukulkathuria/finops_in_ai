import { useState } from "react";

import { api } from "../api/client";
import type { BudgetRequest, ListOf } from "../api/types";
import { ErrorBanner, Loading, useLoad } from "../components/ui";
import { amount, shortId, when } from "../format";

export function Requests() {
  const requests = useLoad(() => api<ListOf<BudgetRequest>>("GET", "/v1/budget-requests"), []);
  const [error, setError] = useState<unknown>(null);

  const decide = async (id: string, body: Record<string, string>) => {
    setError(null);
    try {
      await api("POST", `/v1/budget-requests/${id}/decision`, body);
      requests.reload();
    } catch (err) {
      setError(err);
    }
  };

  if (requests.loading && requests.data === null) return <Loading />;
  const rows = requests.data?.data ?? [];
  return (
    <section>
      <h1>Budget requests</h1>
      <ErrorBanner error={requests.error ?? error} />
      {rows.length === 0 ? <p>No requests.</p> : null}
      <table>
        <thead>
          <tr>
            <th scope="col">Asked</th>
            <th scope="col">By</th>
            <th scope="col">Amount</th>
            <th scope="col">Reason</th>
            <th scope="col">Status</th>
            <th scope="col" />
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id}>
              <td>{when(r.created_at)}</td>
              <td>{shortId(r.user_id)}</td>
              <td>{amount(r.amount, r.unit)}</td>
              <td>{r.reason}</td>
              <td>
                {r.status}
                {r.grant_type ? ` (${r.grant_type.replace("_", " ")})` : ""}
                {r.decision_note ? `: ${r.decision_note}` : ""}
              </td>
              <td>
                {r.status === "pending" ? (
                  <span className="actions">
                    <button type="button" onClick={() => void decide(r.id, { decision: "approve", grant_type: "one_time" })}>
                      Approve this period
                    </button>
                    <button type="button" onClick={() => void decide(r.id, { decision: "approve", grant_type: "permanent" })}>
                      Approve permanently
                    </button>
                    <button type="button" onClick={() => void decide(r.id, { decision: "deny" })}>
                      Deny
                    </button>
                  </span>
                ) : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
