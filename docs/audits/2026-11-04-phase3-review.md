# Phase 3 review loop (spec section 12)

Scope: US-17 to US-19, US-26, provider prompt caching, the price sync job and the optional LiteLLM
adapter, `12348a4..d5021c6`, reviewed by two independent reviewers with fresh context. One
reviewed security: tenancy, the cache, authorization, the price source and LiteLLM. The other
reviewed correctness: money, settlement, concurrency and SQL. Neither found a Critical or High
issue, and the security review found no cross-tenant path. Every Medium finding and most Lows
are fixed with a regression test; the accepted Lows are listed below and recorded in PROGRESS as
D-24.

## Findings and resolution

S marks the security review, C the correctness review.

| Id | Severity | Finding | Resolution |
| --- | --- | --- | --- |
| C-M1 | Medium | A disconnect or shutdown while the cache lookup (an embedding call) or the provider-cache settings read ran left the budget hold to the janitor for 15 minutes and kept an Idempotency-Key claimed | Both run inside the settle-on-failure guard: the hold is released and a `cancelled` row written (`test_m1_a_disconnect_during_the_cache_lookup_releases_the_hold`) |
| S-M1, C-L3 | Medium | Any earlier passing eval run authorized `on`, even after a later failing run; nothing demoted a stage that was on | The gate reads the latest run for the stage version, and recording a failing run takes a stage that is on back to shadow, audited with `reason: eval_failed` (`test_the_eval_gate_follows_the_latest_run`) |
| S-M2 | Medium | The worker role could read every tenant's cached answers, which no worker job needed | Migration 0021: the worker sees only ids, orgs and times of cache entries and may delete them (`test_cached_answers_are_sealed_and_the_worker_only_cleans_up`) |
| S-M3 | Medium | No per-org cap on cache entries, and expired entries were purged by 1% of requests in their own transaction | A worker cache janitor deletes expired entries in batches and trims an org over `CACHE_MAX_ENTRIES_PER_ORG` from its oldest entry; requests no longer purge |
| S-L9 | Low | Cached answers were stored as plain text (spec section 5 asks for stored text to be encrypted) | Answers are sealed with the envelope (AES-256-GCM, a data key per entry), bound to the org and the entry's key; an entry that cannot be opened is a miss |
| S-L4 | Low | The cache key could use a newer policy than the one that filtered the answer, and ignored the detection rules | The key uses the policy that inspected the request and `RULES_VERSION` (`test_cache_keys_change_with_the_detection_rules`) |
| S-L5 | Low | A failed price download logged the full URL, which may carry credentials or a token | The error carries only the HTTP status or the error type, with nothing chained (`test_security_l5_a_failed_download_never_names_the_url`) |
| S-L6 | Low | A model new to the catalog skipped every check; a chat model with a context window of 0 read as unlimited to the router | A new model without any price is refused; a chat model must state its context window and output limit |
| S-L7 | Low | `import litellm` reads a `.env` file into the process (its default mode, DEV), and the first import could happen inside a request | `LITELLM_MODE=PRODUCTION` before the import, which now happens at startup |
| S-L8 | Low | The router's quality checks spent the caller's team, user and key budgets | They are charged to the org only (`test_us19_routing.py` asserts it) |
| C-L2 | Low | Compaction touched tool output (JSON, TSV) and tabs, and was documented as lossless | Only user text is compacted, tabs and trailing tabs stay, the wording says what is lost; the stage is version 1.1, so it runs in shadow until its own eval passes |
| C-L4 | Low | A cache hit's technique saving used what the answer cost when made (after any provider discount), so techniques did not add up to the baseline | The saving is the answer's full price at the model that would answer (`test_l4_a_cache_hit_saves_its_full_price`) |
| C-L5 | Low | A request that was routed or compressed and then failed still reported its saving | Reports of a request that was not served keep what the stages did and save nothing |
| C-L6 | Low | Graceful shutdown awaited a snapshot of background tasks, so a stream finishing meanwhile could start a quality check after the database closed | No quality check starts while draining, and draining waits until nothing is left (`test_l6_drain_waits_for_late_writes_and_starts_no_quality_check`) |
| C-L7 | Low | The semantic lookup took the first row of a relaxed-order index scan, which need not be the nearest | The five nearest are re-ranked in a materialized CTE before one is taken |
| C-L8 | Low | A LiteLLM stream closed by the client was not closed upstream | The wrapper is closed in a `finally` (`test_l8_a_litellm_stream_is_closed_when_the_client_goes_away`) |

## Accepted risks (Low), recorded as D-24

- Eval runs are attested by an org admin (`ORG_MANAGE`): their score and threshold come from the
  caller, so the gate records a decision rather than proving one. The router's own judged
  agreement is reported, not wired to the gate.
- The price guard compares each sync with the current row: a list source that steps a price down
  tenfold per sync is not caught. The price list is operator configuration; protect its source
  (file permissions, a trusted https host).
- The `filelock>=4.2.1` override puts a LiteLLM dependency outside its declared range; the contract
  suite and the switch test run on it.
- Cached answers have no per-user erasure: rows carry hashes, not user ids, and the product has
  no user erasure flow yet. Retention is the org's TTL (at most 30 days) and answers are sealed.
- An org baseline model different from the requested model produces savings that no technique
  explains, by design: the baseline is the org's reference.
- A failed request's stage reports still count as applied (their saving is zero).
