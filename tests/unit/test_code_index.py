"""US-22: Python modules are outlined from their syntax tree (never run), resolved into a graph
with certain and inferred edges, and summarized as packs much smaller than the source; Graphify
output is read leniently; a question's identifiers pick the modules."""

import orjson
import pytest

from gateway.adapters.code.graphify import load_graphify
from gateway.adapters.code.python_ast import outline
from gateway.domain.code import (
    INFERRED,
    Pack,
    build_graph,
    choose,
    clean_path,
    module_name,
    question_terms,
    rank,
)
from gateway.domain.errors import InvalidInput
from gateway.domain.graph import Vertex
from gateway.pipeline.code import index_file

CART = '''"""The shopping cart."""
from shop.pricing import price_of
from . import shipping


class Cart(Base):
    """Items a customer will buy."""

    def total(self) -> int:
        """The cart's total price."""
        return sum(price_of(i) for i in self.items) + self.fee()

    def fee(self) -> int:
        return 0


async def checkout(cart: Cart, *, express: bool = False) -> str:
    return shipping.ship(cart)
'''


def test_a_module_is_outlined_from_its_syntax_tree() -> None:
    module = outline("shop/cart.py", CART)
    assert module is not None
    assert (module.name, module.doc) == ("shop.cart", "The shopping cart.")
    assert "shop.pricing.price_of" in module.imports
    assert "shop.shipping" in module.imports  # relative, resolved against the package
    by_name = {s.name: s for s in module.symbols}
    assert by_name["Cart"].signature == "class Cart(Base)"
    assert by_name["Cart.total"].signature == "def total(self) -> int"
    assert by_name["Cart.total"].doc == "The cart's total price."
    assert {"price_of", "fee", "sum"} <= set(by_name["Cart.total"].calls)
    assert by_name["checkout"].signature == (
        "async def checkout(cart: Cart, *, express: bool=False) -> str"
    )
    assert outline("broken.py", "def (:") is None
    assert outline("deep.py", "x = " + "(" * 200_000 + ")" * 200_000) is None  # not a crash


def test_paths_and_module_names() -> None:
    assert module_name("shop/__init__.py") == "shop"
    assert module_name("shop/cart.py") == "shop.cart"
    assert clean_path("shop\\cart.py") == "shop/cart.py"
    for bad in ("/etc/passwd.py", "../up.py", ""):
        assert clean_path(bad) is None


def test_the_graph_resolves_imports_and_calls_and_packs_are_small() -> None:
    pricing = outline("shop/pricing.py", '"""Prices."""\ndef price_of(item):\n    return 1\n')
    cart = outline("shop/cart.py", CART + "\n# padding\n" * 200)
    assert pricing is not None
    assert cart is not None
    graph = build_graph([cart, pricing])
    names = {v.id: v.name for v in graph.vertices}
    edges = {(names[e.src], names[e.dst], e.kind, e.confidence) for e in graph.edges}
    assert ("shop.cart", "shop.pricing", "imports", 1.0) in edges
    assert ("shop.cart", "Cart.total", "defines", 1.0) in edges
    assert ("Cart.total", "Cart.fee", "calls", 1.0) in edges  # in the same module: certain
    assert ("Cart.total", "price_of", "calls", INFERRED) in edges  # across modules: inferred
    (cart_pack,) = [p for p in graph.packs if p.path == "shop/cart.py"]
    assert "def total(self) -> int  # The cart's total price." in cart_pack.text
    assert "imports: shop.pricing" in cart_pack.text
    assert cart_pack.tokens < cart_pack.source_tokens / 3


def test_question_terms_split_names() -> None:
    terms = question_terms("How does ChatService.settle_hold work in the cart?")
    assert {"chatservice", "chat", "service", "settle_hold", "settle", "hold", "cart"} <= terms
    assert not {"how", "does", "the"} & terms


def test_modules_are_ranked_and_packed_within_the_budget() -> None:
    found = [
        Vertex(*_ids(1), "symbol", "Cart.total", "shop/cart.py"),
        Vertex(*_ids(2), "module", "shop.cart", "shop/cart.py"),
        Vertex(*_ids(3), "symbol", "price_of", "shop/pricing.py"),
    ]
    order = rank(found)
    assert order == ["shop/cart.py", "shop/pricing.py"]
    big, small = _pack("shop/cart.py", 900), _pack("shop/pricing.py", 50)
    assert choose([big, small], order, 500) == [small]  # the bigger one does not fit
    assert choose([big, small], order, 1_000) == [big, small]


def _ids(n: int) -> tuple[object]:
    from uuid import UUID

    return (UUID(int=n),)


def _pack(path: str, tokens: int) -> Pack:
    from uuid import uuid7

    return Pack(uuid7(), path, "x", tokens, tokens * 5)


def test_graphify_output_is_read_leniently() -> None:
    raw = {
        "nodes": [
            {"id": "f1", "type": "file", "label": "cart.py", "source_file": "shop/cart.py"},
            {"id": "c1", "type": "class", "label": "Cart", "source_file": "shop/cart.py"},
            {"id": "f2", "type": "file", "label": "pricing.py", "source_file": "shop/pricing.py"},
        ],
        "links": [
            {"source": "f1", "target": "c1", "relation": "contains", "confidence": "EXTRACTED"},
            {"source": "f1", "target": "f2", "relation": "imports", "confidence_score": 0.7},
            {"source": "c1", "target": "f2", "relation": "semantically_similar_to"},
            {"source": "f1", "target": "nope", "relation": "calls"},
        ],
    }
    loaded = load_graphify(raw)
    kinds = {(e.kind, e.confidence) for e in loaded.graph.edges}
    assert kinds == {("defines", 1.0), ("imports", 0.7)}  # a score is kept as given
    assert loaded.skipped == 2
    (pack,) = [p for p in loaded.graph.packs if p.path == "shop/cart.py"]
    assert "- Cart" in pack.text
    names = {v.name for v in loaded.graph.vertices}
    assert {"cart", "pricing", "Cart"} == names  # a file is found by its stem, as a module is
    with pytest.raises(ValueError, match="id"):
        load_graphify({"nodes": [{"id": ""}], "links": []})
    with pytest.raises(ValueError, match="nodes"):
        load_graphify({"links": []})


def test_an_uploaded_file_is_a_bundle_or_a_graph() -> None:
    bundle = {"files": {"shop/cart.py": CART, "../evil.py": "x = 1", "README.md": "hi"}}
    graph, report = index_file(orjson.dumps(bundle))
    assert (report.source, report.modules) == ("python", 1)
    assert set(report.skipped) == {"../evil.py", "README.md"}
    assert graph.packs
    for bad in (b"not json", b"[]", b'{"files": 5}'):
        with pytest.raises(InvalidInput):
            index_file(bad)
