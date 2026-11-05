# AI FinOps Gateway

A self-hostable AI control plane that every LLM, embedding, OCR and image-generation call passes
through, so an organization can **govern, optimize and account for AI spend**. It is
OpenAI-compatible inbound, multi-provider outbound, and backed by one PostgreSQL 19 database
(relational data, pgvector embeddings and a graph store).

> Specification: `AI FinOps Gateway End-to-End Build Specification_v2.md` (v2, 2026-11-02).
> Progress, open items and evidence: [`PROGRESS.md`](PROGRESS.md). Decisions: [`docs/adr/`](docs/adr).

## Architecture

```text
 clients (OpenAI SDK, apps, agents, dashboard)
        |
        v
 +--------------------------- gateway (stateless, scale horizontally) ---------------------------+
 |  data plane /v1/*            control plane /v1/admin/*, /v1/auth/*                            |
 |  ingress -> authn -> authz -> budget reserve -> guardrails -> cache -> context -> router       |
 |          -> provider (fallback chain) -> output guardrails -> commit (ledger + outbox)         |
 |  ports: LLMAdapter | DocumentParser | Classifier | Embedder | CacheStore | GraphStore | ...    |
 +-----------------------------------------------------------------------------------------------+
        |                 |                                   |
        v                 v                                   v
 PostgreSQL 19     providers / sidecars                 worker (outbox, janitor,
 (RLS, pgvector,   (OpenAI-compatible, Anthropic,        rollups, price sync)
  graph, ledger)    Gemini, mock, OCR, Laya, embedder)
```

Ports and adapters (ADR 0001): `gateway/domain` is pure logic, `gateway/ports` holds the
interfaces, `gateway/adapters` holds every vendor integration, `gateway/app` wires them from
config. `import-linter` enforces the direction of dependencies in CI.

Provider adapters are native by default (OpenAI-compatible, Anthropic, Gemini). LiteLLM is an
optional, pinned extra (`uv sync --extra litellm`, or `--build-arg EXTRAS=litellm` for the image)
for OpenAI-compatible providers. Switching is one line of config: `adapter: litellm` on a
provider in `config/gateway.yaml`, or `PROVIDER_ADAPTER_DEFAULT=litellm`. Both adapters pass the
same contract suite, and the switch test shows they give the same answers, usage and cost. The
ledger records which adapter served (`litellm:<provider>`). Selecting LiteLLM where it cannot
serve the provider fails at startup (ADR 0008).

## Quick start (dev stack)

Prerequisites: Docker with Compose v2, [uv](https://docs.astral.sh/uv/) (Python 3.14 is fetched
automatically), Node 24+ for the dashboard.

```bash
uv sync                                # Python dependencies (locked in uv.lock)
uv run python scripts/init_env.py      # writes .env with fresh random secrets (never overwrites)
docker compose up -d --build           # PostgreSQL 19 RC1, mock LLM, migrations, gateway
curl http://127.0.0.1:8080/readyz      # {"status":"ok","checks":{"database":"ok","objects":"ok"}}
open http://127.0.0.1:5173            # dashboard (sign in with the bootstrap admin)
```

If port 5432 is taken on your host, create the env file with
`POSTGRES_PORT=55432 uv run python scripts/init_env.py`. Every published port binds to
`127.0.0.1` only.

## Development

| Task | Command |
| --- | --- |
| Lint and format | `uv run ruff check . && uv run ruff format --check .` |
| Types | `uv run mypy` (strict) |
| Layer contracts | `uv run lint-imports` |
| Unit and contract tests | `uv run pytest tests/unit tests/contract` |
| Integration tests (real PostgreSQL 19) | `uv run pytest -m integration` (needs `TEST_DATABASE_URL` in `.env`) |
| Route authorization audit | `uv run python scripts/audit_routes.py` |
| Latency gate on seeded data (section 11) | `uv run python -m tests.load.bench_context` (needs `TEST_DATABASE_URL`; the `bench` workflow runs it at 1M chunks and 1M edges) |
| RLS coverage audit | `AUDIT_DATABASE_URL=... uv run python scripts/audit_rls.py` |
| Migrations | `uv run alembic upgrade head` (uses `MIGRATOR_DATABASE_URL`) |
| Dashboard (dev server, proxies `/v1` to the gateway) | `cd web && npm ci && npm run dev` |
| Dashboard checks | `cd web && npm run typecheck && npm test && npm run build` |

