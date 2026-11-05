# Phase 4 review loop (spec section 12)

Scope: US-21 and US-22, `e87676f..b1ef7fd`: RAG relevance filtering, the code graph, the Python
and Graphify indexers, context packs, the projects API, chat `code_context` and the MCP tools.
Two independent reviewers with fresh context read the diff. One reviewed security: tenancy, ACLs,
untrusted input, injection and the MCP endpoint. The other reviewed correctness and performance:
SQL semantics, savings, the protocol, resources and test validity. Neither found a Critical or
High issue, and the security review found no cross-tenant or cross-team path. Every Medium finding
and most Lows are fixed with a regression test; the accepted Lows are listed below and recorded in
PROGRESS as D-27.

## Findings and resolution

S marks the security review, C the correctness review.

| Id | Severity | Finding | Resolution |
| --- | --- | --- | --- |
| S-M1 | Medium | The frame-neutralizing pattern had two adjacent runs of optional gap characters: a `<` followed by many spaces cost quadratic time on the event loop (16,000 spaces took 7.6 s) | Both runs are possessive, so the scan is linear (`test_neutralizing_frames_takes_linear_time`: 100,000 spaces in well under a second) |
| S-M2 | Medium | A 20 MiB bundle was parsed with `ast` inside the gateway: about 275 bytes of memory per source byte, and the GIL held for seconds, for every tenant | Python is parsed in an isolated probe process: no gateway environment, stopped after 120 s, 2 GiB of address space on Linux, two at a time. A file over 512,000 characters is left out (`test_code_probe.py`, `test_big_or_repeated_modules_are_left_out_and_reported`) |
| C-M1 | Medium | The `code_context` report counted the whole modules as the request's original input, so the ledger claimed a saving against files the caller never sent | The report is cost-neutral: the context was asked for, like retrieved passages. The whole modules' tokens stay in its decisions for the US-22 comparison |
| C-M2, S-L1 | Medium | A docstring with an escaped NUL or a lone surrogate, or a path or label with NUL, failed the whole index with a 500 | Text that reaches the database is one clean line (control characters become spaces, surrogates are replaced), paths with control characters are refused, and a `DataError` maps to 400 (`test_text_reaching_the_database_is_one_clean_line`) |
| C-M3 | Medium | No evidence for the section 11 latency targets on seeded data | `tests/load/bench_context.py` seeds 1M chunks and 1M graph edges and measures the pack read, the 2 and 3 hop walks and the filtered vector search through the gateway's own queries, with EXPLAIN plans. The `bench` workflow runs it at full scale; the integration suite runs it at a tiny scale on every push |
| C-M4 | Medium | The fixed-depth joins multiply paths per hop: a hub at depth 3 from many start vertices could sort millions of rows | A walk of 2 or 3 hops starts from at most 20 vertices (`test_a_walk_of_two_or_three_hops_starts_from_few_vertices`) |
| C-M5 | Medium | A pack larger than the budget was silently left out, and a long pack was cut mid-line without a marker | Packs are cut at a line end with a "more lines" marker; the best-ranked module is cut to fit the budget rather than dropped (`test_a_pack_is_cut_at_a_line_end_never_dropped_for_its_size`) |
| C-M6, S-I1 | Medium | `find` applied its limit alphabetically before ranking, so a common name part could crowd out the module the question named | The names a question gives whole come first in SQL, and ranking adds 2 for them (`test_review_best_paths_find_order_indexes_and_concurrent_reindex`) |
| S-L2 | Low | `UNIQUE (org_id, name)` told anyone in the org, through a refused create, the name of a project they cannot read | Migration 0023: a name is unique per owner (the creating user, or the team whose key created it), and only that index maps to 409 (`test_us22_project_names_are_unique_per_owner_and_reveal_nothing`) |
| S-L3 | Low | Graphify `source_file` values and labels were stored unchecked: absolute paths, `..` or newlines could shape pack lines | Paths go through `clean_path` and are dropped when invalid; names become one clean line |
| S-L4 | Low | The MCP tools returned passages and packs without the org's passage screening, though their output reaches a model | Both tools screen as a chat request does and report what they dropped (`test_us22_mcp_tools_screen_passages_like_chat`) |
| S-I2 | Info | MCP answers carried no cache directive | `Cache-Control: no-store` on every answer |
| C-L1 | Low | Graph reads ordered by name without a unique tiebreak | Every order ends with the vertex id |
| C-L2 | Low | `a.py` and `a/__init__.py` (both `a`) collapsed silently | The second is left out and reported in `skipped` |
| C-L3 | Low | Identifiers outside ASCII were never terms | Identifiers in any script are terms (see D-27 for what still is not) |
| C-L6 | Low | The context read the project, the vertices and the packs in separate transactions | One transaction and one consistent snapshot |
| C-L7 | Low | The edge indexes did not cover `project_id`, so each first-hop row needed a heap fetch | Migration 0023 adds it to both INCLUDE lists |
| C-L8 | Low | `x-finops-pruned-tokens` could be negative | Clamped at 0 |
| C-L9 | Low | A reranker returning raw logits would make the 0 to 1 threshold meaningless | A score outside 0 to 1 is an unusable answer, and the rerank fails open to the vector order (`test_scores_outside_zero_to_one_fail_open`) |
| C-L10 | Low | No test proved the best path under LIMIT, or concurrent re-indexing | A diamond graph and two concurrent re-indexes are tested |

