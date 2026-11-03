---
name: write-tests
description: Write tests that prove acceptance criteria, including negative and property-based cases.
---
# write-tests

**Trigger:** every code change.

**Steps**
1. Map each acceptance criterion of the story to at least one test; name tests after behaviour
   (`test_revoked_key_is_rejected_within_5_seconds`).
2. Add at least one negative case per feature (wrong role, foreign id, bad input, limit hit).
3. Pure logic: unit tests plus `hypothesis` properties for invariants (spend never exceeds the
   limit, cache keys differ across scopes, token counts never negative).
4. SQL, RLS, concurrency: integration tests in `tests/integration` against real PostgreSQL 19
   through the `app` role. Never test isolation as the table owner.
5. Adapters: extend the family contract suite with recorded fixtures; never call real providers.
6. No sleeps for synchronisation; poll with a deadline or use events.
7. Coverage: 85% branch overall, 95% on budgets, authorization, cache keys and crypto.

**Done when**
- [ ] Every assertion checks an outcome, not just that code ran
- [ ] Negative case present; suite green twice in a row (no flakes)