Integration tests create a disposable database per session, apply the same setup SQL as the
container bootstrap, migrate it, and connect through the real `app`, `worker` and
`readonly_analytics` roles, so Row-Level Security is tested exactly as it runs in production.

## Budgets and spend control

Every model call reserves its worst-case cost against each budget that applies (member, team,
org) before the provider is called, and settles the actual cost with the ledger row afterwards.
Concurrent requests can never push spending past a hard limit; a request that would gets
`429 budget_exceeded` naming the tightest scope and a link to ask for more.

| Endpoint | Who | Purpose |
| --- | --- | --- |
| `PUT /v1/admin/budgets` | org admin; team lead for member limits in their team | create or change a limit (day, week, month; usd, tokens, images) |
| `GET /v1/admin/budgets`, `DELETE /v1/admin/budgets/{id}` | admin, finance (read), team lead (own teams) | limits with committed, reserved and remaining for the current period |
| `GET /v1/admin/budget-alerts` | as above | the one soft-threshold alert per budget and period |
| `GET`/`PUT /v1/admin/budget-settings` | org admin | org timezone for periods; `overcommit` or `strict` allocation |
| `POST /v1/budget-requests`, `GET`, `POST /{id}/decision` | members ask; leads (admins for team budgets) decide once or permanently | requests for more budget |
| `GET /v1/me/usage` | any user, or an API key bound to a user | own usage and remaining budget, nobody else's |

Soft-threshold alerts reach your systems through a signed webhook: `PUT /v1/admin/webhook`
`{"url": "https://..."}` returns the signing secret once. Each delivery is a POST with
`X-FinOps-Event`, `X-FinOps-Delivery` (dedupe on it: delivery is at least once),
`X-FinOps-Timestamp` and `X-FinOps-Signature: v1=<hex HMAC-SHA256(secret, "<timestamp>.<body>")>`.
Receivers must be public https hosts; failed deliveries back off for up to 10 attempts
(`GET /v1/admin/webhook/deliveries` shows their state).

Send `Idempotency-Key: <id>` on `/v1/chat/completions` or `/v1/embeddings` and a retry replays
the first response (`idempotent-replayed: true`) instead of being charged again. The `worker`
compose service (`python -m worker`) returns budget holds of crashed requests, rebuilds counters
from the ledger and purges old idempotency keys.

## Guardrails (PII, secrets, prompt injection)

Every chat and embedding request is inspected after its budget hold and before the provider
sees it; answers are inspected on the way back, streams included.

| Check | Default | Other settings |
| --- | --- | --- |
| PII (email, phone, card with Luhn, IBAN, SSN, Aadhaar, PAN, IP) | `redact`: `[EMAIL_1]`, the same value keeps its number | `block`, `off` |
| Secrets (cloud, GitHub, OpenAI, Anthropic, Slack, Stripe keys, JWTs, private keys, gateway keys) | `block` | `redact` (`[REDACTED_SECRET]`) |
| Prompt injection (rules first, then the classifier) | `block` at probability 0.8 | `flag` (recorded only), `off`; `injection_threshold` 0.5 to 0.99 |
| Classifier unavailable | `closed`: 503 `guardrail_unavailable` | `open` (recorded as `failed_open`) |
| Model output | `redact` secrets and PII | `off` |
| Hosted classifier (Jev) may see prompt text | `true` | `false`: the check is skipped and recorded |

`GET`/`PUT /v1/admin/guardrails` reads and changes the org policy (admins change it, auditors
read it; every change is audited). Obfuscation is handled: zero-width characters, fullwidth
digits, base64 payloads, leetspeak, text hidden in Unicode tag characters, and English, French,
Spanish, Portuguese, German, Russian, Chinese and Hindi phrasings. A blocked request gets
`400 guardrail_blocked` naming the rules, or `400 prompt_injection`; it is recorded in the ledger
as `blocked` and never charged. Each decision is logged and stored in the ledger row's
`flags.guardrails` with its rule, backend and version, never the matched text; the metric is
`finops_guardrail_decisions_total`.

