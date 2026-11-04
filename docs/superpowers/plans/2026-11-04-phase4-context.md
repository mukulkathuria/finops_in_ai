# Phase 4 Context Engineering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** RAG answers carry only relevant passages within a token budget (US-21), and code
questions are answered from graph-based context packs instead of whole files (US-22), with an
MCP context tool for agents and IDEs.

**Architecture:** Retrieval asks for more candidates than it keeps; a `Reranker` scores them and
a relevance filter drops those under the org's threshold and packs the rest by score within a
token budget. The filter is a US-26 stage (`rag_relevance`, shadow by default, `on` after a
passed eval) whose report is built from the request notes at hold time (the `file_conversion`
pattern). Code lives in `projects` with document-style ACL groups; a `CodeIndexer` turns source
or Graphify output into `graph_vertex`/`graph_edge` rows and refreshes one `context_packs` row per
module. All graph reads go through `GraphStore` (ADR 0004: explicit joins for fixed depth,
recursive CTEs for variable depth; no SQL/PGQ on PostgreSQL 19).

**Tech Stack:** as Phases 1 to 3; bge-reranker behind a `/v1/rerank` HTTP API (Cohere/Jina
shape, served by vLLM, Infinity or TEI-compatible routers); the mock sidecar implements it.

**Spec:** `../AI FinOps Gateway End-to-End Build Specification_v2.md` sections 2 (graph rule),
4 (step 7, F4, F5), 5 (code and knowledge graph DDL, ACL inheritance), 7 (US-21, US-22),
8 (Phase 4 exit), 11 (latency targets: pack read p95 < 10 ms, 2-3 hop query p95 < 30 ms).

## Global Constraints

- Every new table: `org_id`, ENABLE + FORCE RLS, `tenant_isolation`, explicit grants, UUIDv7.
- Graph and context data inherit the project's ACL: a caller who cannot read a project gets no
  vertex, edge or pack of it, through any API, the chat path or the MCP tool.
- Business code never writes graph SQL: `GraphStore` is the only reader (spec section 2).
- A reranker failure is fail-open to the vector order (reported); it never fails the request.
- Tokens pruned and context tokens are reported in the ledger notes and as stage reports, so
  the savings report shows them per technique.
- Uploads are size-capped and parsed without executing anything (`ast`, JSON).

## Review Focus

1. A project the caller cannot read leaking through the context builder or the MCP tool:
   cross-team and cross-org tests on every entry point.
2. A reranker that is down or slow: the request is answered with the vector order and the
   ledger says `rerank: failed`.
3. The token budget smaller than the best passage: nothing over budget is sent and the report
   says so.
4. Re-indexing a project while a chat reads its packs: the reader sees the old or the new pack,
   never a mix (one transaction per index run).
5. A cyclic or very deep import graph: variable-depth traversal terminates (depth cap, cycle
   guard) within the latency budget.

---

### Task 1: Reranker port, relevance filter and the `rag_relevance` stage (US-21)

**Files:** `gateway/ports/rerank.py`, `gateway/adapters/rerank/http.py`,
`gateway/domain/retrieval.py` (`pack`), `gateway/pipeline/retrieval.py`, `gateway/api/openai.py`,
`gateway/domain/savings.py` (`relevance_report`), settings and `.env.example`
(`RERANKER_BASE_URL`, `RERANKER_MODEL`, `RERANKER_API_KEY`, `RERANKER_TIMEOUT_S`),
`sidecars/mock_llm/app.py` (`/v1/rerank`, lexical overlap), org settings `retrieval`
(`threshold`, `max_tokens`, `candidates`).

- `Reranker.rerank(query, passages) -> list[float]` (one score in [0, 1] per passage, in order).
- `pack(hits, scores, *, threshold, max_tokens, k) -> Packed(kept, dropped, pruned_tokens)`:
  drop under threshold, then take by score while the budget allows, at most `k`.
- Tests: unit (`pack` edge cases: empty, all below, budget smaller than the first passage);
  integration `test_us21_rag_relevance.py`: noise chunks dropped, gold chunks kept, pruned tokens
  in the ledger notes and the `rag_relevance` report; shadow sends the vector top-k unchanged;
  reranker down means vector order and `rerank: failed`.

### Task 2: Graph schema, `GraphStore` and the capability probe (US-22)

**Files:** `migrations/versions/0021_code_graph.py` (`projects`, `graph_vertex`, `graph_edge`
with the spec's covering indexes, `context_packs`), `gateway/ports/graph.py`,
`gateway/adapters/postgres/graph.py` (`JoinGraphStore`, `RecursiveCteGraphStore`,
`RoutedGraphStore`), `/readyz` reports `sql_pgq: absent|present`.

- `GraphStore.neighbors(project_id, ids, *, kinds, direction, depth<=3)`,
  `GraphStore.reachable(project_id, ids, *, kinds, max_depth, limit)`.
- Tests: both stores return the same answers on a seeded graph (contract suite); cycles and the
  depth cap terminate; another org's or team's project returns nothing.

### Task 3: Indexers, projects API and context packs (US-22)

**Files:** `gateway/ports/code.py` (`CodeIndexer`), `gateway/adapters/code/python_ast.py`,
`gateway/adapters/code/graphify.py`, `gateway/pipeline/code.py`, `control_plane/api/projects.py`
(`POST/GET /v1/projects`, `POST /v1/projects/{id}/index` with a zip or Graphify JSON,
`GET /v1/projects/{id}/context?q=`).

- An index run replaces the project's graph and packs in one transaction.
- A pack per module: path, docstring, signatures, imports, callers and callees, token count.

### Task 4: Context builder, chat integration and the eval (US-22)

**Files:** `gateway/pipeline/code.py` (`ContextBuilder`), `gateway/api/openai.py`
(`code_context: {project_id, max_tokens}`), `scripts/eval_code_context.py`,
`tests/integration/test_us22_code_context.py`.

- Question to symbols (name match, then pack embeddings), packs as single-row reads, one hop of
  neighbours' signatures, packed within the budget; notes record pack tokens and whole-file
  tokens; a `code_context` stage report.
- The eval indexes this repository and asks fixed questions: hit rate of expected modules and
  symbols, and pack tokens against whole-file tokens (both reported).

### Task 5: MCP context tool

**Files:** `gateway/api/mcp.py` (`POST /v1/mcp`, JSON-RPC 2.0: `initialize`, `tools/list`,
`tools/call` for `get_code_context` and `search_documents`), tests.

- API-key auth, the caller's ACL on every call, size limits, no streaming in this phase.

### Task 6: Phase 4 review loop

Two independent reviewers (correctness, security) over the phase diff; every High and Medium
finding fixed with a regression test and re-reviewed; record in `docs/audits/`.
