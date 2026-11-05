# Phase 5 (agents and image generation) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this
> plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** US-24 (image generation with quotas, moderation, caching and per-image cost) and US-25
(a shared agent workspace with a context broker, deduplication, loop control, per-agent budgets
and a discovery-token metric).

**Architecture:** Image generation is a third request kind beside chat and embeddings: it runs
through `ChatService` (`_begin` reserve, guardrails, provider call, `_end` settle), so budgets
(unit `images`), the ledger (`kind = 'image'`, `images`) and idempotency apply unchanged. A
`Moderator` port screens prompts before any spend. The workspace is a blackboard in PostgreSQL
(workspaces shared like projects) served by MCP tools on the existing `/v1/mcp` endpoint.

**Tech Stack:** FastAPI, async SQLAlchemy + psycopg3, PostgreSQL 19 + pgvector, httpx2.

**Spec:** `AI FinOps Gateway End-to-End Build Specification_v2.md` (F6, F7, US-24, US-25, Phase 5).

## Global Constraints

- Money is `bigint` micro-USD; budgets reserve the worst case before any spend and settle after.
- Every tenant table has `org_id`, RLS enabled and forced; ACL checks run in the same statement.
- The broker returns ranked, deduplicated, permission-filtered snippets, never whole files.
- Exit: US-24 and US-25 pass; discovery tokens per benchmark task drop against the no-workspace
  baseline; the image quota holds under concurrency.

## Review Focus

- Concurrent image requests against a small quota: exactly the quota succeeds, never more.
- A moderation backend that fails: image requests fail closed (no spend, clear error).
- A cached image answer reaches only the org that made it, and costs nothing.
- A fact or summary of a workspace the caller cannot read never comes back (search included).
- An agent repeating a question: the duplicate is answered from the record, a loop is refused.

---

### Task 1: Image generation (US-24)

**Files:** `gateway/domain/llm.py` (`ImageRequest`, `ImageResponse`), `gateway/domain/images.py`
(size and quality validation, per-image price from `price_image` and the catalog's
`capabilities.image_prices`, `image_demand`), `gateway/ports/llm.py` (`generate_images`),
adapters (`openai_compat`, `litellm`; Anthropic and Gemini refuse), `gateway/ports/moderation.py`,
`gateway/adapters/moderation/` (`rules`, `openai_compat`), `ChatService.generate_images`, the
image cache (sealed answers in `cache_entries`, exact key only), `POST /v1/images/generations`,
settings and `.env.example`, the mock's `/v1/moderations`.

- Moderation fails closed; a flagged prompt is a 400 `content_blocked` with its categories and a
  `blocked` ledger row that charges nothing.
- Tests: member and team image quotas, six concurrent requests against a quota of three, cost
  per image in the ledger, moderation by rules and by the mock's endpoint, a cache hit at zero
  cost for the same org only.

### Task 2: Workspace tables, service and broker (US-25)

**Files:** migration `0024_workspaces.py` (`workspaces`, `tasks`, `task_agents`, `facts`,
`file_summaries`, `agent_messages`), `gateway/domain/workspace.py`,
`gateway/adapters/postgres/workspace.py`, `gateway/pipeline/workspace.py`,
`gateway/api/workspaces.py`, MCP tools `search_workspace`, `get_fact`, `put_fact`,
`get_file_summary`, `put_file_summary`, `ask_agent`, `answer_question`, `read_inbox`.

- Facts deduplicate by content hash; a search repeated within a task is served from the task's
  record and costs no new discovery tokens; a question repeated within a task returns its
  record; the same agent asking it a third time is a `loop_detected` refusal; clarification
  chains stop at three hops.
- Discovery tokens (broker reads, `search_documents` and `get_code_context` with a `task_id`,
  clarification text) accrue per task and agent; a task's `agent_token_budget` caps each agent.
- Tests: agent B finds agent A's fact without re-search, deduplication, loop and hop refusals,
  per-agent budget, ACL denial, and the discovery comparison against a no-workspace task.

### Task 3: Phase 5 review loop

Two independent reviewers (security; correctness and performance), fixes with regression tests,
a re-review, `docs/audits/<date>-phase5-review.md`.
