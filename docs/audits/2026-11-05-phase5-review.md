# Phase 5 review loop (spec section 12)

Scope: US-24 and US-25, and the Phase 4 latency tuning, `2113215..e70270e`:

- image generation: quotas, moderation, the cache and per-image prices;
- the agents' workspace and its MCP broker;
- migration 0026, which partitions `chunks` by org.

Two independent reviewers with fresh context read the diff.

- **Security:** tenancy, ACLs, quotas, moderation, injection, denial of service and the
  partitions.
- **Correctness and performance:** SQL semantics, settlement, the acceptance criteria and the
  latency gate.

The security review found one High that would lose data: migration 0026 copied `chunks` under
forced row-level security and kept none of its rows. Its second High priced any image size at a
flat model's base price. The correctness review found no Critical or High. Neither found a path
across tenants or teams.

Both Highs and every Medium are fixed, each with a test that fails before the fix. What is
accepted is recorded in ADR 0009 and in PROGRESS as D-28.

## Findings and resolution

S marks the security review, C the correctness review.

| Id | Severity | Finding | Resolution |
| --- | --- | --- | --- |
| S-A1 | High | Migration 0026 ran `INSERT ... SELECT * FROM chunks` as `migrator`, a NOBYPASSRLS owner bound by the forced tenant policy, in a session with no org: it copied nothing, then dropped the table. An empty CI database hid it | The copy lifts FORCE ROW LEVEL SECURITY, compares the counts before the drop, and locks `documents` then `chunks` first. CI then showed the second half: a foreign key added to a filled table is checked in bulk under forced RLS, and `orgs` looked empty. The new table now has its foreign keys before the rows arrive. `tests/integration/test_migrations.py` seeds two orgs at 0025 and migrates up, down and up as `migrator` (`53bfd47`, `a3da759`) |
| S-B1, C-I1 | High, Medium | A model priced per image without a table took any size and quality at that price, and defaults the provider might refuse were injected into every request | What is rendered is what is priced. With a table, an option left out is the cheapest offered with the ones named, and the provider is asked for it by name. Without a table, the model renders its default image only. Anything else is 422 `unsupported_image_option` (`test_what_is_rendered_is_what_is_priced`) |
| C-P1 | Medium | 0026 took no lock before its copy, so an ingest could commit between the copy and the drop | Fixed with S-A1 |
| C-W1 | Medium | A repeated search was answered from its record forever, so facts written after it were never found; an empty result was remembered too | The record keeps the workspace's fact count (migration 0027); a repeat after a new fact runs again (`test_c_w1_a_repeated_search_runs_again_once_a_fact_is_added`) |
| S-D1, C-W2 | Medium | Agent names were free labels: any workspace member could read another agent's inbox, answer for it, or spend its budget; fresh names got fresh budgets without limit | A name in a task belongs to the member that first used it (the user, or the team of a team key; `agent_claimed`). A task holds at most 100 agents (`test_s_d1_an_agents_name_in_a_task_belongs_to_the_member_that_first_used_it`). That reads without a task are not counted is accepted (ADR 0009) |
| S-D2 | Medium | Facts, summaries and answers carried only the label an agent gave | Each records the member behind it (`written_by`, `answered_by`). Hop chains and reworded questions are cooperative (ADR 0009) |
| S-C1, S-C2, C-W3 | Medium | Embedding before the ACL check; no bound on facts, summaries, questions, agents or remembered searches; nothing could remove a workspace | `put_fact` checks the workspace, the task, the agent and a known fact before embedding, and takes the heavy rate limit as a document ingest does. Search checks before embedding. Caps: 2,000 facts a workspace (exact, counted in the statement that adds one), 10,000 summaries; 100 agents, 5,000 questions and 5,000 remembered searches a task. `DELETE /v1/workspaces/{id}` (creator or admin) removes everything in a workspace (`test_s_c2_*`, `test_put_fact_takes_the_heavy_limit_before_any_embedding`) |
| S-E1, C-W4 | Medium | Workspace content reached agents with only frame neutralizing, while documents pass the passage screen | Facts, summaries and messages pass the organisation's passage screen as they are read. A blocked one is withheld (`content_withheld`, or counted in `withheld`). The policy is fetched before the transaction, and screening reads no database (`test_s_e1_what_the_workspace_returns_passes_the_passage_screen`). Writes are not screened, as documents are not (ADR 0009) |
| C-I2 | Medium | The price-sync guard did not check `image_prices`: a broken list could make an image option free | Each option is held to the price rules: no paid option made free or moved tenfold, no free option added to a paid model, and a switch between a flat price and a table compared on the default image. A new model priced only by its table is no longer refused as unpriced (`test_each_image_option_is_held_to_the_same_rules`) |
| S-F1 | Medium | The built-in moderation rules read the prompt as written: zero-width characters, look-alike letters, leetspeak and spaced letters passed | The rules also read the prompt folded: NFKC without invisible characters, Cyrillic and Greek look-alikes and leetspeak mapped to Latin letters, and letters spelled one at a time joined (`test_rules_see_through_spelling_tricks`) |
| S-G1, C-I4 | Medium, Low | Image answers of several MB each could fill the cache up to the per-org entry cap; they are also kept in idempotency records | An answer over 8 MiB of base64 is served but not cached (`test_an_oversized_image_answer_is_served_but_not_cached`). Idempotency records keep answers as they do for chat, purged after a day (ADR 0009) |
| C-I3 | Low | An image render that timed out was retried: the provider could bill twice while the ledger recorded once | A render is retried only when the provider refused to start it (a rate limit) (`test_a_render_is_retried_only_when_the_provider_refused_to_start_it`) |
| S-E2 | Low | `ask_agent` returned the answer bare in its structured result | Only the framed text carries it |
| C-P2 | Info | The partitioning gain depends on how tenants are spread | The bench report states its distribution. A skewed tenant is an open check (ADR 0009) |
| S-I1 | Info | The partitions have no grants and no RLS of their own | The migration test asserts that no role can read a partition directly |
| S-F2 | Low | `MODERATION_BASE_URL` may be `http` | Accepted: it is operator configuration, and internal sidecars (embedder, reranker, OCR) use `http` on the private network alike |
| S-G2 | Low | Image requests take the standard rate limit, not the heavy one | Accepted: images are held by money and image budgets, which the heavy limit exists to stand in for |
| S-H1 | Low | Workspace child tables reference tasks and workspaces without `org_id` in the key | Backlog: every statement checks the task within the workspace's ACL first, and RLS holds the org |

