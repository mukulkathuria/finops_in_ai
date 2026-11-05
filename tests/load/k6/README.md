# Load profiles (spec section 11)

k6 scripts against the running stack. The mock provider answers in a fixed delay, and the
gateway's overhead is a request's time beyond it.

That overhead includes the embedding of each question, since the semantic cache embeds it,
in shadow mode too. The mock answers embeddings in 10 ms, as a GPU embedding server does for a
short query. The workflow's job summary lists the average time of every gateway stage.

| Profile | Script | What it proves |
| --- | --- | --- |
| Baseline | `baseline.js` | At the target load: overhead p95 ≤ 100 ms and p99 ≤ 250 ms; errors under 0.1% |
| Spike | `spike.js` | From 30% to 150% of the rate in a minute: errors under 1%; back within the SLO right after |
| Soak | `baseline.js` with `VUS=500 HOLD=8h` | Hours at half the load: the same thresholds, and no leak or drift (watch memory, the pool, counters) |
| Budget race | `race.js` | 500 requests at once against a small budget: only 200 or 429 `budget_exceeded`, committed never past the limit |
| Failure under load | `failover.js` | PostgreSQL restarts mid-run: within two minutes, errors under 0.1% and overhead within the SLO again |

## Running them

Start the stack with open signup and a fixed provider delay:

```bash
python3 scripts/init_env.py
printf '%s\n' SIGNUP_MODE=open MOCK_LATENCY_MS_MIN=50 MOCK_LATENCY_MS_MAX=50 \
  MOCK_EMBED_LATENCY_MS=10 >> .env
docker compose up -d --build --wait
```

Then run a profile, scaled as you need:

```bash
tests/load/k6/run.sh baseline -e VUS=1000 -e RAMP=10m -e HOLD=30m   # the spec's baseline
tests/load/k6/run.sh spike -e RATE=500
tests/load/k6/run.sh race
(sleep 60 && docker compose restart db) & tests/load/k6/run.sh failover; wait
```

The `load` workflow runs every profile at CI scale (100 users, 60 requests a second) each week
and on any change to these scripts. Each run adds a summary to its job page. A soak needs
hours, so it runs by hand.

Each profile creates its own org, team and keys through the API. Every user has its own key, so
the per-credential rate limit is not what is measured.
