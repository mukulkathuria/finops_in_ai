"""The context broker's workspace tools (spec section 4, F6; US-25), served by `/v1/mcp`. They run
as the calling credential under the workspace's ACL. A read that names its task counts the
agent's discovery tokens there, within the task's budget per agent."""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Final, Protocol, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gateway.api.caller import Caller
from gateway.api.files import file_owner
from gateway.domain.workspace import (
    AGENT_PATTERN,
    MAX_FACT_CHARS,
    MAX_QUESTION_CHARS,
    MAX_RESULTS,
    Charge,
)
from gateway.pipeline.workspace import WorkspaceService

AgentName = Annotated[str, Field(pattern=AGENT_PATTERN, description="An agent's name.")]


class WorkspaceSession(Protocol):
    @property
    def caller(self) -> Caller: ...

    @property
    def workspace(self) -> WorkspaceService: ...

    @property
    def heavy(self) -> Callable[[], Awaitable[None]]: ...


class Accounted(BaseModel):
    """Optional on other tools' reads (documents, code context): with all three, the read's
    tokens count as the agent's discovery on that workspace task."""

    workspace_id: UUID | None = Field(default=None, description="The workspace of the task.")
    task_id: UUID | None = Field(default=None, description="The task the read is for.")
    agent: str | None = Field(default=None, pattern=AGENT_PATTERN, description="The agent.")

    @model_validator(mode="after")
    def _together(self) -> Self:
        given = [v is not None for v in (self.workspace_id, self.task_id, self.agent)]
        if any(given) and not all(given):
            raise ValueError("workspace_id, task_id and agent go together")
        return self

    def charge(self) -> Charge | None:
        if self.task_id is None or self.agent is None:
            return None
        return Charge(self.task_id, self.agent)


class _Scoped(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: UUID = Field(description="The workspace (POST /v1/workspaces creates one).")
    agent: AgentName
    task_id: UUID | None = Field(
        default=None, description="The task this is for: its discovery tokens count there."
    )

    def charge(self) -> Charge | None:
        return Charge(self.task_id, self.agent) if self.task_id is not None else None


class PutFactArgs(_Scoped):
    model_config = ConfigDict(extra="forbid", title="put_fact arguments")
    content: str = Field(min_length=1, max_length=MAX_FACT_CHARS, description="The fact.")
    key: str | None = Field(default=None, min_length=1, max_length=200, description="A name.")
    source: str | None = Field(default=None, max_length=1_000, description="Where it came from.")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="How sure, 0 to 1.")


class GetFactArgs(_Scoped):
    model_config = ConfigDict(extra="forbid", title="get_fact arguments")
    fact_id: UUID | None = Field(default=None, description="The fact's id.")
    key: str | None = Field(default=None, min_length=1, max_length=200, description="Its key.")

    @model_validator(mode="after")
    def _one(self) -> Self:
        if (self.fact_id is None) == (self.key is None):
            raise ValueError("give fact_id or key")
        return self


class SearchWorkspaceArgs(_Scoped):
    model_config = ConfigDict(extra="forbid", title="search_workspace arguments")
    query: str = Field(min_length=1, max_length=4_000, description="What to look for.")
    k: int = Field(default=5, ge=1, le=MAX_RESULTS, description="How many facts to return.")


class GetSummaryArgs(_Scoped):
    model_config = ConfigDict(extra="forbid", title="get_file_summary arguments")
    path: str = Field(min_length=1, max_length=1_000, description="The file's path.")


class PutSummaryArgs(_Scoped):
    model_config = ConfigDict(extra="forbid", title="put_file_summary arguments")
    path: str = Field(min_length=1, max_length=1_000, description="The file's path.")
    summary: str = Field(min_length=1, max_length=MAX_FACT_CHARS, description="The summary.")


class AskArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", title="ask_agent arguments")
    workspace_id: UUID
    task_id: UUID = Field(description="The task the question is about.")
    agent: AgentName
    to_agent: AgentName
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    reply_to: UUID | None = Field(
        default=None, description="The message this question follows up, if any."
    )


class AnswerArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", title="answer_question arguments")
    workspace_id: UUID
    agent: AgentName
    message_id: UUID
    answer: str = Field(min_length=1, max_length=MAX_FACT_CHARS)


class InboxArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", title="read_inbox arguments")
    workspace_id: UUID
    task_id: UUID
    agent: AgentName


def _owner(session: WorkspaceSession) -> tuple[Any, frozenset[str]]:
    return file_owner(session.caller), session.caller.groups


async def put_fact(session: WorkspaceSession, args: PutFactArgs) -> tuple[str, dict[str, Any]]:
    await session.heavy()  # a new fact is embedded, as a document ingest is
    owner, groups = _owner(session)
    fact_id, known = await session.workspace.put_fact(
        owner,
        groups,
        args.workspace_id,
        agent=args.agent,
        content=args.content,
        key=args.key,
        source=args.source,
        confidence=args.confidence,
        task_id=args.task_id,
    )
    text = f"Already known as fact {fact_id}." if known else f"Recorded as fact {fact_id}."
    return text, {"fact_id": str(fact_id), "deduplicated": known}


