import { useState } from "react";

import { api } from "../api/client";
import type { AuditPage, Verification } from "../api/types";
import { ErrorBanner, Loading, Notice, useLoad } from "../components/ui";
import { shortId, when } from "../format";

export function Audit() {
  const page = useLoad(() => api<AuditPage>("GET", "/v1/admin/audit?limit=200"), []);
  const [check, setCheck] = useState<Verification | null>(null);
  const [error, setError] = useState<unknown>(null);

  const verify = async () => {
    setError(null);
    try {
      setCheck(await api<Verification>("GET", "/v1/admin/audit/verify"));
    } catch (err) {
      setError(err);
    }
  };

  if (page.loading && page.data === null) return <Loading />;
  return (
    <section>
      <h1>Audit log</h1>
      <ErrorBanner error={page.error ?? error} />
      <button type="button" onClick={() => void verify()}>
        Verify the hash chain
      </button>
      {check ? (
        <Notice>
          {check.ok
            ? `Chain intact: ${check.checked} entries verified.`
            : `Chain broken at entry ${check.first_bad_seq ?? "?"} (${check.checked} checked).`}
        </Notice>
      ) : null}
      <table>
        <thead>
          <tr>
            <th scope="col">#</th>
            <th scope="col">When</th>
            <th scope="col">Action</th>
            <th scope="col">Actor</th>
            <th scope="col">Target</th>
          </tr>
        </thead>
        <tbody>
          {(page.data?.data ?? []).map((e) => (
            <tr key={e.seq}>
              <td>{e.seq}</td>
              <td>{when(e.ts)}</td>
              <td>
                <code>{e.action}</code>
              </td>
              <td>
                {e.actor_type} {shortId(e.actor_id)}
              </td>
              <td>
                {e.target_type ?? ""} {e.target_id ? shortId(e.target_id) : ""}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
