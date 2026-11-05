"""The agents' shared workspace (US-25): `POST`/`GET /v1/workspaces`, tasks, and a task's
discovery report. Agents read and write the workspace through the MCP broker (`/v1/mcp`)."""

from datetime import datetime
from typing import Annotated, Literal, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from gateway.adapters.postgres.workspace import TaskRecord, WorkspaceRecord
from gateway.api.caller import AnyCaller, ModelCaller
from gateway.api.documents import AccessIn, default_group
from gateway.api.files import file_owner
from gateway.domain.policy import Permission
from gateway.domain.retrieval import Access, share_groups
from gateway.pipeline.workspace import TaskReport, WorkspaceService

router = APIRouter(tags=["data-plane"])


class WorkspaceServices(Protocol):
    @property
    def workspace_service(self) -> WorkspaceService: ...


def get_workspaces(request: Request) -> WorkspaceService:
    services: WorkspaceServices = request.app.state.container
    return services.workspace_service


Workspaces = Annotated[WorkspaceService, Depends(get_workspaces)]


class WorkspaceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    access: AccessIn = Field(default_factory=AccessIn)


class WorkspaceOut(BaseModel):
    id: str
    object: Literal["workspace"] = "workspace"
    name: str
    access: list[str]
    facts: int
    created_at: int

    @classmethod
    def of(cls, w: WorkspaceRecord) -> WorkspaceOut:
        return cls(
            id=str(w.id),
            name=w.name,
            access=sorted(w.acl_groups),
            facts=w.fact_count,
            created_at=int(w.created_at.timestamp()),
        )


class WorkspaceList(BaseModel):
    object: Literal["list"] = "list"
    data: list[WorkspaceOut]


class TaskIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=300)
    agent_token_budget: int | None = Field(default=None, ge=1, le=1_000_000_000)


class TaskOut(BaseModel):
    id: str
    object: Literal["workspace.task"] = "workspace.task"
    workspace_id: str
    title: str
    agent_token_budget: int | None
    created_at: int

    @classmethod
    def of(cls, t: TaskRecord) -> TaskOut:
        return cls(
            id=str(t.id),
            workspace_id=str(t.workspace_id),
            title=t.title,
            agent_token_budget=t.agent_token_budget,
            created_at=_epoch(t.created_at),
        )


class AgentUse(BaseModel):
    agent: str
    discovery_tokens: int


class TaskReportOut(TaskOut):
    discovery_tokens: int
    agents: list[AgentUse]
    facts: int
    messages: int

    @classmethod
    def report(cls, r: TaskReport) -> TaskReportOut:
        task = TaskOut.of(r.task)
        return cls(
            **task.model_dump(),
            discovery_tokens=r.discovery_tokens,
            agents=[AgentUse(agent=a, discovery_tokens=t) for a, t in r.agents],
            facts=r.facts,
            messages=r.messages,
        )


def _epoch(at: datetime) -> int:
    return int(at.timestamp())


@router.post("/v1/workspaces")
async def create_workspace(
    body: WorkspaceIn, caller: ModelCaller, workspaces: Workspaces
) -> WorkspaceOut:
    access = Access(
        org=body.access.org,
        teams=tuple(body.access.teams),
        users=tuple(body.access.users),
        roles=tuple(body.access.roles),
    )
    groups = share_groups(
        access,
        own=caller.groups,
        default=default_group(caller),
        admin=caller.can(Permission.ORG_MANAGE),
    )
    return WorkspaceOut.of(await workspaces.create(file_owner(caller), body.name, groups))


@router.get("/v1/workspaces")
async def list_workspaces(
    caller: AnyCaller, workspaces: Workspaces, limit: Annotated[int, Query(ge=1, le=1000)] = 100
) -> WorkspaceList:
    found = await workspaces.workspaces(file_owner(caller), caller.groups, limit=limit)
    return WorkspaceList(data=[WorkspaceOut.of(w) for w in found])


@router.delete("/v1/workspaces/{workspace_id}")
async def delete_workspace(
    workspace_id: UUID, caller: ModelCaller, workspaces: Workspaces
) -> dict[str, object]:
    """The workspace and everything in it: its creator decides, or an admin."""
    admin = caller.can(Permission.ORG_MANAGE)
    await workspaces.delete(file_owner(caller), workspace_id, admin=admin)
    return {"id": str(workspace_id), "object": "workspace", "deleted": True}


@router.post("/v1/workspaces/{workspace_id}/tasks")
async def create_task(
    workspace_id: UUID, body: TaskIn, caller: ModelCaller, workspaces: Workspaces
) -> TaskOut:
    task = await workspaces.create_task(
        file_owner(caller),
        caller.groups,
        workspace_id,
        title=body.title,
        budget=body.agent_token_budget,
    )
    return TaskOut.of(task)


@router.get("/v1/workspaces/{workspace_id}/tasks/{task_id}")
async def task_report(
    workspace_id: UUID, task_id: UUID, caller: AnyCaller, workspaces: Workspaces
) -> TaskReportOut:
    """The task with its discovery tokens per agent (US-25's metric)."""
    report = await workspaces.report(file_owner(caller), caller.groups, workspace_id, task_id)
    return TaskReportOut.report(report)