Classifier backend (`.env`): `CLASSIFIER_BACKEND=rules|laya|jev`. Laya runs locally
(`pip install "laya[serve]"`, then `laya-serve` on port 8000; set `LAYA_BASE_URL`); the dev stack
points at the mock provider's stand-in. Jev is hosted and needs `JEV_API_KEY` (the gateway refuses
to start with `jev` and no key). Deterministic rules always run first and always win.

## Files (uploads)

`POST /v1/files` (multipart `file` and `purpose`, as with the OpenAI SDK's `files.create`) stores
a file for the uploader's team after checking it from its bytes; `GET /v1/files`,
`GET /v1/files/{id}`, `GET /v1/files/{id}/content` and `DELETE /v1/files/{id}` list, read,
download and delete. Only the uploader's team (and the uploading user) can see a file.

| Check | Refusal (`error.code`) |
| --- | --- |
| Type sniffed from the content must match the extension (PDF, PNG, JPEG, GIF, WebP, DOCX, XLSX, PPTX, TXT, MD, CSV, JSON, HTML) | 422 `unsupported_file_type`, `file_type_mismatch` |
| Size (`MAX_UPLOAD_BYTES`, 25 MiB) | 413 `file_too_large`, 422 `empty_file` |
| PDF pages and slides (`MAX_FILE_PAGES`), checked in an isolated, time-limited process | 422 `too_many_pages`, `encrypted_file`, `corrupt_file`, `unprocessable_file` |
| Office archives: entries, unpacked size and compression ratio, every member decompressed under caps | 422 `archive_bomb` |
| Image size from the header (`MAX_IMAGE_PIXELS`) | 422 `image_too_large` |
| Malware test signature, inside archives too; optional ClamAV (`CLAMAV_ADDRESS`, fails closed) | 422 `malware_detected`, 503 `scanner_unavailable` |
| Names with a path or control and invisible characters | 422 `invalid_filename` |

Originals live in the object store (`OBJECT_STORE_PATH`, a volume in the dev stack) under a key
made from the file id, never from its name. Downloads are attachments with `nosniff` and a
sandboxing CSP.

## Attachments as Markdown (document parsing)

`GET /v1/files/{id}/markdown` converts a stored file once and serves it from the parse cache
afterwards; a chat message can attach files with `{"type": "file", "file": {"file_id": "file-..."}}`.
The gateway resolves them before the budget hold and the guardrails, so document text is checked
for PII, secrets and injections like anything a user types, and the provider receives Markdown in
a `<document name="...">` frame. Responses carry `x-finops-file-tokens-before` and
`x-finops-file-tokens-after`; the ledger row records backend, version and tokens per file. A
request its budgets cannot cover is refused before any file is converted (its worst-case hold is
tried and rolled back first), and one request takes at most 20 files and 20 MiB of page images
(`too_many_files`, `attachments_too_large`).

| Content | Converted by |
| --- | --- |
| Plain text, Markdown, CSV, JSON | used as it is (in the gateway) |
| DOCX, XLSX, PPTX, HTML | MarkItDown (`PARSER_BASE_URL`) |
| PDF pages with a text layer | the text layer, read by the OCR sidecar without the model |
| Scanned pages and images | the OCR backend named by `OCR_BACKEND`: GLM-OCR or PaddleOCR-VL |

A page read below `OCR_MIN_CONFIDENCE` keeps its original image (attached for a vision model) and
is flagged. Switching `OCR_BACKEND` needs a restart and no code change; the parse cache is keyed by
content hash, backend, backend version and options, so results of the two backends never mix and
every artifact records the backend that made it. The sidecar (`sidecars/docparse`) speaks one
contract (`POST /v1/parse`, `GET /v1/info`) and calls the OCR model through an OpenAI-compatible
server (vLLM) at `DOCPARSE_VLM_BASE_URL`; run one sidecar per backend in production. Choose the
backend on your own documents with `scripts/ocr_eval.py` ([docs/ocr-evaluation.md](docs/ocr-evaluation.md)).

## Retrieval under your permissions

`POST /v1/documents` indexes text (or a stored file, converted to Markdown) and shares it with
the org, teams, users or org-level roles; only org admins may share with groups they are not in
themselves. `POST /v1/retrieval/search` returns the nearest chunks the caller may see, and a chat
request adds them with `"retrieval": {"k": 5}` (OpenAI SDK: `extra_body`). The permission filter
runs inside the same SQL statement as the pgvector search (HNSW, cosine, iterative scans), never
after it, so the model never receives a row the caller may not see, whatever the prompt. Retrieved
passages reach the model as lower-trust reference data and pass through the guardrails; a passage
the org's policy would block on (a secret, PII under `block`, an injection under `block`) is
dropped instead of blocking the request, and recorded in the ledger (`flags.retrieval.dropped`).
When the index scan finds fewer than k chunks the caller may read, an exact search over those
chunks runs. Pages read with low confidence are not indexed (the response names them in
`warnings`). Embeddings come from an OpenAI-compatible server (`EMBEDDER_BASE_URL`, bge-m3, 1024
dimensions).

Relevance filtering (US-21) is the optimization stage `rag_relevance`: off by default, since it
adds a reranker call (`RERANKER_BASE_URL`, bge-reranker behind a `/rerank` API) to every
retrieval. Once switched on (after its eval run passed), a chat with `retrieval` works like this:

1. The gateway retrieves `candidates` passages by vector.
2. The reranker scores them, and passages under the org's `threshold` are dropped.
3. The best of the rest are packed within `max_tokens`, at most k.

Set the threshold, budget and candidates with `PUT /v1/admin/retrieval-settings` (`{"threshold":
0.3, "max_tokens": 4000, "candidates": 20}`). The response says how many passages went out
(`x-finops-retrieved-chunks`) and how many tokens the filter pruned against the k nearest
(`x-finops-pruned-tokens`). The ledger and the savings report show the saving as `rag_relevance`.
In shadow the k nearest go out and the report says what the filter would have pruned. If the
reranker fails, the k nearest go out and the ledger records `rerank: failed`.

## Code context (the code graph)

A code question needs the modules it names, not whole files. A project holds a code graph and
one context pack per module: its path, docstring, imports, importers and signatures, at a
fraction of the source's tokens. A project is shared like a document (`access`). Who can do what:

- Anyone who may read the project can ask for context.
- Only an org admin, the user who created the project, or the team whose key created it can
  index it.

A project's name is unique for its owner: the user who created it, or the team whose key did.

1. Create a project: `POST /v1/projects` (`{"name": "shop", "access": {"org": true}}`).
2. Upload its sources to `/v1/files` in one of two forms:
   - a source bundle that `python scripts/pack_sources.py <repo> bundle.json` writes;
   - Graphify's `graph.json`.
3. Index the project: `POST /v1/projects/{id}/index` (`{"file_id": "file-..."}`). Indexing
   replaces the graph and packs whole, in one transaction.
4. Ask: `GET /v1/projects/{id}/context?q=How does Cart compute its total?&max_tokens=4000`
   returns the packs of the best-matching modules within the budget, with their token counts
   and what the whole files would have cost.

How the indexers read the code:

- The Python indexer reads modules with the standard library's `ast` and never runs them. It
  parses in a separate process with none of the gateway's environment: at most two at a time,
  each stopped after 120 seconds and held to 2 GiB of memory (on Linux). A file over 512,000
  characters is left out, like one that does not parse.
- A call between modules gets confidence 0.6 when only a project-wide unique name resolves it.
- Graphify relations outside the gateway's edge kinds are skipped and counted.

Every graph read checks the project's ACL in the same statement (`GraphStore`, ADR 0004: joins
for fixed depth, recursive CTEs for reachability).

