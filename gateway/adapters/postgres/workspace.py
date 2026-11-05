"""The workspace tables (US-25). Every statement that reads or writes a workspace's content
checks its ACL in the same statement, under row-level security. A count that bounds a workspace
or a task is checked in the statement that adds to it (Phase 5 review)."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncConnection

from gateway.adapters.postgres.database import fetch_all, fetch_one


@dataclass(frozen=True, slots=True)
class WorkspaceRecord:
    id: UUID
    name: str
    acl_groups: tuple[str, ...]
    created_by: UUID | None
    created_by_team: UUID | None
    created_at: datetime
    fact_count: int = 0


@dataclass(frozen=True, slots=True)
class TaskRecord:
    id: UUID
    workspace_id: UUID
    title: str
    agent_token_budget: int | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class FactRecord:
    id: UUID
    key: str | None
    content: str
    source: str | None
    confidence: float
    agent: str
    task_id: UUID | None
    created_at: datetime
    score: float | None = None


@dataclass(frozen=True, slots=True)
class SummaryRecord:
    path: str
    summary: str
    agent: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class MessageRecord:
    id: UUID
    task_id: UUID
    from_agent: str
    to_agent: str
    question: str
    answer: str | None
    hops: int
    asked: int
    created_at: datetime
    answered_at: datetime | None


@dataclass(frozen=True, slots=True)
class RememberedSearch:
    fact_ids: tuple[UUID, ...]
    fact_count: int  # the workspace's facts when the search ran


@dataclass(frozen=True, slots=True)
class AgentClaim:
    present: bool  # False: the task had no room for a new agent
    principal: UUID | None  # the member the name is bound to (None: never bound)


_CREATE_WORKSPACE: Final = """
INSERT INTO workspaces (org_id, name, acl_groups, created_by, created_by_team)
VALUES (app_current_org(), :name, CAST(:groups AS text[]), :created_by, :created_by_team)
RETURNING id, name, acl_groups, created_by, created_by_team, created_at, fact_count
"""
_GET_WORKSPACE: Final = """
SELECT w.id, w.name, w.acl_groups, w.created_by, w.created_by_team, w.created_at, w.fact_count
FROM workspaces w
WHERE w.id = :workspace AND w.acl_groups && CAST(:groups AS text[])
"""
_LIST_WORKSPACES: Final = """
SELECT w.id, w.name, w.acl_groups, w.created_by, w.created_by_team, w.created_at, w.fact_count
FROM workspaces w
WHERE w.acl_groups && CAST(:groups AS text[])
ORDER BY w.created_at DESC, w.id DESC
LIMIT :limit
"""
# Its creator decides, or an admin: the user who made it, or, for a team key's workspace, a key
# of that team. Tasks, facts, summaries, questions and remembered searches cascade.
_DELETE_WORKSPACE: Final = """
DELETE FROM workspaces
WHERE id = :workspace
  AND (CAST(:admin AS boolean)
       OR (created_by IS NOT NULL AND created_by = :user_id)
       OR (created_by IS NULL AND CAST(:user_id AS uuid) IS NULL
           AND created_by_team = :team_id))
RETURNING id
"""
_CREATE_TASK: Final = """
INSERT INTO tasks (org_id, workspace_id, title, agent_token_budget)
SELECT app_current_org(), w.id, :title, :budget
FROM workspaces w
WHERE w.id = :workspace AND w.acl_groups && CAST(:groups AS text[])
RETURNING id, workspace_id, title, agent_token_budget, created_at
"""
_GET_TASK: Final = """
SELECT t.id, t.workspace_id, t.title, t.agent_token_budget, t.created_at
FROM tasks t JOIN workspaces w ON w.id = t.workspace_id
WHERE t.id = :task AND t.workspace_id = :workspace AND w.acl_groups && CAST(:groups AS text[])
"""
_TASK_AGENTS: Final = """
SELECT agent, discovery_tokens FROM task_agents WHERE task_id = :task ORDER BY agent
"""
_TASK_COUNTS: Final = """
SELECT (SELECT count(*) FROM facts WHERE task_id = :task) AS facts,
       (SELECT count(*) FROM agent_messages WHERE task_id = :task) AS messages
