"""Python modules read with the standard library's `ast` (US-22; it stands in for tree-sitter for
Python): imports, classes, functions and methods with their signatures, the first line of their
docstrings and the names they call. The source is parsed, never executed, and only inside the
isolated outline probe (probe.py): never in the gateway's own process."""

import ast
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from gateway.domain.code import (
    MAX_MODULE_CHARS,
    MAX_MODULES,
    MAX_NAME,
    Module,
    Symbol,
    clean_path,
    first_line,
    module_name,
    one_line,
)
from gateway.domain.parsing import text_tokens

_MAX_SIGNATURE: Final = 300
Function = ast.FunctionDef | ast.AsyncFunctionDef


@dataclass(frozen=True, slots=True)
class Outlines:
    modules: tuple[Module, ...]
    skipped: tuple[str, ...]  # not Python, unparsable, too big, outside the project or repeated


def _signature(node: Function) -> str:
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    returns = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    return one_line(f"{prefix} {node.name}({ast.unparse(node.args)}){returns}", _MAX_SIGNATURE)


def _calls(node: ast.AST) -> tuple[str, ...]:
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            if isinstance(sub.func, ast.Name):
                names.add(sub.func.id)
            elif isinstance(sub.func, ast.Attribute):
                names.add(sub.func.attr)
    return tuple(sorted(names))


def _function(node: Function, owner: str) -> Symbol:
    name = (f"{owner}.{node.name}" if owner else node.name)[:MAX_NAME]
    doc = first_line(ast.get_docstring(node))
    return Symbol(name, "function", _signature(node), doc, _calls(node))


def _class(node: ast.ClassDef) -> list[Symbol]:
    bases = ", ".join(ast.unparse(b) for b in node.bases)
    head = one_line(
        f"class {node.name}({bases})" if bases else f"class {node.name}", _MAX_SIGNATURE
    )
    doc = first_line(ast.get_docstring(node))
    symbols = [Symbol(node.name[:MAX_NAME], "class", head, doc)]
    symbols += [
        _function(item, node.name)
        for item in node.body
        if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    return symbols


def _imports(tree: ast.Module, package: list[str]) -> tuple[str, ...]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative to the module's package
                if node.level - 1 > len(package):
                    continue
                base = package[: len(package) - (node.level - 1)]
                prefix = ".".join([*base, node.module] if node.module else base)
            else:
                prefix = node.module or ""
            if prefix:
                names.append(prefix)
                names += [f"{prefix}.{alias.name}" for alias in node.names if alias.name != "*"]
    return tuple(dict.fromkeys(names))


def outline(path: str, source: str) -> Module | None:
    """A module's outline, or None when it does not parse."""
    try:
        tree = ast.parse(source, filename=path)
        name = module_name(path)
        parts = name.split(".")
        package = parts if path.endswith("__init__.py") else parts[:-1]
        symbols: list[Symbol] = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                symbols.append(_function(node, ""))
            elif isinstance(node, ast.ClassDef):
                symbols += _class(node)
        return Module(
            path,
            name,
            first_line(ast.get_docstring(tree)),
            _imports(tree, package),
            tuple(symbols),
            text_tokens(source),
        )
    except SyntaxError, ValueError, RecursionError, MemoryError:
        return None


def outline_all(files: Mapping[object, object]) -> Outlines:
    """The outlines of a source bundle's Python modules, in path order. Left out: what is not a
    `.py` file inside the project, does not parse, is over MAX_MODULE_CHARS, or has a dotted name
    another file took first (`a.py` and `a/__init__.py` are both `a`)."""
    if len(files) > MAX_MODULES:
        raise ValueError(f"`files` maps at most {MAX_MODULES} paths to sources")
    modules: list[Module] = []
    skipped: list[str] = []
    names: set[str] = set()
    for raw_path, source in sorted(files.items(), key=lambda item: str(item[0])):
        path = clean_path(str(raw_path))
        module = None
        if (
            path is not None
            and path.endswith(".py")
            and isinstance(source, str)
            and len(source) <= MAX_MODULE_CHARS
        ):
            module = outline(path, source)
        if module is None or module.name in names:
            skipped.append(one_line(str(raw_path), 200))
            continue
        names.add(module.name)
        modules.append(module)
    return Outlines(tuple(modules), tuple(skipped))
