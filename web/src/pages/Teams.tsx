import { type FormEvent, useState } from "react";

import { api } from "../api/client";
import type { ApiKey, ApiKeyCreated, InvitationCreated, ListOf, Member, Team } from "../api/types";
import { ErrorBanner, Field, Loading, Notice, useLoad } from "../components/ui";
import { when } from "../format";

function TeamDetail({ team }: { team: Team }) {
  const members = useLoad(() => api<ListOf<Member>>("GET", `/v1/admin/teams/${team.id}/members`), [team.id]);
  const keys = useLoad(() => api<ListOf<ApiKey>>("GET", `/v1/admin/teams/${team.id}/api-keys`), [team.id]);
  const [keyName, setKeyName] = useState("");
  const [invitee, setInvitee] = useState("");
  const [created, setCreated] = useState<string | null>(null);
  const [inviteUrl, setInviteUrl] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);

  const createKey = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    try {
      const key = await api<ApiKeyCreated>("POST", `/v1/admin/teams/${team.id}/api-keys`, { name: keyName });
      setCreated(key.key);
      setKeyName("");
      keys.reload();
    } catch (err) {
      setError(err);
    }
  };
  const revoke = async (id: string) => {
    setError(null);
    try {
      await api("DELETE", `/v1/admin/teams/${team.id}/api-keys/${id}`);
      keys.reload();
    } catch (err) {
      setError(err);
    }
  };
  const invite = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    try {
      const sent = await api<InvitationCreated>("POST", "/v1/admin/invitations", {
        email: invitee,
        team_id: team.id,
        team_role: "member",
      });
      setInviteUrl(sent.invite_url);
      setInvitee("");
    } catch (err) {
      setError(err);
    }
  };

  return (
    <div className="panel">
      <h2>{team.name}</h2>
      <ErrorBanner error={members.error ?? keys.error ?? error} />
      <h3>Members</h3>
      <ul>
        {(members.data?.data ?? []).map((m) => (
          <li key={m.user_id}>
            {m.email}
            {m.is_lead ? " (lead)" : ""}
          </li>
        ))}
      </ul>
      <form className="inline" onSubmit={(e) => void invite(e)}>
        <Field label="Invite by e-mail">
          <input type="email" value={invitee} onChange={(e) => setInvitee(e.target.value)} required />
        </Field>
        <button type="submit">Invite</button>
      </form>
      {inviteUrl ? (
        <Notice>
          Send this single-use link to the invitee (it is shown only now): <code>{inviteUrl}</code>
        </Notice>
      ) : null}
      <h3>API keys</h3>
      <table>
        <thead>
          <tr>
            <th scope="col">Name</th>
            <th scope="col">Prefix</th>
            <th scope="col">Last used</th>
            <th scope="col">Status</th>
            <th scope="col" />
          </tr>
        </thead>
        <tbody>
          {(keys.data?.data ?? []).map((k) => (
            <tr key={k.id}>
              <td>{k.name}</td>
              <td>
                <code>{k.prefix}</code>
              </td>
              <td>{when(k.last_used_at)}</td>
              <td>{k.revoked_at ? `revoked ${when(k.revoked_at)}` : "active"}</td>
              <td>
                {k.revoked_at ? null : (
                  <button type="button" onClick={() => void revoke(k.id)}>
                    Revoke
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <form className="inline" onSubmit={(e) => void createKey(e)}>
        <Field label="New key name">
          <input value={keyName} onChange={(e) => setKeyName(e.target.value)} required maxLength={200} />
        </Field>
        <button type="submit">Create key</button>
      </form>
      {created ? (
        <Notice>
          Copy the key now; it is never shown again: <code>{created}</code>
        </Notice>
      ) : null}
    </div>
  );
}

export function Teams({ admin }: { admin: boolean }) {
  const teams = useLoad(() => api<ListOf<Team>>("GET", "/v1/admin/teams"), []);
  const [selected, setSelected] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [error, setError] = useState<unknown>(null);

  const create = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    try {
      await api("POST", "/v1/admin/teams", { name });
      setName("");
      teams.reload();
    } catch (err) {
      setError(err);
    }
  };

  if (teams.loading && teams.data === null) return <Loading />;
  const list = teams.data?.data ?? [];
  const team = list.find((t) => t.id === selected) ?? list[0];
  return (
    <section>
      <h1>Teams and keys</h1>
      <ErrorBanner error={teams.error ?? error} />
      <nav aria-label="Teams" className="tabs">
        {list.map((t) => (
          <button key={t.id} type="button" aria-pressed={t.id === team?.id} onClick={() => setSelected(t.id)}>
            {t.name} ({t.member_count})
          </button>
        ))}
      </nav>
      {admin ? (
        <form className="inline" onSubmit={(e) => void create(e)}>
          <Field label="New team">
            <input value={name} onChange={(e) => setName(e.target.value)} required maxLength={200} />
          </Field>
          <button type="submit">Create team</button>
        </form>
      ) : null}
      {team ? <TeamDetail key={team.id} team={team} /> : <p>No teams yet.</p>}
    </section>
  );
}