"""
# A name new to the task is bound to the member using it, while the task has room; a name
# bound before migration 0027 (no principal) is bound now.
_CLAIM_AGENT: Final = """
INSERT INTO task_agents (task_id, agent, org_id, principal)
SELECT :task, :agent, app_current_org(), :principal
WHERE (SELECT count(*) FROM task_agents WHERE task_id = :task) < :cap
ON CONFLICT (task_id, agent) DO UPDATE SET principal = excluded.principal
  WHERE task_agents.principal IS NULL
"""
_AGENT_PRINCIPAL: Final = """
SELECT principal FROM task_agents WHERE task_id = :task AND agent = :agent
"""
_CHARGE: Final = """
UPDATE task_agents a SET discovery_tokens = a.discovery_tokens + :tokens
FROM tasks t
WHERE a.task_id = :task AND a.agent = :agent AND t.id = a.task_id
  AND (t.agent_token_budget IS NULL OR a.discovery_tokens + :tokens <= t.agent_token_budget)
RETURNING a.discovery_tokens
"""
_PUT_FACT: Final = """
INSERT INTO facts (org_id, workspace_id, task_id, key, content, source, confidence, agent,
                   content_hash, token_count, embedding, embedding_model, written_by)
SELECT app_current_org(), w.id, :task, :key, :content, :source, :confidence, :agent, :hash,
       :tokens, CAST(:embedding AS vector), :model, :written_by
FROM workspaces w
WHERE w.id = :workspace AND w.acl_groups && CAST(:groups AS text[])
ON CONFLICT (workspace_id, content_hash) DO NOTHING
RETURNING id
"""
# Concurrent writers queue on the workspace's row, and each sees the count the last one left.
_COUNT_FACT: Final = """
UPDATE workspaces SET fact_count = fact_count + 1
WHERE id = :workspace AND fact_count < :cap
RETURNING fact_count
"""
_FACT_BY_HASH: Final = """
SELECT f.id FROM facts f
WHERE f.workspace_id = :workspace AND f.content_hash = :hash
  AND EXISTS (SELECT 1 FROM workspaces w
              WHERE w.id = :workspace AND w.acl_groups && CAST(:groups AS text[]))
"""
_FACT_BY_ID: Final = """
SELECT f.id, f.key, f.content, f.source, f.confidence, f.agent, f.task_id, f.created_at
FROM facts f
WHERE f.id = :fact AND f.workspace_id = :workspace
  AND EXISTS (SELECT 1 FROM workspaces w
              WHERE w.id = :workspace AND w.acl_groups && CAST(:groups AS text[]))
"""
_FACT_BY_KEY: Final = """
SELECT f.id, f.key, f.content, f.source, f.confidence, f.agent, f.task_id, f.created_at
FROM facts f
WHERE f.workspace_id = :workspace AND f.key = :key
  AND EXISTS (SELECT 1 FROM workspaces w
              WHERE w.id = :workspace AND w.acl_groups && CAST(:groups AS text[]))
ORDER BY f.created_at DESC, f.id DESC
LIMIT 1
"""
# Exact within the workspace: a workspace holds at most MAX_FACTS facts, and its nearest facts
# must not be lost to a filtered approximate scan.
_SEARCH: Final = """
SELECT f.id, f.key, f.content, f.source, f.confidence, f.agent, f.task_id, f.created_at,
       1 - (f.embedding <=> CAST(:query AS vector)) AS score
FROM facts f
WHERE f.workspace_id = :workspace AND f.embedding_model = :model
  AND EXISTS (SELECT 1 FROM workspaces w
              WHERE w.id = :workspace AND w.acl_groups && CAST(:groups AS text[]))
