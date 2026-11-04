"""The graph store (spec section 2, "PostgreSQL version policy"; ADR 0004): the only way business
code reads the code graph. Every read takes the caller's groups: a caller who cannot read a
project gets nothing of its graph."""

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from gateway.domain.graph import Direction, Reached


class GraphStore(Protocol):
    async def neighbors(
        self,
        org_id: UUID,
        *,
        project_id: UUID,
        groups: frozenset[str],
        ids: Sequence[UUID],
        kinds: Sequence[str],
        direction: Direction = "out",
        depth: int = 1,
        limit: int = 200,
    ) -> list[Reached]:
        """The vertices exactly `depth` hops away (1 to 3) along edges of `kinds`, each with
        the weakest edge on its best path; deterministic edges first."""
        ...

    async def reachable(
        self,
        org_id: UUID,
        *,
        project_id: UUID,
        groups: frozenset[str],
        ids: Sequence[UUID],
        kinds: Sequence[str],
        direction: Direction = "out",
        max_depth: int = 6,
        min_confidence: float = 0.0,
        limit: int = 500,
    ) -> list[Reached]:
        """Every vertex within `max_depth` hops (up to 8) along edges of `kinds` at least as
        sure as `min_confidence`, at its shortest depth. Cycles end the walk."""
        ...
