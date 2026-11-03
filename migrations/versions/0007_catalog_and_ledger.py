"""US-05: model and price catalog (data, not code) and the usage ledger.

Revision ID: 0007
Revises: 0006
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

# (provider, model, kind, in, out, cached_in, cache_write, image, tier_at, tier_in, tier_out,
#  context, max_out, source). Money: micro-USD per 1M tokens (ADR 0003); image: micro-USD each.
# Anthropic prices: first-party API list prices (cached 2026-10-06); Haiku 5.5 doubles past 100K.
SEED = [
    (
        "mock",
        "mock-small",
        "chat",
        150_000,
        600_000,
        75_000,
        None,
        None,
        None,
        None,
        None,
        8_192,
        4_096,
        "seed:mock",
    ),
    (
        "mock",
        "mock-large",
        "chat",
        2_500_000,
        10_000_000,
        1_250_000,
        None,
        None,
        None,
        None,
        None,
        128_000,
        16_384,
        "seed:mock",
    ),
    (
        "mock",
        "mock-embed",
        "embedding",
        20_000,
        0,
        None,
        None,
        None,
        None,
        None,
        None,
        8_192,
        0,
        "seed:mock",
    ),
    ("mock", "mock-image", "image", 0, 0, None, None, 40_000, None, None, None, 0, 0, "seed:mock"),
    (
        "anthropic",
        "claude-opus-5-5",
        "chat",
        4_000_000,
        20_000_000,
        200_000,
        5_000_000,
        None,
        None,
        None,
        None,
        1_000_000,
        128_000,
        "seed:anthropic-2026-10-06",
    ),
    (
        "anthropic",
        "claude-sonnet-5-5",
        "chat",
        2_000_000,
        10_000_000,
        200_000,
        2_500_000,
        None,
        None,
        None,
        None,
        1_000_000,
        128_000,
        "seed:anthropic-2026-10-06",
    ),
    (
        "anthropic",
        "claude-haiku-5-5",
        "chat",
        100_000,
        500_000,
        None,
        125_000,
        None,
        100_000,
        500_000,
        2_500_000,
        1_000_000,
        128_000,
        "seed:anthropic-2026-10-06",
    ),
    (
        "anthropic",
        "claude-fable-5-1",
        "chat",
        10_000_000,
        50_000_000,
        250_000,
        12_500_000,
        None,
        None,
        None,
        None,
        1_000_000,
        128_000,
        "seed:anthropic-2026-10-06",
    ),
]


def _lit(v: object) -> str:
    return (
        "NULL"
        if v is None
        else (str(v) if isinstance(v, int) else "'" + str(v).replace("'", "''") + "'")
    )


def upgrade() -> None:
    rows = ",\n".join(
        "(" + ", ".join(_lit(v) for v in row) + ", '2026-01-01T00:00:00Z')" for row in SEED
    )
    op.execute(f"""
    CREATE TABLE model_catalog (
      id                    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      provider              text NOT NULL CHECK (provider ~ '^[a-z0-9_-]{{1,40}}$'),
      model                 text NOT NULL CHECK (char_length(model) BETWEEN 1 AND 200),
      kind                  text NOT NULL CHECK (kind IN ('chat', 'embedding', 'image')),
      price_input           bigint NOT NULL CHECK (price_input >= 0),
      price_output          bigint NOT NULL CHECK (price_output >= 0),
      price_cached_input    bigint CHECK (price_cached_input >= 0),
      price_cache_write     bigint CHECK (price_cache_write >= 0),
      price_image           bigint CHECK (price_image >= 0),
      tier_threshold_tokens bigint CHECK (tier_threshold_tokens > 0),
      tier_price_input      bigint CHECK (tier_price_input >= 0),
      tier_price_output     bigint CHECK (tier_price_output >= 0),
      context_window        integer NOT NULL CHECK (context_window >= 0),
      max_output_tokens     integer NOT NULL CHECK (max_output_tokens >= 0),
      capabilities          jsonb NOT NULL DEFAULT '{{}}'::jsonb,
      effective_from        timestamptz NOT NULL DEFAULT now(),
      source                text NOT NULL,
      created_at            timestamptz NOT NULL DEFAULT now(),
      UNIQUE (provider, model, effective_from)
    );
    INSERT INTO model_catalog (provider, model, kind, price_input, price_output, price_cached_input,
      price_cache_write, price_image, tier_threshold_tokens, tier_price_input, tier_price_output,
      context_window, max_output_tokens, source, effective_from) VALUES
    {rows};
    -- Rows are never updated: a price change is a new row with a later effective_from.
    GRANT SELECT ON model_catalog TO app, worker, readonly_analytics;

    CREATE TABLE usage_events (
      id                 uuid NOT NULL DEFAULT uuidv7(),
      org_id             uuid NOT NULL,
      team_id            uuid,
      user_id            uuid,
      api_key_id         uuid,
      request_id         text NOT NULL,
      ts                 timestamptz NOT NULL DEFAULT now(),
      kind               text NOT NULL CHECK (kind IN ('chat', 'embed', 'image', 'ocr')),
      provider           text NOT NULL,
      model              text NOT NULL,
      adapter            text NOT NULL,
      catalog_version    bigint NOT NULL,
      input_tokens       bigint NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
      output_tokens      bigint NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
      cached_tokens      bigint NOT NULL DEFAULT 0 CHECK (cached_tokens >= 0),
      cache_write_tokens bigint NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),
      reasoning_tokens   bigint NOT NULL DEFAULT 0 CHECK (reasoning_tokens >= 0),
      images             bigint NOT NULL DEFAULT 0 CHECK (images >= 0),
      cost_micro_usd     bigint NOT NULL DEFAULT 0 CHECK (cost_micro_usd >= 0),
      baseline_micro_usd bigint NOT NULL DEFAULT 0 CHECK (baseline_micro_usd >= 0),
      overhead_micro_usd bigint NOT NULL DEFAULT 0 CHECK (overhead_micro_usd >= 0),
      cache_hit          text NOT NULL DEFAULT 'none' CHECK (cache_hit IN ('none', 'exact', 'semantic')),
      route_tier         text,
      status             text NOT NULL CHECK (status IN ('ok', 'error', 'blocked', 'cancelled')),
      estimated          boolean NOT NULL DEFAULT false,
      latency_ms         integer CHECK (latency_ms >= 0),
      flags              jsonb NOT NULL DEFAULT '{{}}'::jsonb,
      PRIMARY KEY (id, ts)
    ) PARTITION BY RANGE (ts);
    CREATE INDEX usage_events_org_ts ON usage_events (org_id, ts DESC);
    CREATE INDEX usage_events_team_ts ON usage_events (org_id, team_id, ts DESC);
    CREATE INDEX usage_events_user_ts ON usage_events (org_id, user_id, ts DESC);
    CREATE INDEX usage_events_request ON usage_events (org_id, request_id);
    SELECT partman.create_parent(
      p_parent_table := 'public.usage_events', p_control := 'ts', p_interval := '1 month',
      p_premake := 4, p_jobmon := false);
    UPDATE partman.part_config SET infinite_time_partitions = true
     WHERE parent_table = 'public.usage_events';

    ALTER TABLE usage_events ENABLE ROW LEVEL SECURITY;
    ALTER TABLE usage_events FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON usage_events
      USING (org_id = app_current_org()) WITH CHECK (org_id = app_current_org());
    CREATE POLICY worker_read ON usage_events FOR SELECT TO worker USING (true);
    -- The ledger is append-only for the request path.
    GRANT SELECT, INSERT ON usage_events TO app;
    GRANT SELECT ON usage_events TO worker, readonly_analytics;
    """)


def downgrade() -> None:
    op.execute("""
    DELETE FROM partman.part_config WHERE parent_table = 'public.usage_events';
    DROP TABLE usage_events CASCADE;
    DROP TABLE IF EXISTS partman.template_public_usage_events;
    DROP TABLE model_catalog;
    """)