## Accepted risks (Low), recorded as D-27

- Questions find code by the identifiers they name (D-25). Names of two letters or fewer and very
  common words (`get`, `set`, `file`) are not terms, and a question without an identifier finds
  nothing. The eval's questions name identifiers, which is what it measures.
- An error answer to a request whose id cannot be read carries no `id` member: MCP's schema has
  no null request id, although JSON-RPC 2.0 would send `null`.
- Requests with an `Origin` header are refused, and 2025-03-26 clients (no version header) are
  not served (D-26).
- The one-hop signature lookup reads after the context's transaction. A re-index between the two
  reads can only leave the hop empty.
- The reranker's URL scheme and answer size are operator configuration: the server is trusted.
- A re-index rewrites a whole graph, so frequent re-indexes leave dead rows for autovacuum.
- A team key's project belongs to its team: a new key of the same team may index it, and a deleted
  team leaves it to org admins.

## Re-review (`84ea1ca`)

Both reviewers verified every fix above (C-L10's integration test by reading; it runs on CI).
New findings, none Critical or High:

| Id | Severity | Finding | Resolution |
| --- | --- | --- | --- |
| N-M1 | Medium | The probe's answer still became objects in the gateway with no bound until the whole graph was built: a 20 MiB bundle of tiny functions could reach about a million symbols | The probe stops at MAX_GRAPH modules and symbols, a symbol keeps at most 200 call names, an answer over 128 MiB is refused unparsed, and one index runs at a time per process (`test_outlines_stop_at_the_graph_limit_and_calls_are_capped`, `test_an_oversized_answer_is_refused`) |
| N-L1 | Low | A cancelled request left its probe running to the deadline, past the two-at-a-time bound | A cancelled call kills its probe, or never starts it, and frees its slot once the process is gone (`test_a_stopped_child_never_starts_and_a_running_one_is_killed`) |
| N1 | Low | The upload's JSON was parsed on the event loop again | It is parsed in a worker thread |
| N2 | Low | A project whose owner is gone may share a name, and the downgrade of 0023 fails while two owners share one | Kept and documented in the migration: forcing a name on orphaned projects would block deleting their user |
| N-L2, N3, N4, H1-H3 | Low, Info | Names repeat across owners in a listing; `find` sorts all matches of a common term before its limit; Python and the database lower-case non-ASCII alike only in a UTF-8 locale; the benchmark walks from one start vertex on a sparse graph, explains three queries and runs warm; the full run is heavy | Accepted (D-27): listings carry ids; the database must run a UTF-8 locale (the repo sets none, so the base image's default applies, and a C-locale cluster would not find non-ASCII names); the walk cap bounds the worst case; the Phase 6 load tests extend the benchmark |

The `bench` workflow's first runs exposed one harness defect, fixed before any measurement: it
dropped the index behind the chunks' unique key, which PostgreSQL refuses. Indexes that back a
constraint now stay during the load.
