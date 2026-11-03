---
name: load-test
description: Run and interpret the load, spike, stress, soak and budget-race profiles.
---
# load-test

**Trigger:** before release; after any change to a hot path (auth, budgets, cache, pipeline).

**Inputs:** a production-like stack with the mock provider (`sidecars/mock-llm`), seeded data.

**Steps**
1. Start the stack with the mock provider latency set (`MOCK_LATENCY_MS_MIN`/`MAX`).
2. Seed data with `scripts/seed_load.py` (orgs, users, keys, budgets).
3. Run k6 (`tests/load/k6/*.js`) or Locust (`tests/load/locustfile.py`) from a separate machine.
   Profiles: baseline, spike, stress, soak, budget race, failure under load (spec section 11).
4. Read gateway overhead from `x-finops-overhead-ms` and Prometheus histograms, not client totals.
5. Compare with the thresholds in section 11; record before and after numbers for every tuning change.

**Done when**
- [ ] p95 and p99 overhead within budget; error rate under 0.1% (excluding intended 429s)
- [ ] Budget overshoot zero; no pool exhaustion; memory flat during soak
- [ ] Report committed under `docs/audits/`
