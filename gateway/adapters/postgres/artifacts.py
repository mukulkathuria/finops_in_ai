"""The parse cache (spec section 3A): derived artifacts keyed by content hash, backend name,
backend version and options, so the results of two backends are never mixed. Org-scoped (RLS)."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

import orjson
from sqlalchemy import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from gateway.adapters.postgres.database import fetch_all, fetch_one


@dataclass(frozen=True, slots=True)
class ArtifactKey:
    sha256: bytes
    backend: str
    backend_version: str
    options_hash: bytes


@dataclass(frozen=True, slots=True)
class NewArtifact:
    id: UUID
    key: ArtifactKey
    markdown: str
    pages: list[dict[str, Any]]  # page, method, confidence, kept_original, image_key
    warnings: list[str]
    tokens_before: int
    tokens_after: int
    gpu_seconds: float


@dataclass(frozen=True, slots=True)
class Artifact:
    id: UUID
    backend: str
    backend_version: str
    markdown: str
    pages: list[dict[str, Any]]
    warnings: list[str]
    tokens_before: int
    tokens_after: int
    gpu_seconds: float
    created_at: datetime


_GET: Final = """
SELECT id, backend, backend_version, markdown, pages, warnings, tokens_before, tokens_after,
       gpu_seconds, created_at
FROM file_artifacts
WHERE sha256 = :sha256 AND backend = :backend AND backend_version = :backend_version
  AND options_hash = :options_hash
"""
_LATEST: Final = """
SELECT id, backend, backend_version, markdown, pages, warnings, tokens_before, tokens_after,
       gpu_seconds, created_at
FROM file_artifacts
WHERE sha256 = :sha256 AND backend = :backend AND options_hash = :options_hash
ORDER BY created_at DESC
LIMIT 1
"""
_INSERT: Final = """
INSERT INTO file_artifacts (id, org_id, sha256, backend, backend_version, options_hash, markdown,
                            pages, warnings, tokens_before, tokens_after, gpu_seconds)
VALUES (:id, app_current_org(), :sha256, :backend, :backend_version, :options_hash, :markdown,
        CAST(:pages AS jsonb), CAST(:warnings AS jsonb), :tokens_before, :tokens_after,
        :gpu_seconds)
ON CONFLICT (org_id, sha256, backend, backend_version, options_hash) DO NOTHING
RETURNING id, backend, backend_version, markdown, pages, warnings, tokens_before, tokens_after,
          gpu_seconds, created_at
"""


_DELETE_UNREFERENCED: Final = """
DELETE FROM file_artifacts
WHERE sha256 = :sha256 AND NOT EXISTS (SELECT 1 FROM files WHERE files.sha256 = :sha256)
RETURNING pages
"""


def _artifact(row: RowMapping) -> Artifact:
    return Artifact(**dict(row))


def _key(key: ArtifactKey) -> dict[str, object]:
    return {
        "sha256": key.sha256,
        "backend": key.backend,
        "backend_version": key.backend_version,
        "options_hash": key.options_hash,
    }


class ArtifactRepository:
    async def get(self, conn: AsyncConnection, key: ArtifactKey) -> Artifact | None:
        row = await fetch_one(conn, _GET, _key(key))
        return _artifact(row) if row is not None else None

    async def latest(self, conn: AsyncConnection, key: ArtifactKey) -> Artifact | None:
        """The newest artifact for the content, backend and options, whatever its version: what
        to serve while the backend cannot say which version it runs."""
        row = await fetch_one(conn, _LATEST, _key(key))
        return _artifact(row) if row is not None else None

    async def insert(self, conn: AsyncConnection, new: NewArtifact) -> tuple[Artifact, bool]:
        """The stored artifact, and whether this call created it (a parallel parse may win)."""
        row = await fetch_one(
            conn,
            _INSERT,
            {
                **_key(new.key),
                "id": new.id,
                "markdown": new.markdown,
                "pages": orjson.dumps(new.pages).decode(),
                "warnings": orjson.dumps(new.warnings).decode(),
                "tokens_before": new.tokens_before,
                "tokens_after": new.tokens_after,
                "gpu_seconds": new.gpu_seconds,
            },
        )
        if row is not None:
            return _artifact(row), True
        existing = await self.get(conn, new.key)
        if existing is None:
            raise RuntimeError("the artifact conflicted but cannot be read back")
        return existing, False

    async def delete_unreferenced(self, conn: AsyncConnection, sha256: bytes) -> list[str]:
        """Remove the artifacts of a content no file in the org has any more; returns the object
        keys of their kept page images, to remove from the store."""
        rows = await fetch_all(conn, _DELETE_UNREFERENCED, {"sha256": sha256})
        return [str(p["image_key"]) for row in rows for p in row["pages"] if p.get("image_key")]
