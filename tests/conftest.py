"""Shared pytest configuration.

Local runs pick up `.env` (CI sets variables directly). Nothing here talks to the network.
"""

import asyncio
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent

for key, value in dotenv_values(ROOT / ".env").items():
    if value is not None:
        os.environ.setdefault(key, value)


def pytest_asyncio_loop_factories(
    config: pytest.Config, item: pytest.Item
) -> Mapping[str, Any] | None:
    # psycopg's async mode cannot run on Windows' default Proactor loop.
    if sys.platform == "win32":
        return {"selector": asyncio.SelectorEventLoop}
    return None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        path = str(item.path)
        if f"{os.sep}integration{os.sep}" in path:
            item.add_marker(pytest.mark.integration)
        elif f"{os.sep}contract{os.sep}" in path:
            item.add_marker(pytest.mark.contract)
