"""The agents' shared workspace (spec section 4, F6; US-25): its limits, and the rules for
repeated questions, loops and clarification hops."""

import hashlib
import unicodedata
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from gateway.domain.errors import DomainError, InvalidInput

MAX_FACT_CHARS: Final = 20_000
MAX_SNIPPET_CHARS: Final = 1_000  # a search returns snippets, never whole facts
MAX_QUESTION_CHARS: Final = 4_000
MAX_RESULTS: Final = 20
MAX_HOPS: Final = 3  # a clarification chain stops here
LOOP_REPEATS: Final = 3  # an unanswered question asked this often is a loop
AGENT_PATTERN: Final = r"^[A-Za-z0-9_.:-]{1,100}$"
# Bounds (Phase 5 review): what one workspace or task may hold.
MAX_FACTS: Final = 2_000  # a workspace's facts: its exact search reads every one
MAX_SUMMARIES: Final = 10_000  # file summaries in a workspace
MAX_AGENTS: Final = 100  # agent names one member may use in a task, each with its own budget
MAX_MESSAGES: Final = 5_000  # questions in a task
MAX_QUERIES: Final = 5_000  # searches remembered in a task; past it they still run


class AgentBudgetExceeded(DomainError):
    kind = "budget_exceeded"
    default_code = "agent_budget_exceeded"


class AgentClaimed(DomainError):
    """An agent's name in a task belongs to the member that first used it there."""

    kind = "permission"
    default_code = "agent_claimed"


class LoopDetected(DomainError):
    kind = "conflict"
    default_code = "loop_detected"


@dataclass(frozen=True, slots=True)
class Charge:
    """Where a read's discovery tokens go: a task of the workspace, and the agent reading."""

    task_id: UUID
    agent: str


def digest(text: str) -> bytes:
    """What makes two questions, queries or facts the same: case, spacing and Unicode forms do
    not."""
    normal = " ".join(unicodedata.normalize("NFKC", text).casefold().split())
    return hashlib.sha256(normal.encode()).digest()


def snippet(text: str) -> str:
    return text if len(text) <= MAX_SNIPPET_CHARS else text[: MAX_SNIPPET_CHARS - 3] + "..."


def next_hop(parent_hops: int | None) -> int:
    """A question's hop count: one more than the question it answers to. Past MAX_HOPS the
    chain stops."""
    hops = 1 if parent_hops is None else parent_hops + 1
    if hops > MAX_HOPS:
        raise InvalidInput(
            f"a clarification chain stops at {MAX_HOPS} hops: answer from what is known",
            code="hop_limit_reached",
        )
    return hops