## Latency gate (spec section 11): the filtered vector search at 1M chunks

Measured by the `bench` workflow:

- 1M chunks spread evenly over 50 orgs;
- one client;
- 1,000 samples.

Every tuning change, with its numbers:

| Step | Change | p50 | p95 | Index | Build |
| --- | --- | --- | --- | --- | --- |
| Before | One `chunks` table, HNSW on `vector(1024)` for every org | 84 ms | 482 ms (miss) | 7.8 GB | 348 s |
| 1 | Migration 0026: hash-partitioned by org (16), HNSW on `halfvec(1024)` | 306.6 ms | 383.4 ms (miss) | 2.6 GB | 193 s |
| 2 | `enable_sort = off` in the search's own transaction (`941707c`) | 5.85 ms | 41.4 ms (pass; p99 56.9 ms) | 2.6 GB | 197 s |

Step 1 pruned each search to its org's partition ("Subplans Removed: 15"). The planner then
sorted all 20,000 of the org's chunks through the `(org_id, embedding_model)` index. It prices a
distance as cheap, but each distance read a TOASTed 4 KB vector: 70,000 buffers, 349 ms.

Step 2 makes the ordered HNSW scan win. A scan that comes back short still falls back to the
exact search. The other gates passed throughout: a pack read p95 1.7 ms, and 2 and 3 hops 2.4
ms and 3.4 ms.

## Re-review (`08351b8`, fixed in `e111306`)

Two fresh reviewers checked every fix in the code, both security and correctness.

- **Verified:** every S-* and C-* resolution above, with two partial ones (S-D1, S-E1). The
  reviewers checked:
  - the RLS lifting and re-forcing in 0026 and 0027;
  - the minimal grants;
  - the exact fact cap under READ COMMITTED (writers queue on the workspace row);
  - the claim race, where the first member wins;
  - the delete authorization and its cascade;
  - that screening covers every read;
  - that the image retries and the price guard behave as described.
- **New:** no Critical or High. One Medium came from both reviewers, and one only from the
  correctness review. The rest are Low or Info.

| Id | Severity | Finding | Resolution |
| --- | --- | --- | --- |
| R-S1, R-C5 | Medium | `ask` never bound its addressee: a member could take a name nobody had used yet and read or answer the questions meant for it, and the real agent then got `agent_claimed` | A question goes only to an agent that has joined the task (acted in it); otherwise 404 `agent_not_in_task` (`test_s_d1_*`) |
| R-C1 | Medium | `enable_sort = off` sends every search to the HNSW scan first; a tiny org beside large ones, or a narrow ACL, could walk up to 20,000 tuples before the exact fallback. Not measured | The index attempt stops after 5,000 tuples (`hnsw.max_scan_tuples`). The bench measures a 50-chunk org beside the large ones, and a caller who reads one team's chunks, against the same 50 ms gate |
| R-S3, R-C6 | Low | Any member could use up a task's 100 agent slots; a legacy name with no principal was never bound once a task was full | Slots are per member; a name from before 0027 is bound on its next use at any count |
| R-S4 | Low | A fact's key and source, and a summary's path, were neither screened nor neutralized | They pass the screen with the text (`test_s_e1_*`) |
| R-S8 | Info | A workspace deleted during a write surfaced a foreign-key violation as a 500 | Mapped to 404 where the transaction ends |
| R-C2 | Low | An omitted image option became the cheapest in the table even when the default was offered (a prompt alone could render 256x256) | The default comes first where the table offers it |
| R-C3, R-S6 | Low | A new image option at 1 micro-USD passed the guard and could become the price of an omitted option | Refused under a tenth of the cheapest option before. Keys were already format-checked by the price-list parser |
| R-C4 | Low | Moving from a flat price to a table alone was refused as "price_image would drop to zero" | The flat check is skipped where a table prices the images |
| R-C7 | Info | Test gaps | Added: the exact fact cap under 8 concurrent writers; the summary, question and remembered-search caps; withheld summaries and answers; the grants 0027's downgrade revokes |
| R-S2 | Low | A name used before 0027 has no principal, so the first member to use it again takes it | Accepted (ADR 0009): a one-time window for tasks created before the upgrade |
| R-S5 | Low | A flat-priced model leaves omitted options to the provider, whose default may be dearer (`quality: auto`) | Accepted: `price_image` is the price of the model's default rendering. A model whose default varies needs an `image_prices` table (README) |
| R-S7 | Info | `search_workspace` embeds under the standard limit | As document search does; the query is at most 4,000 characters |
| R-S9 | Info | Screening drops what a blocking rule matches; a redact-only policy leaves MCP tool output unredacted | Accepted (ADR 0009), as for the Phase 4 document and code tools: an org that needs agents kept from such values sets those rules to block |
