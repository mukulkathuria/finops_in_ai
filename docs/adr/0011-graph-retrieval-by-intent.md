# ADR 0011: Graph retrieval for knowledge domains, chosen by intent

- Status: accepted
- Date: 2026-11-21
- Stories: US-13, US-21 follow-up (spec sections 3A, 4 F4)
- Builds on: ADR 0004, ADR 0010 (the graphs in PostgreSQL)

## Context
Retrieval was vector search only. The owner asked for graph retrieval, but "not every document
or search": only where the intent shows a graph serves better. Their example was medical books.
Symptoms and history point to a disease, and a disease to the blood test that confirms it. That
answer joins facts no single passage holds together, and a vector search ranks passages by
wording.

## Decision
- **Knowledge domains declare where a graph serves.** An org admin creates a domain
  (`/v1/admin/knowledge-domains`) with:
  - its entity types and relation types;
  - example questions;
  - an intent threshold.

  The examples' mean embedding is the domain's intent. Creating and deleting a domain is audited.
- **Only documents filed under a domain are extracted.**
  - `POST /v1/documents` takes `knowledge_domain`. Every other document stays vector-only, and
    the domain's graph holds only what its documents state.
  - The worker extracts in the background, one document at a time under a lease, a few chunks at
    once.
  - Extraction uses an OpenAI-compatible chat model with a strict JSON schema whose enums are the
    domain's types. Its output is checked again: a model's output is data.
  - A document's entities, relations and evidence (the chunks that state each relation) are
    stored in one transaction. A failure marks the document `failed`, with the reason, and
    stores nothing.
- **The graph is three tables in PostgreSQL** (`kg_entities`, `kg_relations`, `kg_evidence`,
  migration 0033), under the same forced row security as every tenant table. Entities are kept
  once per domain, type and normalized name.
- **Each query chooses its route in three steps**, each of which can send it back to the vector
  search. The report says which step did.
  1. **Intent:** the cosine of the query's embedding to the nearest domain's intent must reach
     the domain's threshold. The query is embedded once, for the vector search too, so this costs
     no extra embedding.
  2. **Entities:** the query must name entities of that domain's graph. Its runs of up to four
     words are matched against normalized entity names.
  3. **Evidence:**
     - Several named entities that meet in one entity (symptoms in a disease) centre the answer
       there, each centre weighted by the share of the named entities it meets.
     - Otherwise the named entities are the centre.
     - The passages are the chunks stating the centre's relations, a second hop included (the
       test that diagnoses the disease), best centre first. The vector search fills any room
       left up to k.
- **Permissions hold inside the graph.**
  - An entity, a candidate or a relation counts only through evidence in a document the caller
    may read.
  - A chunk is sent only if its own access groups match, the same filter as the vector search.
  - A caller cannot learn from the graph what a document it may not read says.
- **The route is visible:** `route` in the search response, `x-finops-retrieval-route` in chat,
  and the whole report in the ledger notes.

## Alternatives considered
- **An LLM classifier for intent:** a model call per query, on the request's critical path. The
  embedding is already made, and an admin can tune a threshold, not a prompt.
- **Extracting every document:** the extraction model is paid per chunk, and most documents do
  not answer relationship questions. Filing by domain keeps the cost where the graph pays off.
- **Entity linking by embeddings:** an embedding per entity and a vector search per query. Exact
  matching of normalized names is precise for named things (diseases, tests), costs nothing at
  query time, and the extraction model normalizes names as the documents spell them.

## Consequences
- **Cost:** the extraction model's tokens are counted per document (`graph_tokens`); the org
  pays its model provider for them directly. Extraction runs only when
  `GRAPH_EXTRACTOR_BASE_URL` and `GRAPH_EXTRACTOR_MODEL` are set; without them a document cannot
  be filed under a domain (`graph_extraction_off`).
- **Coverage:** synonyms the documents spell differently ("high temperature" and "fever") are
  different entities. A query about one does not find the other.
- **Not yet built:**
  - an embedding-based entity matcher, which could close the synonym gap;
  - the graph is SQL only, with no Apache AGE projection like ADR 0010's code and workspace
    graphs yet.
- **Tests:** `tests/integration/test_graph_rag.py` proves the route choice, the second hop a
  vector search misses, permissions inside the graph, and the fall-back once a domain is
  deleted. The mock model has a deterministic extractor for it.
