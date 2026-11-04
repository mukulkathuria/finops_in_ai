"""Uploaded files (US-14). Rows are org-scoped by RLS; who may see a file inside the org (its
team, or the user who uploaded it) is decided here, in every query."""

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from gateway.adapters.postgres.database import fetch_all, fetch_one


@dataclass(frozen=True, slots=True)
class NewFile:
    id: UUID
    team_id: UUID | None
    user_id: UUID | None
    api_key_id: UUID | None
    filename: str
    kind: str
    mime: str
    size: int  # bytes
    sha256: bytes
    pages: int | None
    purpose: str
    storage_key: str


@dataclass(frozen=True, slots=True)
class FileRecord:
    id: UUID
    team_id: UUID | None
    user_id: UUID | None
    filename: str
    kind: str
    mime: str
    size: int  # bytes
    sha256: bytes
    pages: int | None
    purpose: str
    storage_key: str
    created_at: datetime


_INSERT: Final = """
INSERT INTO files (id, org_id, team_id, user_id, api_key_id, filename, kind, mime, size,
                   sha256, pages, purpose, storage_key)
VALUES (:id, app_current_org(), :team_id, :user_id, :api_key_id, :filename, :kind, :mime,
        :size, :sha256, :pages, :purpose, :storage_key)
RETURNING id, team_id, user_id, filename, kind, mime, size, sha256, pages, purpose,
          storage_key, created_at
"""
_GET: Final = """
SELECT id, team_id, user_id, filename, kind, mime, size, sha256, pages, purpose, storage_key,
       created_at
FROM files
WHERE id = :id AND (team_id = :team_id OR user_id = :user_id)
"""
_LIST: Final = """
SELECT id, team_id, user_id, filename, kind, mime, size, sha256, pages, purpose, storage_key,
       created_at
FROM files
WHERE (team_id = :team_id OR user_id = :user_id)
  AND (CAST(:after AS uuid) IS NULL OR id < CAST(:after AS uuid))
ORDER BY id DESC
LIMIT :limit
"""
_DELETE: Final = """
DELETE FROM files
WHERE id = :id AND (team_id = :team_id OR user_id = :user_id)
RETURNING storage_key
"""


class FileRepository:
    async def insert(self, conn: AsyncConnection, new: NewFile) -> FileRecord:
        row = await fetch_one(conn, _INSERT, asdict(new))
        if row is None:
            raise RuntimeError("file insert returned no row")
        return FileRecord(**dict(row))

    async def get(
        self, conn: AsyncConnection, file_id: UUID, *, team_id: UUID | None, user_id: UUID | None
    ) -> FileRecord | None:
        row = await fetch_one(conn, _GET, {"id": file_id, "team_id": team_id, "user_id": user_id})
        return FileRecord(**dict(row)) if row is not None else None

    async def list(
        self,
        conn: AsyncConnection,
        *,
        team_id: UUID | None,
        user_id: UUID | None,
        after: UUID | None,
        limit: int,
    ) -> list[FileRecord]:
        rows = await fetch_all(
            conn,
            _LIST,
            {"team_id": team_id, "user_id": user_id, "after": after, "limit": limit},
        )
        return [FileRecord(**dict(row)) for row in rows]

    async def delete(
        self, conn: AsyncConnection, file_id: UUID, *, team_id: UUID | None, user_id: UUID | None
    ) -> str | None:
        row = await fetch_one(
            conn, _DELETE, {"id": file_id, "team_id": team_id, "user_id": user_id}
        )
        return str(row["storage_key"]) if row is not None else None
