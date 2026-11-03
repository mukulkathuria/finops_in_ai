"""Create `.env` from `.env.example` with fresh random secrets. Never overwrites an existing file.

Every `CHANGE_ME` becomes a random URL-safe token; database URLs reuse the matching role password
and the POSTGRES_PORT value (override it with the POSTGRES_PORT environment variable, for example
when port 5432 is already taken on the host).
Usage: uv run python scripts/init_env.py [target-path]
"""

import os
import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLACEHOLDER = "CHANGE_ME"
ROLE_PASSWORD_KEYS = {
    "postgres": "POSTGRES_PASSWORD",
    "app": "FINOPS_APP_PASSWORD",
    "worker": "FINOPS_WORKER_PASSWORD",
    "migrator": "FINOPS_MIGRATOR_PASSWORD",
    "readonly_analytics": "FINOPS_READONLY_PASSWORD",
}
_LINE = re.compile(r"^(?P<key>[A-Z][A-Z0-9_]*)=(?P<value>\S*)(?P<rest>\s+#.*)?$")
_URL = re.compile(
    r"^postgresql://(?P<user>\w+):" + PLACEHOLDER + r"@(?P<host>[^:/]+):\d+/(?P<db>\w+)$"
)


def render(example: str, port: str | None = None) -> str:
    values: dict[str, str] = {}
    pg_port = "5432"
    for line in example.splitlines():
        m = _LINE.match(line)
        if m and m["value"] == PLACEHOLDER:
            values[m["key"]] = secrets.token_urlsafe(24)
        elif m and m["key"] == "POSTGRES_PORT":
            pg_port = m["value"]
    if port:
        values["POSTGRES_PORT"] = pg_port = port
    out = []
    for line in example.splitlines():
        m = _LINE.match(line)
        if not m:
            out.append(line)
            continue
        key, value, rest = m["key"], m["value"], m["rest"] or ""
        url = _URL.match(value)
        if url:
            password = values[ROLE_PASSWORD_KEYS[url["user"]]]
            value = f"postgresql://{url['user']}:{password}@{url['host']}:{pg_port}/{url['db']}"
        elif key in values:
            value = values[key]
        out.append(f"{key}={value}{rest}")
    return "\n".join(out) + "\n"


def main() -> int:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / ".env"
    if target.exists():
        print(f"{target} already exists; refusing to overwrite", file=sys.stderr)
        return 1
    text = render(
        (ROOT / ".env.example").read_text(encoding="utf-8"), os.environ.get("POSTGRES_PORT")
    )
    target.write_text(text, encoding="utf-8")
    target.chmod(0o600)
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