async def get_fact(session: WorkspaceSession, args: GetFactArgs) -> tuple[str, dict[str, Any]]:
    owner, groups = _owner(session)
    fact, text = await session.workspace.get_fact(
        owner, groups, args.workspace_id, fact_id=args.fact_id, key=args.key, charge=args.charge()
    )
    return text, {"fact_id": str(fact.id), "key": fact.key, "agent": fact.agent}


async def search_workspace(
    session: WorkspaceSession, args: SearchWorkspaceArgs
) -> tuple[str, dict[str, Any]]:
    owner, groups = _owner(session)
    found = await session.workspace.search(
        owner, groups, args.workspace_id, args.query, k=args.k, charge=args.charge()
    )
    results = [
        {"fact_id": str(f.id), "key": f.key, "agent": f.agent, "score": round(f.score or 0, 4)}
        for f in found.facts
    ]
    summary = {
        "results": results,
        "deduplicated": found.deduplicated,
        "tokens": found.tokens,
        "withheld": found.withheld,
    }
    return found.text, summary


async def get_file_summary(
    session: WorkspaceSession, args: GetSummaryArgs
) -> tuple[str, dict[str, Any]]:
    owner, groups = _owner(session)
    found, text = await session.workspace.summary(
        owner, groups, args.workspace_id, path=args.path, charge=args.charge()
    )
    return text, {"path": found.path, "agent": found.agent}


async def put_file_summary(
    session: WorkspaceSession, args: PutSummaryArgs
) -> tuple[str, dict[str, Any]]:
    owner, groups = _owner(session)
    await session.workspace.put_summary(
        owner, groups, args.workspace_id, path=args.path, summary=args.summary, agent=args.agent
    )
    return f"Recorded the summary of {args.path}.", {"path": args.path}


async def ask_agent(session: WorkspaceSession, args: AskArgs) -> tuple[str, dict[str, Any]]:
    owner, groups = _owner(session)
    asked = await session.workspace.ask(
        owner,
        groups,
        args.workspace_id,
        task_id=args.task_id,
        agent=args.agent,
        to_agent=args.to_agent,
        question=args.question,
        reply_to=args.reply_to,
    )
    summary = {  # the answer itself only in the framed text, never bare (Phase 5 review S-E2)
        "message_id": str(asked.message.id),
        "status": asked.status,
        "asked": asked.message.asked,
        "hops": asked.message.hops,
    }
    return asked.text, summary


async def answer_question(
    session: WorkspaceSession, args: AnswerArgs
) -> tuple[str, dict[str, Any]]:
    owner, groups = _owner(session)
    message = await session.workspace.answer(
        owner,
        groups,
        args.workspace_id,
        message_id=args.message_id,
        agent=args.agent,
        answer=args.answer,
    )
    return f"Answered message {message.id}.", {"message_id": str(message.id)}


async def read_inbox(session: WorkspaceSession, args: InboxArgs) -> tuple[str, dict[str, Any]]:
    owner, groups = _owner(session)
    messages, text = await session.workspace.inbox(
        owner, groups, args.workspace_id, task_id=args.task_id, agent=args.agent
    )
    return text, {"messages": [str(m.id) for m in messages]}


Run = Callable[[WorkspaceSession, Any], Awaitable[tuple[str, dict[str, Any]]]]
TOOLS: Final[dict[str, tuple[str, str, type[BaseModel], Run]]] = {
    "put_fact": (
        "Record a fact",
        "Record a fact in a shared workspace so other agents can find it without searching"
        " again. An identical fact is kept once; a key makes it easy to get.",
        PutFactArgs,
        put_fact,
    ),
    "get_fact": (
        "Get a fact",
        "A fact of the workspace, whole, by its id or its key.",
        GetFactArgs,
        get_fact,
    ),
    "search_workspace": (
        "Search the workspace",
        "Facts of the workspace nearest a query, as ranked snippets. The same agent repeating"
        " a search in a task, with no fact added since, is answered from the record.",
        SearchWorkspaceArgs,
        search_workspace,
    ),
    "get_file_summary": (
        "Get a file summary",
        "The summary an agent recorded for a file path: read it instead of the file.",
        GetSummaryArgs,
        get_file_summary,
    ),
    "put_file_summary": (
        "Record a file summary",
        "Record or replace the summary of a file path for the other agents.",
        PutSummaryArgs,
        put_file_summary,
    ),
    "ask_agent": (
        "Ask an agent",
        "Ask another agent a question about a task. The same question to the same agent is one"
        " message: once answered, its answer comes back at once. An unanswered question asked"
        " again and again is refused as a loop, and follow-up chains stop at 3 hops.",
        AskArgs,
        ask_agent,
    ),
    "answer_question": (
        "Answer a question",
        "Answer a question another agent asked you.",
        AnswerArgs,
        answer_question,
    ),
    "read_inbox": (
        "Read the inbox",
        "The questions waiting for you in a task, and the answers to the ones you asked.",
        InboxArgs,
        read_inbox,
    ),
}
