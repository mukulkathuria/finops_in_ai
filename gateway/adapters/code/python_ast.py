"""Python modules read with the standard library's `ast` (US-22; it stands in for tree-sitter for
Python): imports, classes, functions and methods with their signatures, the first line of their
docstrings and the names they call. The source is parsed, never executed."""

import ast
from typing import Final

from gateway.domain.code import MAX_NAME, Module, Symbol, first_line, module_name
from gateway.domain.parsing import text_tokens

_MAX_SIGNATURE: Final = 300
Function = ast.FunctionDef | ast.AsyncFunctionDef


def _signature(node: Function) -> str:
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    returns = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    return f"{prefix} {node.name}({ast.unparse(node.args)}){returns}"[:_MAX_SIGNATURE]


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
    head = f"class {node.name}({bases})" if bases else f"class {node.name}"
    doc = first_line(ast.get_docstring(node))
    symbols = [Symbol(node.name[:MAX_NAME], "class", head[:_MAX_SIGNATURE], doc)]
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