A chat request asks for the context with `"code_context": {"project_id": "...", "max_tokens":
4000}` (OpenAI SDK: `extra_body`):

- The packs go before the question as lower-trust reference data.
- So do the signatures the named symbols call in other modules, one hop along `calls` edges.
- A pack the org's policy would block on is dropped (`flags.code_context.dropped`).
- The response header `x-finops-code-context-tokens` says how many tokens the context took.
- The `code_context` stage report records the context's tokens next to the whole modules'
  (`whole_module_tokens`). It claims no saving: the caller never sent those files.

`python scripts/eval_code_context.py` indexes this repository and asks fixed questions. It
reports the hit rate (expected module and symbol in the context) and the context's tokens against
the whole modules'; a unit test holds the hit rate at 0.9 or more.

## MCP context tools

`POST /v1/mcp` serves the Model Context Protocol over Streamable HTTP, with an API key as the
bearer token. Each request gets one JSON response; the endpoint keeps no sessions and opens no
event streams. Two tools run under the credential's ACL:

- `get_code_context`: the context packs of the project modules a question names.
- `search_documents`: the passages of the documents the credential may read.

Protocol versions:

- 2026-07-28: `server/discover` describes the server. Each request carries its version in
  `_meta`, and the `MCP-Protocol-Version`, `Mcp-Method` and `Mcp-Name` headers must repeat the
  body (400 otherwise).
