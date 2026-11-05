"""The release license check (scripts/check_licenses.py): SPDX choices, fallbacks, approvals."""

import json
from pathlib import Path

import pytest

from scripts.check_licenses import judge, main, permissive


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("MIT", True),
        ("Apache-2.0 OR GPL-3.0-only", True),  # a choice passes when one side does
        ("MPL-2.0 AND (Apache-2.0 OR MIT)", False),  # AND keeps the copyleft part
        ("(MIT AND BSD-3-Clause) OR LGPL-2.1-or-later", True),
        ("GPL-2.0-only WITH Classpath-exception-2.0", False),  # WITH keeps the base license
        ("AGPL-3.0-or-later", False),
        ("LicenseRef-Proprietary", False),
    ],
)
def test_spdx_expressions(expression: str, expected: bool) -> None:
    assert permissive(expression) is expected


@pytest.mark.parametrize("broken", ["MIT AND", "(MIT", "MIT Apache-2.0"])
def test_a_malformed_expression_is_unknown(broken: str) -> None:
    assert judge(broken, []) is None


def test_names_are_judged_when_no_expression_is_declared() -> None:
    assert judge(None, ["Dual License", "Apache Software License", "BSD License"]) is True
    assert judge(None, ["MIT License", "GNU Lesser General Public License v3 (LGPLv3)"]) is False
    assert judge(None, []) is None


def _dist(site: Path, name: str, **fields: str) -> None:
    info = site / f"{name}-1.0.dist-info"
    info.mkdir()
    lines = ["Metadata-Version: 2.4", f"Name: {name}", "Version: 1.0"]
    lines += [f"{key.replace('_', '-')}: {value}" for key, value in fields.items()]
    (info / "METADATA").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_refuses_unapproved_copyleft_and_unknown_and_accepts_approved(tmp_path: Path) -> None:
    site = tmp_path / "site-packages"
    site.mkdir()
    _dist(site, "fine", License_Expression="MIT")
    _dist(site, "psycopg", License_Expression="LGPL-3.0-only")  # approved
    assert main([str(site)]) == 0
    _dist(site, "viral", License_Expression="GPL-3.0-only")
    _dist(site, "silent")
    assert main([str(site)]) == 1


def test_npm_lockfile_production_packages_only(tmp_path: Path) -> None:
    lock = tmp_path / "package-lock.json"
    packages: dict[str, dict[str, object]] = {
        "": {"name": "web"},
        "node_modules/react": {"version": "19.3.1", "license": "MIT"},
        "node_modules/devtool": {"version": "1.0.0", "license": "GPL-3.0-only", "dev": True},
    }
    lock.write_text(json.dumps({"lockfileVersion": 3, "packages": packages}), encoding="utf-8")
    assert main(["--npm-lock", str(lock)]) == 0
    packages["node_modules/viral"] = {"version": "1.0.0", "license": "AGPL-3.0-only"}
    lock.write_text(json.dumps({"lockfileVersion": 3, "packages": packages}), encoding="utf-8")
    assert main(["--npm-lock", str(lock)]) == 1
