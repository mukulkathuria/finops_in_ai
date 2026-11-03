"""US-28: append-only audit log with a per-org SHA-256 hash chain, partitioned by month.

Revision ID: 0003
Revises: 0002
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE audit_log (
      id          uuid NOT NULL DEFAULT uuidv7(),
      org_id      uuid NOT NULL,          -- no FK: the audit trail outlives the objects it records
      seq         bigint NOT NULL CHECK (seq > 0),
      ts          timestamptz NOT NULL,
      actor_id    uuid,
      actor_type  text NOT NULL CHECK (actor_type IN ('user', 'api_key', 'system', 'anonymous')),
      action      text NOT NULL CHECK (action ~ '^[a-z_]+([.][a-z_]+)+$'),
      target_type text,
      target_id   text,
      details     jsonb NOT NULL DEFAULT '{}'::jsonb,
      ip          inet,
      request_id  text,
      prev_hash   bytea NOT NULL CHECK (octet_length(prev_hash) = 32),
      hash        bytea NOT NULL CHECK (octet_length(hash) = 32),
      PRIMARY KEY (id, ts)
    ) PARTITION BY RANGE (ts);
    CREATE INDEX audit_log_org_seq ON audit_log (org_id, seq);

    SELECT partman.create_parent(
      p_parent_table := 'public.audit_log', p_control := 'ts', p_interval := '1 month',
      p_premake := 4, p_jobmon := false);
    UPDATE partman.part_config SET infinite_time_partitions = true
     WHERE parent_table = 'public.audit_log';

    CREATE TABLE audit_chain_head (
      org_id    uuid PRIMARY KEY,
      last_seq  bigint NOT NULL,
      last_hash bytea NOT NULL CHECK (octet_length(last_hash) = 32)
    );

    -- Injective canonical form: a JSON array, so no field value can shift a delimiter.
    CREATE FUNCTION audit_canonical(
      p_seq bigint, p_ts timestamptz, p_org uuid, p_actor_id uuid, p_actor_type text,
      p_action text, p_target_type text, p_target_id text, p_details jsonb, p_ip inet,
      p_request_id text) RETURNS bytea
      LANGUAGE sql STABLE PARALLEL SAFE
      AS $$ SELECT convert_to(jsonb_build_array(
               p_seq, to_char(p_ts AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
               p_org, p_actor_id, p_actor_type, p_action, p_target_type, p_target_id,
               p_details, host(p_ip), p_request_id)::text, 'UTF8') $$;

    -- Runs as the caller (SECURITY INVOKER), so RLS confines it to app.org_id. The chain head row
    -- lock serialises appends per org; it commits or rolls back with the audited change.
    CREATE FUNCTION audit_append(
      p_actor_id uuid, p_actor_type text, p_action text, p_target_type text, p_target_id text,
      p_details jsonb, p_ip inet, p_request_id text) RETURNS bigint
      LANGUAGE plpgsql
      AS $$
    DECLARE
      v_org  uuid := app_current_org();
      v_ts   timestamptz := now();
      v_seq  bigint;
      v_prev bytea;
      v_hash bytea;
    BEGIN
      IF v_org IS NULL THEN
        RAISE EXCEPTION 'audit_append requires app.org_id' USING ERRCODE = 'insufficient_privilege';
      END IF;
      INSERT INTO audit_chain_head (org_id, last_seq, last_hash)
        VALUES (v_org, 0, decode(repeat('00', 32), 'hex')) ON CONFLICT (org_id) DO NOTHING;
      SELECT last_seq + 1, last_hash INTO v_seq, v_prev
        FROM audit_chain_head WHERE org_id = v_org FOR UPDATE;
      v_hash := sha256(v_prev || audit_canonical(v_seq, v_ts, v_org, p_actor_id, p_actor_type,
                         p_action, p_target_type, p_target_id, p_details, p_ip, p_request_id));
      INSERT INTO audit_log (org_id, seq, ts, actor_id, actor_type, action, target_type,
                             target_id, details, ip, request_id, prev_hash, hash)
        VALUES (v_org, v_seq, v_ts, p_actor_id, p_actor_type, p_action, p_target_type,
                p_target_id, p_details, p_ip, p_request_id, v_prev, v_hash);
      UPDATE audit_chain_head SET last_seq = v_seq, last_hash = v_hash WHERE org_id = v_org;
      RETURN v_seq;
    END $$;

    -- Recomputes the chain: detects edited fields, deleted or inserted rows and a truncated tail.
    CREATE FUNCTION audit_verify(p_org uuid)
      RETURNS TABLE (ok boolean, checked bigint, first_bad_seq bigint)
      LANGUAGE plpgsql STABLE
      AS $$
    DECLARE
      r      record;
      v_prev bytea := decode(repeat('00', 32), 'hex');
      v_n    bigint := 0;
    BEGIN
      FOR r IN SELECT * FROM audit_log a WHERE a.org_id = p_org ORDER BY a.seq LOOP
        IF r.seq <> v_n + 1 OR r.prev_hash <> v_prev OR r.hash <> sha256(v_prev || audit_canonical(
             r.seq, r.ts, r.org_id, r.actor_id, r.actor_type, r.action, r.target_type,
             r.target_id, r.details, r.ip, r.request_id)) THEN
          RETURN QUERY SELECT false, v_n, r.seq;
          RETURN;
        END IF;
        v_prev := r.hash;
        v_n := v_n + 1;
      END LOOP;
      IF EXISTS (SELECT 1 FROM audit_chain_head h
                  WHERE h.org_id = p_org AND (h.last_seq <> v_n OR h.last_hash <> v_prev)) THEN
        RETURN QUERY SELECT false, v_n, v_n + 1;
        RETURN;
      END IF;
      RETURN QUERY SELECT true, v_n, NULL::bigint;
    END $$;

    -- Append-only even for roles that could be granted more later.
    CREATE FUNCTION audit_log_immutable() RETURNS trigger LANGUAGE plpgsql
      AS $$ BEGIN RAISE EXCEPTION 'audit_log is append-only' USING ERRCODE = 'insufficient_privilege'; END $$;
    CREATE TRIGGER audit_log_no_update BEFORE UPDATE OR DELETE ON audit_log
      FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();

    ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
    ALTER TABLE audit_log FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON audit_log
      USING (org_id = app_current_org()) WITH CHECK (org_id = app_current_org());
    CREATE POLICY worker_read ON audit_log FOR SELECT TO worker USING (true);
    ALTER TABLE audit_chain_head ENABLE ROW LEVEL SECURITY;
    ALTER TABLE audit_chain_head FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON audit_chain_head
      USING (org_id = app_current_org()) WITH CHECK (org_id = app_current_org());
    CREATE POLICY worker_read ON audit_chain_head FOR SELECT TO worker USING (true);

    GRANT SELECT, INSERT ON audit_log TO app;
    GRANT SELECT, INSERT, UPDATE ON audit_chain_head TO app;
    GRANT SELECT ON audit_log, audit_chain_head TO worker;
    REVOKE ALL ON FUNCTION audit_append(uuid, text, text, text, text, jsonb, inet, text),
                           audit_verify(uuid), audit_canonical(bigint, timestamptz, uuid, uuid, text,
                           text, text, text, jsonb, inet, text), audit_log_immutable() FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION audit_append(uuid, text, text, text, text, jsonb, inet, text) TO app;
    GRANT EXECUTE ON FUNCTION audit_verify(uuid) TO app, worker;
    GRANT EXECUTE ON FUNCTION audit_canonical(bigint, timestamptz, uuid, uuid, text, text, text,
                                              text, jsonb, inet, text) TO app, worker;
    """)


def downgrade() -> None:
    op.execute("""
    DROP FUNCTION audit_verify(uuid);
    DROP FUNCTION audit_append(uuid, text, text, text, text, jsonb, inet, text);
    DROP FUNCTION audit_canonical(bigint, timestamptz, uuid, uuid, text, text, text, text, jsonb, inet, text);
    DROP TABLE audit_chain_head;
    DELETE FROM partman.part_config WHERE parent_table = 'public.audit_log';
    DROP TABLE audit_log CASCADE;
    DROP TABLE IF EXISTS partman.template_public_audit_log;
    DROP FUNCTION audit_log_immutable();
    """)
