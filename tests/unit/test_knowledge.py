"""ADR 0011: the text rules of graph retrieval, and relation extraction through a JSON schema
(against the mock model's deterministic extractor)."""

import httpx2
import pytest
from mock_llm.app import app as mock_provider

from gateway.adapters.extraction.openai_compat import OpenAICompatExtractor
from gateway.domain.knowledge import (
    DomainSchema,
    ExtractionFailed,
    Mention,
    Relation,
    centroid,
    check_types,
    clean,
    graph_norm,
    query_terms,
)

MEDICAL = DomainSchema(
    "medical",
    "diseases and how they show",
    ("disease", "symptom", "test"),
    ("has_symptom", "diagnosed_by"),
)


def _extractor(
    http: httpx2.AsyncClient, base: str = "http://mock-llm:8000/v1"
) -> OpenAICompatExtractor:
    return OpenAICompatExtractor(
        base, http=http, model="mock-small", api_key="dev-only", timeout_s=5
    )


def test_names_and_queries_meet_in_one_normal_form() -> None:
    assert graph_norm("  Joint   Pain, ") == "joint pain"
    terms = query_terms("Fever, rash and joint pain?")
    assert {"fever", "rash", "joint pain", "joint", "pain"} <= set(terms)
    assert "rash and joint pain" in terms  # up to four words
    assert all(len(t.split()) <= 4 for t in terms)
    assert query_terms("") == []


def test_types_are_snake_case_and_kept_once() -> None:
    assert check_types(["Disease", "symptom", "disease"], "entity types") == ("disease", "symptom")
    with pytest.raises(ValueError, match="underscores"):
        check_types(["blood test"], "entity types")


def test_only_the_domains_relations_between_named_entities_are_kept() -> None:
    dengue = Mention("Dengue", "disease")
    raw = [
        Relation(dengue, "has_symptom", Mention(" fever ", "symptom")),
        Relation(dengue, "has_symptom", Mention("Fever", "symptom")),  # the same, once
        Relation(dengue, "causes", Mention("fever", "symptom")),  # not a relation type
        Relation(Mention("Dengue", "planet"), "has_symptom", Mention("fever", "symptom")),
        Relation(dengue, "has_symptom", Mention("  ", "symptom")),  # no name
        Relation(dengue, "has_symptom", Mention("dengue", "disease")),  # itself
    ]
    assert clean(raw, MEDICAL) == (Relation(dengue, "has_symptom", Mention("fever", "symptom")),)


def test_the_intent_is_the_unit_mean_of_the_examples() -> None:
    assert centroid([[1.0, 0.0], [0.0, 1.0]]) == pytest.approx([0.7071, 0.7071], abs=1e-4)
    with pytest.raises(ValueError, match="example question"):
        centroid([])


async def test_the_extractor_reads_relations_through_a_json_schema() -> None:
    passage = "Dengue has symptom fever, rash and joint pain. Dengue diagnosed by NS1 antigen test."
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=mock_provider)) as http:
        found = await _extractor(http).extract(MEDICAL, passage)
    assert [(r.source.name, r.type, r.target.name, r.target.type) for r in found.relations] == [
        ("Dengue", "has_symptom", "fever", "symptom"),
        ("Dengue", "has_symptom", "rash", "symptom"),
        ("Dengue", "has_symptom", "joint pain", "symptom"),
        ("Dengue", "diagnosed_by", "NS1 antigen test", "test"),
    ]
    assert found.tokens > 0


async def test_a_model_that_cannot_answer_is_an_extraction_failure() -> None:
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=mock_provider)) as http:
        with pytest.raises(ExtractionFailed, match="404"):
            await _extractor(http, "http://mock-llm:8000/nowhere").extract(MEDICAL, "text")