- 2025-11-25 and 2025-06-18: the `initialize` handshake, answered without a session.

Both tools screen what they return as a chat request does: a passage or pack the org's policy
blocks on is dropped and counted (`dropped`). Every answer is sent with `Cache-Control: no-store`.

A request with an `Origin` header is refused (403), so no web page can drive the tools. To add the
server to Claude Code: `claude mcp add --transport http finops https://<gateway>/v1/mcp --header
"Authorization: Bearer <key>"`.

## Agent workspace (shared memory for agents)

Agents working on one problem share a workspace instead of each searching again. A workspace is
shared like a document (`POST /v1/workspaces` with `access`). Tasks group the agents' work (`POST
/v1/workspaces/{id}/tasks`, with an optional `agent_token_budget`). Agents use the workspace
through the MCP endpoint (`/v1/mcp`):

| Tool | What it does |
| --- | --- |
| `put_fact`, `get_fact` | Record a fact (kept once per content; a `key` names it); get it whole by id or key |
| `search_workspace` | The facts nearest a query, as ranked snippets. The same agent repeating a search in a task gets the record (the fact ids) instead |
| `put_file_summary`, `get_file_summary` | One summary per file path, read instead of the file |
| `ask_agent`, `answer_question`, `read_inbox` | Questions between agents (see below) |

Questions between agents:

- The same question to the same agent in a task is one message; once answered, its answer
  comes back at once.
- An unanswered question asked a third time is refused as a loop.
- Follow-up chains stop at 3 hops.

Discovery tokens are what agents read to find things out:

- workspace reads;
- clarification;
- `search_documents` or `get_code_context` called with `workspace_id`, `task_id` and `agent`.

They accrue per task and agent, and `GET /v1/workspaces/{id}/tasks/{task_id}` reports them. A
task's `agent_token_budget` refuses any read that would take an agent past it.

## Image generation

`POST /v1/images/generations` takes the OpenAI request (`model`, `prompt`, `n` up to 4, `size`,
`quality`) and answers with base64 images (`b64_json`; `url` is not offered).

1. The prompt passes the guardrails, as any input does, and then moderation: the built-in rules
   (`MODERATION_BACKEND=rules`) or an OpenAI-compatible moderation API (`openai_compat`). A
   flagged prompt is a 400 `content_blocked` that names its categories. With no verdict (the
   API is down) the request fails closed with a 503. Neither charges anything.
2. Budgets in the `images` unit hold the images per member and per team, as money budgets hold
   the price. Both are reserved before the provider is called, so concurrent requests cannot pass
   a quota together.
3. Each image costs its catalog price for its size and quality: `capabilities.image_prices`,
   which the price list carries as `"image_prices": {"1024x1024/hd": 80000}`. A model without a
   table charges `price_image` for every image. The ledger row counts the images (`kind`
   `image`).
4. With the org's response cache on, the same prompt and options from the same caller come back
   from the cache at no cost (`x-finops-cache: exact`), sealed like cached chat answers.

## Optimization stages (shadow mode and the eval gate)

Every optimization is a stage with one contract (spec section 4): it may make the request cheaper
and reports the tokens before and after, the cost difference, its latency and its risk to answer
quality. `GET /v1/admin/stages` lists the stages with the org's mode and latest eval run;
`PUT /v1/admin/stages/{name}` sets `off`, `shadow` or `on`; `POST /v1/admin/stages/{name}/evals`
records an eval run (dataset, metric, score, threshold). A stage ships in `shadow`: it runs on a
copy and reports what it would have saved. `on` is accepted only while the latest eval run for the
stage's current version has passed (409 `eval_gate_not_passed`); recording a failing run takes a
stage that is on back to shadow (audited), and a new version of a stage runs in shadow again until
its own eval passes. Eval runs are attested by an org admin: the gate records that decision. A
stage that fails is skipped and reported, never failing the request. Reports are written to
`stage_reports` with the ledger row; a request that was not served saves nothing. The first stage,
`compression` (version 1.1), removes redundant spaces and blank lines from what users wrote and
leaves tool output, tabs, fenced code and indentation as they were. Text aligned in columns with
spaces loses its alignment.

## Response cache (exact and semantic)

`PUT /v1/admin/cache-settings` with `{"mode": "exact" | "semantic" | "off", "ttl_s": 86400,
"threshold": 0.95}` (off by default) serves repeated requests from the cache. The key covers:

- the org and the caller's scope (team and user);
- the model and every parameter;
- the system prompt and the whole conversation;
- the guardrail policy that inspected the request, and the detection rules' version.

So an answer never crosses an org or a permission scope, and never outlives a policy change.
Answers are stored sealed (AES-256-GCM, a data key per entry, bound to the org and the entry's
key); one that cannot be opened is a miss. The worker (required, as for budget holds) deletes
expired entries every `CACHE_JANITOR_INTERVAL_SECONDS`, and after each pass an org is back under
`CACHE_MAX_ENTRIES_PER_ORG`, its oldest entries going first. The worker sees when entries expire,
never what they hold.
`semantic` also matches a single-turn question by meaning (embeddings, cosine similarity at or
above the threshold) inside that same partition. A hit, streamed or not, is served without calling
the model and charges nothing. It carries `x-finops-cache: exact|semantic`, and the savings report
shows it as `exact_cache` or `semantic_cache` with what the answer would have cost. Only complete
answers without tool calls are kept, for `ttl_s` seconds.

Provider prompt caching is on by default; `"provider_cache": false` in the same settings turns it
off. Requests are marked for the provider's own cache. For Anthropic that means one breakpoint on
the system prompt (or, without one, the last tool) and, in a conversation, one on the newest turn,
so the next turn reads everything before it at the cached price. A single question is not marked:
writing it costs more than plain input and nothing would read it. OpenAI and Gemini cache on their
own. The ledger records cache reads and writes, and the savings report shows the result as
`provider_cache` (reads save; writes cost the premium).

## Routing (a cheaper model, with a quality check)

The router is the optimization stage `router`: it ships `off`, can run in `shadow`, and goes `on`
only after its eval run passed (see above). `PUT /v1/admin/routing-settings` with
`{"large_model": "provider/model", "small_model": "provider/model", "threshold": 0.35,
"judge_rate": 0.05, "tolerance": 0.15, "opt_out_teams": ["<team id>"]}` picks the two tiers (chat
models of one provider). A request for the large tier whose complexity scores below the threshold
goes to the small tier if that model can take the whole request (context window and output
limit). The score comes from rules: length, code or tools, reasoning words and conversation depth.

- The answer names the model that gave it (`model` and `x-finops-model`).
- The ledger records `route_tier` (`small`, or `large` when the router kept the request there),
  and the baseline stays at the model asked for, so the savings report shows the saving under
  `router`.
- A sampled share (`judge_rate`) of the requests the router sends to the small tier is answered by
  the other tier too, in the background. In shadow the small tier answers on the side, so quality
  is measured before cutover. The agreement of the two answers (the cosine similarity of their
  embeddings) is stored, and `GET /v1/reports/routing?start=&end=` shows the samples, the mean
  agreement and whether it is within the tolerance.
- Each check is charged as its own ledger row (`<request id>.judge`) with no baseline, so it counts
  against the router's savings. It is charged to the org, never to the caller's team, user or key
  budgets. At most eight run at once; past that a sample is skipped, and none starts while the
  gateway shuts down.
- A team in `opt_out_teams` is never routed.

## Savings (baseline versus actual)

Every ledger row records a baseline: the request as the client sent it (its input before any
optimization, attachments at their original size) and the output it got, priced at the org's
baseline model without discounts. Set the baseline model with `PUT /v1/admin/savings-settings`
(`{"baseline_model": "provider/model"}`); without one, each request's own model is its baseline.
The actual cost is what the provider charged plus the gateway's own work: OCR GPU seconds,
classifier calls and query embeddings, at `OCR_GPU_SECOND_MICRO_USD`, `CLASSIFIER_CALL_MICRO_USD`
and `EMBEDDING_MICRO_USD_PER_MTOK`. `GET /v1/reports/savings?start=&end=&group_by=team|model|day`
returns totals read from the ledger, so they always match it, and the savings by technique from
the stage reports. The dashboard's Savings page shows both. Finance and org admins see the whole
org; a team lead with `reports:read_team` passes `team_id`.

## Prices (the model catalog)

Prices are data. `model_catalog` holds one row per model and price change, and every ledger row
records the catalog version it was priced at. The worker keeps the catalog in step with a price
list, `PRICE_SYNC_SOURCE` (default `config/prices.json`, or an https URL), every
`PRICE_SYNC_INTERVAL_SECONDS`. Prices are micro-USD per million tokens (images: per image).

- For each model, the list's entry in effect now is compared with the catalog. A change becomes a
  new row in effect at once; the gateway sees it within a minute.
- An entry with a later `effective_from` takes effect at that time.
- A model the list leaves out keeps its rows.
- The whole list is rejected if any entry is invalid; a chat model must state its context window
  and output limit.
- A change that would make a paid model free, move a price more than tenfold or change a model's
  kind is refused and logged, so a broken list cannot switch budgets off. So is a new model
  without any price (price self-hosted models at their internal rate).
- A failed download is logged with its status only, never the URL. The list itself is operator
  configuration: protect its source.

## Limits

| Limit | Default (setting) | Refusal |
| --- | --- | --- |
| Requests per API key or user | 1,200 a minute (`RATE_LIMIT_PER_MINUTE`) | 429 `rate_limited`, with `retry-after` |
| Uploads, document ingests and Markdown conversions per API key or user | 60 a minute (`HEAVY_RATE_LIMIT_PER_MINUTE`) | 429 `rate_limited` |
| Request body; upload | 10 MiB (`MAX_REQUEST_BYTES`); 25 MiB (`MAX_UPLOAD_BYTES`) | 413 `request_too_large`, `file_too_large` |
| Text the input guardrails read per request | 2,000,000 characters (`GUARDRAIL_MAX_INPUT_CHARS`) | 413 `input_too_large` |
| Chunks in an organisation's retrieval index | 1,000,000 (`MAX_INDEXED_CHUNKS_PER_ORG`) | 403 `index_quota_exceeded` |
| Sign-in and signup attempts per client address | 30 a minute (`AUTH_RATE_LIMIT_PER_MINUTE`) | 429 `rate_limited` |

## Repository layout

| Path | Purpose |
| --- | --- |
| `gateway/` | Data plane, shared domain, ports, adapters, pipeline and the composition root |
| `control_plane/` | Admin API: orgs, teams, users, keys, budgets, policies, reports, audit |
| `worker/` | Outbox consumer and background jobs |
| `migrations/` | Alembic migrations (explicit SQL, RLS on every tenant table) |
| `sidecars/` | Separately deployed services (mock LLM, OCR, Laya classifier) |
| `infra/` | Dockerfiles, Terraform, Helm, observability config |
| `web/` | React 19 dashboard |
| `docs/adr/` | Architecture decision records |
| `docs/threat-model.md` | Threat model with control and test traceability |
| `agent-skills/` | Written procedures for recurring engineering tasks |
| `scripts/` | Audits and operational scripts |
| `tests/` | Unit, contract, integration, end-to-end and load tests |

## Important notes

- **PostgreSQL 19 and SQL/PGQ.** SQL/PGQ property graphs were reverted during the PostgreSQL 19
  beta cycle and are absent from 19 RC1 (verified, see ADR 0004). Graph reads go through the
  `GraphStore` port using indexed joins and recursive CTEs, which run on PG16+ including PG19.
- **Secrets** live only in `.env` (gitignored) or your secret manager. `.env.example` holds
  placeholders only. Provider keys are stored with envelope encryption and never returned.
- This is engineering guidance, not legal advice; have counsel review compliance claims.
