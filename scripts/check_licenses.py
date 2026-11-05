"""License check for releases: no copyleft or unknown license in a runtime closure without approval.

The closures are:
- the gateway image's Python packages;
- the docparse sidecar's Python packages;
- the dashboard's production npm packages.

Each package's license is read from:
- for Python, the PEP 639 `License-Expression`, else the `License` field and the trove
  classifiers;
- for npm, the lockfile's `license` field.

In an SPDX expression, a choice (`OR`) passes when one side does. A copyleft, proprietary or
unknown license fails unless APPROVED names the package, with the reason it is acceptable.

  python scripts/check_licenses.py [--npm-lock web/package-lock.json] <site-packages or venv>...
"""

import argparse
import json
import re
import sys
from collections.abc import Iterator
from importlib.metadata import PackageMetadata, PathDistribution
from pathlib import Path
from typing import Final

RESTRICTIVE: Final = re.compile(
    r"GPL|MPL|EUPL|SSPL|CDDL|EPL-|OSL-|CC-BY-SA|Mozilla|GNU|Affero|Eclipse Public|Sleepycat"
    r"|Proprietary|Commercial",
    re.IGNORECASE,
)
# Weak copyleft, used unmodified as libraries: its obligations cover changes to its own files.
APPROVED: Final = {
    "certifi": "MPL-2.0: Mozilla's root certificates, unmodified",
    "orjson": "MPL-2.0 for part of its code, the rest Apache-2.0 or MIT; unmodified",
    "psycopg": "LGPL-3.0: the PostgreSQL driver, imported unmodified and replaceable in the image",
    "psycopg-binary": "LGPL-3.0: psycopg's prebuilt build, unmodified",
    "tqdm": "MPL-2.0 and MIT: a progress bar that huggingface-hub pulls in, unmodified",
}

Package = tuple[str, str, str | None, list[str]]  # name, version, SPDX expression, license names


def permissive(expression: str) -> bool:
    """Judge an SPDX expression. AND binds tighter than OR, and WITH keeps its base license.

    Raises IndexError or ValueError when the expression is malformed.
    """
    tokens: list[str] = re.findall(r"[()]|[^\s()]+", expression)
    position = 0

    def take() -> str:
        nonlocal position
        position += 1
        return tokens[position - 1]

    def peek(word: str) -> bool:
        return position < len(tokens) and tokens[position].upper() == word

    def either() -> bool:
        ok = both()
        while peek("OR"):
            take()
            ok = both() or ok
        return ok

    def both() -> bool:
        ok = one()
        while peek("AND"):
            take()
            ok = one() and ok
        return ok

    def one() -> bool:
        word = take()
        if word == "(":
            ok = either()
            if take() != ")":
                raise ValueError(expression)
            return ok
        if peek("WITH"):
            take()
            take()
        return not RESTRICTIVE.search(word)

    ok = either()
    if position != len(tokens):
        raise ValueError(expression)
    return ok


def judge(expression: str | None, names: list[str]) -> bool | None:
    """True when permissive, False when restrictive, None when nothing names a license."""
    if expression:
        try:
            return permissive(expression)
        except IndexError, ValueError:
            return None
    if not names:
        return None
    return not any(RESTRICTIVE.search(name) for name in names)


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _python_license(meta: PackageMetadata) -> tuple[str | None, list[str]]:
    """The SPDX expression if declared, else the names in the License field and classifiers."""
    expression = meta.get("License-Expression")
    if expression:
        return str(expression), []
    names = []
    first = str(meta.get("License") or "").strip().splitlines()[:1]
    if first and len(first[0]) <= 80 and first[0].strip().upper() != "UNKNOWN":
        names.append(first[0].strip())
    for classifier in meta.get_all("Classifier") or []:
        parts = [part.strip() for part in str(classifier).split("::")]
        if parts[0] == "License" and len(parts) >= 3:
            names.append(parts[-1])
    return None, names


def python_packages(root: Path) -> Iterator[Package]:
    """Each distribution installed in a site-packages directory, or in a virtualenv."""
    infos = sorted(root.glob("*.dist-info")) or sorted(
        root.glob("[Ll]ib/**/site-packages/*.dist-info")
    )
    if not infos:
        raise SystemExit(f"no installed distributions under {root}")
    for info in infos:
        meta = PathDistribution(info).metadata
        expression, names = _python_license(meta)
        yield str(meta["Name"]), str(meta["Version"]), expression, names


def npm_packages(lock: Path) -> Iterator[Package]:
    """Each production package of an npm lockfile (version 2 or 3)."""
    for path, entry in json.loads(lock.read_text(encoding="utf-8"))["packages"].items():
        if path and not entry.get("dev") and not entry.get("devOptional"):
            found = entry.get("license")
            name = path.rsplit("node_modules/", 1)[-1]
            yield name, str(entry.get("version")), None if found is None else str(found), []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="No unapproved copyleft in a runtime closure.")
    parser.add_argument("roots", nargs="*", type=Path, help="site-packages or virtualenv")
    parser.add_argument("--npm-lock", type=Path, action="append", default=[])
    args = parser.parse_args(argv)
    sources = [(str(root), python_packages(root)) for root in args.roots]
    sources += [(str(lock), npm_packages(lock)) for lock in args.npm_lock]
    refused: list[str] = []
    approved: set[str] = set()
    for source, packages in sources:
        count = 0
        for name, version, expression, names in packages:
            count += 1
            if judge(expression, names) is True:
                continue
            if normalized(name) in APPROVED:
                approved.add(f"{name} ({APPROVED[normalized(name)]})")
            else:
                shown = expression or "; ".join(names) or "no license declared"
                refused.append(f"{name} {version}: {shown} [{source}]")
        print(f"{count} packages in {source}")
    for line in sorted(approved):
        print(f"approved: {line}")
    for line in refused:
        print(f"REFUSED: {line}", file=sys.stderr)
    if refused:
        print("a copyleft, proprietary or unknown license needs approval", file=sys.stderr)
        return 1
    print("licenses: every other package is permissive")
    return 0


if __name__ == "__main__":
    sys.exit(main())
