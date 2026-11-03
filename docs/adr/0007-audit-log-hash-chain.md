# ADR 0007: Append-only audit log with a per-org hash chain

- Status: accepted
- Date: 2026-11-03
- Stories: US-28 (spec sections 5, 10, 12)

## Context
Logins, key changes, limit changes, policy changes and denials must be recorded append-only with
a verifiable hash chain. The log never stores prompt text. High-volume tables are partitioned by
month with pg_partman.

## Decision
- `audit_log` is range-partitioned by month (`pg_partman`, 4 months premade, infinite time
  partitions so maintenance runs even without traffic). Partitions are never granted directly.
- Each org has its own chain: `hash = sha256(prev_hash || canonical(row))` where `canonical` is
  the row as a JSON array (injective, so field values cannot forge delimiters). `audit_chain_head`
  holds the last sequence number and hash; `audit_append()` locks that row, so appends are
  serialised per org and commit or roll back together with the change they record.
- `audit_verify(org)` recomputes the chain and detects edited fields, deleted, inserted or
  reordered rows, and a truncated tail (head mismatch).
- Append-only is enforced three ways: no UPDATE or DELETE grants, a trigger that rejects UPDATE
  and DELETE even for privileged roles, and the hash chain for anything that bypasses both.
- `AuditEvent` refuses detail keys that look like secrets or prompts.
- Authorization denials are audited by the route guard in their own short transaction.

## Consequences
- A database superuser could rewrite the whole chain consistently. Mitigation for later:
  periodically export the chain head to an external, write-once store and compare.
- Audit throughput per org is bounded by the head-row lock; audit events are low volume.
- Partition retention (dropping old months) is the only way rows leave the table.