ORDER BY f.embedding <=> CAST(:query AS vector), f.id
LIMIT :k
"""
_QUERY_SEEN: Final = """
SELECT fact_ids, fact_count FROM task_queries
WHERE task_id = :task AND agent = :agent AND query_hash = :hash
"""
_REMEMBER_QUERY: Final = """
INSERT INTO task_queries (task_id, agent, query_hash, org_id, fact_ids, fact_count)
SELECT :task, :agent, :hash, app_current_org(), CAST(:facts AS uuid[]), :fact_count
WHERE (SELECT count(*) FROM task_queries WHERE task_id = :task) < :cap
   OR EXISTS (SELECT 1 FROM task_queries
              WHERE task_id = :task AND agent = :agent AND query_hash = :hash)
ON CONFLICT (task_id, agent, query_hash) DO UPDATE
  SET fact_ids = excluded.fact_ids, fact_count = excluded.fact_count, created_at = now()
"""
_PUT_SUMMARY: Final = """
INSERT INTO file_summaries (org_id, workspace_id, path, summary, agent, token_count, written_by)
SELECT app_current_org(), w.id, :path, :summary, :agent, :tokens, :written_by
FROM workspaces w
WHERE w.id = :workspace AND w.acl_groups && CAST(:groups AS text[])
  AND ((SELECT count(*) FROM file_summaries s WHERE s.workspace_id = w.id) < :cap
       OR EXISTS (SELECT 1 FROM file_summaries s WHERE s.workspace_id = w.id AND s.path = :path))
ON CONFLICT (workspace_id, path) DO UPDATE
  SET summary = excluded.summary, agent = excluded.agent, token_count = excluded.token_count,
      written_by = excluded.written_by, updated_at = now()
RETURNING path
"""
_GET_SUMMARY: Final = """
SELECT s.path, s.summary, s.agent, s.updated_at
FROM file_summaries s
WHERE s.workspace_id = :workspace AND s.path = :path
  AND EXISTS (SELECT 1 FROM workspaces w
              WHERE w.id = :workspace AND w.acl_groups && CAST(:groups AS text[]))
"""
_ASK: Final = """
INSERT INTO agent_messages (org_id, task_id, from_agent, to_agent, question, question_hash, hops,
                            written_by)
SELECT app_current_org(), :task, :agent, :to, :question, :hash, :hops, :written_by
WHERE (SELECT count(*) FROM agent_messages WHERE task_id = :task) < :cap
   OR EXISTS (SELECT 1 FROM agent_messages
              WHERE task_id = :task AND to_agent = :to AND question_hash = :hash)
ON CONFLICT (task_id, to_agent, question_hash) DO UPDATE SET asked = agent_messages.asked + 1
RETURNING id, task_id, from_agent, to_agent, question, answer, hops, asked, created_at,
          answered_at, (xmax = 0) AS created
"""
_MESSAGE: Final = """
SELECT m.id, m.task_id, m.from_agent, m.to_agent, m.question, m.answer, m.hops, m.asked,
       m.created_at, m.answered_at
FROM agent_messages m
JOIN tasks t ON t.id = m.task_id
JOIN workspaces w ON w.id = t.workspace_id
WHERE m.id = :message AND t.workspace_id = :workspace
  AND w.acl_groups && CAST(:groups AS text[])
"""
_ANSWER: Final = """
UPDATE agent_messages SET answer = :answer, answered_at = now(), answered_by = :answered_by
WHERE id = :message AND answer IS NULL
RETURNING id, task_id, from_agent, to_agent, question, answer, hops, asked, created_at,
          answered_at
"""
_INBOX: Final = """
SELECT m.id, m.task_id, m.from_agent, m.to_agent, m.question, m.answer, m.hops, m.asked,
       m.created_at, m.answered_at
