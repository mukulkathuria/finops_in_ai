"""Bundle a repository's Python sources for code indexing (US-22).

    python scripts/pack_sources.py <repository> <bundle.json>

Upload the bundle to `/v1/files` and index a project from it
(`POST /v1/projects/{id}/index` with `{"file_id": ...}`). Hidden directories, virtual environments
and caches are left out; nothing is executed."""

import json
import sys
from pathlib import Path

_SKIPPED = frozenset({"venv", "node_modules", "__pycache__", "site-packages", "build", "dist"})


def bundle(root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*.py")):
        parts = path.relative_to(root).parts
        if any(p.startswith(".") or p in _SKIPPED for p in parts):
            continue
        files["/".join(parts)] = path.read_text(encoding="utf-8", errors="replace")
    return files


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    files = bundle(Path(argv[1]))
    Path(argv[2]).write_text(json.dumps({"files": files}), encoding="utf-8")
    print(f"{len(files)} Python files bundled into {argv[2]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