FROM agent_messages m
WHERE m.task_id = :task
  AND ((m.to_agent = :agent AND m.answer IS NULL)
       OR (m.from_agent = :agent AND m.answer IS NOT NULL))
ORDER BY m.created_at, m.id
LIMIT :limit
"""


def _vector(values: Sequence[float]) -> str:
    return "[" + ",".join(format(v, ".7g") for v in values) + "]"


def _workspace(row: RowMapping) -> WorkspaceRecord:
    return WorkspaceRecord(
        row["id"],
        row["name"],
        tuple(row["acl_groups"]),
        row["created_by"],
        row["created_by_team"],
        row["created_at"],
        int(row["fact_count"]),
    )


def _task(row: RowMapping) -> TaskRecord:
    return TaskRecord(
        row["id"], row["workspace_id"], row["title"], row["agent_token_budget"], row["created_at"]
    )


def _fact(row: RowMapping) -> FactRecord:
    score = row.get("score")
    return FactRecord(
        row["id"],
        row["key"],
        row["content"],
        row["source"],
        float(row["confidence"]),
        row["agent"],
        row["task_id"],
        row["created_at"],
        float(score) if score is not None else None,
    )


def _message(row: RowMapping) -> MessageRecord:
    return MessageRecord(
        row["id"],
        row["task_id"],
        row["from_agent"],
        row["to_agent"],
        row["question"],
        row["answer"],
        int(row["hops"]),
        int(row["asked"]),
        row["created_at"],
        row["answered_at"],
    )


class WorkspaceRepository:
    async def create(
        self,
        conn: AsyncConnection,
        *,
        name: str,
        groups: Sequence[str],
        created_by: UUID | None,
        created_by_team: UUID | None,
    ) -> WorkspaceRecord:
        params = {
            "name": name,
            "groups": sorted(groups),
            "created_by": created_by,
            "created_by_team": created_by_team,
        }
        row = await fetch_one(conn, _CREATE_WORKSPACE, params)
        if row is None:
            raise RuntimeError("workspace insert returned no row")
        return _workspace(row)

    async def workspace(
        self, conn: AsyncConnection, workspace_id: UUID, *, groups: frozenset[str]
    ) -> WorkspaceRecord | None:
        params = {"workspace": workspace_id, "groups": sorted(groups)}
        row = await fetch_one(conn, _GET_WORKSPACE, params)
        return _workspace(row) if row is not None else None

    async def workspaces(
        self, conn: AsyncConnection, *, groups: frozenset[str], limit: int
    ) -> list[WorkspaceRecord]:
        rows = await fetch_all(conn, _LIST_WORKSPACES, {"groups": sorted(groups), "limit": limit})
        return [_workspace(r) for r in rows]

    async def delete(
        self,
        conn: AsyncConnection,
        workspace_id: UUID,
        *,
        admin: bool,
        user_id: UUID | None,
        team_id: UUID | None,
    ) -> bool:
        params = {"workspace": workspace_id, "admin": admin, "user_id": user_id, "team_id": team_id}
        return await fetch_one(conn, _DELETE_WORKSPACE, params) is not None

    async def create_task(
        self,
        conn: AsyncConnection,
        workspace_id: UUID,
        *,
        groups: frozenset[str],
        title: str,
        budget: int | None,
    ) -> TaskRecord | None:
        params = {
            "workspace": workspace_id,
            "groups": sorted(groups),
            "title": title,
            "budget": budget,
        }
        row = await fetch_one(conn, _CREATE_TASK, params)
        return _task(row) if row is not None else None

    async def task(
        self, conn: AsyncConnection, workspace_id: UUID, task_id: UUID, *, groups: frozenset[str]
    ) -> TaskRecord | None:
        params = {"workspace": workspace_id, "task": task_id, "groups": sorted(groups)}
        row = await fetch_one(conn, _GET_TASK, params)
        return _task(row) if row is not None else None

    async def task_agents(self, conn: AsyncConnection, task_id: UUID) -> list[tuple[str, int]]:
        rows = await fetch_all(conn, _TASK_AGENTS, {"task": task_id})
        return [(str(r["agent"]), int(r["discovery_tokens"])) for r in rows]

    async def task_counts(self, conn: AsyncConnection, task_id: UUID) -> tuple[int, int]:
        row = await fetch_one(conn, _TASK_COUNTS, {"task": task_id})
        return (int(row["facts"]), int(row["messages"])) if row is not None else (0, 0)

    async def claim(
        self, conn: AsyncConnection, task_id: UUID, agent: str, principal: UUID, *, cap: int
    ) -> AgentClaim:
        """Bind an agent's name in a task to `principal` if the name is new there (and the task
        has room for it), and say whom it is bound to."""
        params = {"task": task_id, "agent": agent, "principal": principal, "cap": cap}
        await conn.execute(text(_CLAIM_AGENT), params)
        row = await fetch_one(conn, _AGENT_PRINCIPAL, params)
        return AgentClaim(row is not None, row["principal"] if row is not None else None)

    async def charge(self, conn: AsyncConnection, task_id: UUID, agent: str, tokens: int) -> bool:
        """Add a claimed agent's discovery tokens to a task, within the task's budget; False when
        they would pass it (and nothing is added). One statement: concurrent reads cannot
        overshoot."""
        params = {"task": task_id, "agent": agent, "tokens": tokens}
        return await fetch_one(conn, _CHARGE, params) is not None

    async def put_fact(
        self,
        conn: AsyncConnection,
        workspace_id: UUID,
        *,
        groups: frozenset[str],
        task_id: UUID | None,
        key: str | None,
        content: str,
        source: str | None,
        confidence: float,
        agent: str,
        content_hash: bytes,
        tokens: int,
        vector: Sequence[float],
        model: str,
        written_by: UUID,
    ) -> UUID | None:
        """The new fact's id; None when the workspace has this content already, or cannot be
        read."""
        params = {
            "workspace": workspace_id,
            "groups": sorted(groups),
            "task": task_id,
            "key": key,
            "content": content,
            "source": source,
            "confidence": confidence,
            "agent": agent,
            "hash": content_hash,
            "tokens": tokens,
            "embedding": _vector(vector),
            "model": model,
            "written_by": written_by,
        }
        row = await fetch_one(conn, _PUT_FACT, params)
        return row["id"] if row is not None else None

    async def count_fact(self, conn: AsyncConnection, workspace_id: UUID, *, cap: int) -> bool:
        """Count a new fact in its workspace; False when the workspace is full."""
        params = {"workspace": workspace_id, "cap": cap}
        return await fetch_one(conn, _COUNT_FACT, params) is not None

    async def fact_by_hash(
        self,
        conn: AsyncConnection,
        workspace_id: UUID,
        *,
        groups: frozenset[str],
        content_hash: bytes,
    ) -> UUID | None:
        params = {"workspace": workspace_id, "groups": sorted(groups), "hash": content_hash}
        row = await fetch_one(conn, _FACT_BY_HASH, params)
        return row["id"] if row is not None else None

    async def fact(
        self,
        conn: AsyncConnection,
        workspace_id: UUID,
        *,
        groups: frozenset[str],
        fact_id: UUID | None,
        key: str | None,
    ) -> FactRecord | None:
        params: dict[str, object] = {"workspace": workspace_id, "groups": sorted(groups)}
        if fact_id is not None:
            row = await fetch_one(conn, _FACT_BY_ID, {**params, "fact": fact_id})
        else:
            row = await fetch_one(conn, _FACT_BY_KEY, {**params, "key": key})
        return _fact(row) if row is not None else None

    async def search(
        self,
        conn: AsyncConnection,
        workspace_id: UUID,
        *,
        groups: frozenset[str],
        vector: Sequence[float],
        model: str,
        k: int,
    ) -> list[FactRecord]:
        params = {
            "workspace": workspace_id,
            "groups": sorted(groups),
            "query": _vector(vector),
            "model": model,
            "k": k,
        }
        return [_fact(r) for r in await fetch_all(conn, _SEARCH, params)]

    async def query_seen(
        self, conn: AsyncConnection, task_id: UUID, agent: str, query_hash: bytes
    ) -> RememberedSearch | None:
        params = {"task": task_id, "agent": agent, "hash": query_hash}
        row = await fetch_one(conn, _QUERY_SEEN, params)
        if row is None:
            return None
        return RememberedSearch(tuple(row["fact_ids"]), int(row["fact_count"]))

    async def remember_query(
        self,
        conn: AsyncConnection,
        task_id: UUID,
        agent: str,
        query_hash: bytes,
        fact_ids: Sequence[UUID],
        *,
        fact_count: int,
        cap: int,
    ) -> None:
        params = {
            "task": task_id,
            "agent": agent,
            "hash": query_hash,
            "facts": list(fact_ids),
            "fact_count": fact_count,
            "cap": cap,
        }
        await conn.execute(text(_REMEMBER_QUERY), params)

    async def put_summary(
        self,
        conn: AsyncConnection,
        workspace_id: UUID,
        *,
        groups: frozenset[str],
        path: str,
        summary: str,
        agent: str,
        tokens: int,
        written_by: UUID,
        cap: int,
    ) -> bool:
        """False when the workspace cannot be read, or is full and has no summary of this path
        to replace."""
        params = {
            "workspace": workspace_id,
            "groups": sorted(groups),
            "path": path,
            "summary": summary,
            "agent": agent,
            "tokens": tokens,
            "written_by": written_by,
            "cap": cap,
        }
        return await fetch_one(conn, _PUT_SUMMARY, params) is not None

    async def summary(
        self, conn: AsyncConnection, workspace_id: UUID, *, groups: frozenset[str], path: str
    ) -> SummaryRecord | None:
        params = {"workspace": workspace_id, "groups": sorted(groups), "path": path}
        row = await fetch_one(conn, _GET_SUMMARY, params)
        if row is None:
            return None
        return SummaryRecord(row["path"], row["summary"], row["agent"], row["updated_at"])

    async def ask(
        self,
        conn: AsyncConnection,
        *,
        task_id: UUID,
        agent: str,
        to_agent: str,
        question: str,
        question_hash: bytes,
        hops: int,
        written_by: UUID,
        cap: int,
    ) -> tuple[MessageRecord, bool] | None:
        """The question's message, new or the one already asked (its count raised), and whether
        it is new; None when the task is full. One statement, so two agents asking at once
        share one message."""
        params = {
            "task": task_id,
            "agent": agent,
            "to": to_agent,
            "question": question,
            "hash": question_hash,
            "hops": hops,
            "written_by": written_by,
            "cap": cap,
        }
        row = await fetch_one(conn, _ASK, params)
        return (_message(row), bool(row["created"])) if row is not None else None

    async def message(
        self, conn: AsyncConnection, workspace_id: UUID, message_id: UUID, *, groups: frozenset[str]
    ) -> MessageRecord | None:
        params = {"workspace": workspace_id, "message": message_id, "groups": sorted(groups)}
        row = await fetch_one(conn, _MESSAGE, params)
        return _message(row) if row is not None else None

    async def answer(
        self, conn: AsyncConnection, message_id: UUID, answer: str, *, answered_by: UUID
    ) -> MessageRecord | None:
        params = {"message": message_id, "answer": answer, "answered_by": answered_by}
        row = await fetch_one(conn, _ANSWER, params)
        return _message(row) if row is not None else None

    async def inbox(
        self, conn: AsyncConnection, task_id: UUID, agent: str, *, limit: int
    ) -> list[MessageRecord]:
        params = {"task": task_id, "agent": agent, "limit": limit}
        return [_message(r) for r in await fetch_all(conn, _INBOX, params)]
