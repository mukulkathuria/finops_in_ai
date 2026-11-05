# ADR 0009: The agent workspace's trust boundary, and Phase 5's accepted risks

- Status: accepted
- Date: 2026-11-05
- Stories: US-24, US-25 (spec section 4 F6 and F7, section 10, section 12)

## Context

The Phase 5 review (`docs/audits/2026-11-05-phase5-review.md`) found that the agent workspace
trusted names an agent chose for itself. Any member of a workspace's ACL could:

- read another agent's inbox;
- answer its questions;
- spend its discovery budget.

Each of these was a free string compared to a free string. The same review asked where
moderation, discovery accounting and image storage draw their lines. Spec section 12 lets a
Medium finding stand only with an accepted-risk ADR. This is that record for the Phase 5
findings that are fixed only in part, or not fixed.

## Decision

The workspace's trust boundary is its ACL. Within it, identity comes from the credential, and
the agent's name is only a label the credential holds.

1. **Agent names are bound to members per task.** The first member to use a name in a task
   holds it. A member is a user, or for a team key the team (migration 0027,
   `task_agents.principal`). Another member's call under that name is refused with
   `agent_claimed`. One orchestrator credential may still run any number of agents. Every fact,
   summary, question and answer records the member behind it (`written_by`, `answered_by`).
2. **Discovery accounting is cooperative.** A read counts against a task only when it names the
   task and the agent. A read that names neither is served and not counted. The per-agent
   budget stops runaway loops inside a task; it is not a money control. Money is held by the
   gateway's budgets on every model call the agents make. Hop chains are what clients say they
   are: leaving out `reply_to` starts a new chain. A reworded question is a new question to the
   loop rule.
3. **Workspace content is screened when it is read, as documents are.** Facts, summaries and
   messages pass the organisation's passage screen on the way out. A blocked one is withheld
   with `content_withheld`, or counted in a search's `withheld`. Writes are not screened. A fact
   holding a secret is stored, as a document holding one is, and never handed to an agent while
   the policy blocks secrets.
4. **Image answers in idempotency records stay plaintext, as chat answers do.** The janitor
   purges them after a day. Cached image answers are sealed per org, and an answer over 8 MiB is
   not cached.
5. **The vector search's gain depends on tenants being spread across partitions.** Chunks are
   hash-partitioned by org into 16 partitions. One very large tenant has a partition, and an
   HNSW graph, mostly to itself. Its search no longer skips other orgs' rows, but walks a graph
   the size of its own corpus. The seeded benchmark measures 50 orgs of 20,000 chunks each.

## Alternatives considered

- **One credential per agent, with names derived from the credential.** This would rule out
  the common shape, one orchestrator running many agents, and every MCP client would have to
  mint keys. Binding a name to the member who first uses it in a task gives the same protection
  between members and keeps one credential per run.
- **Require `task_id` on every broker read.** This would break the plain uses of
  `search_documents` and `get_code_context` that predate the workspace. The cooperative count
  measures what US-25 asks for (discovery tokens per task) without changing those tools.
- **Screen writes as well as reads.** Documents are not screened when indexed. Doing it only
  for facts would make the two stores disagree, and screening on read already keeps blocked
  content from reaching an agent.
- **Seal idempotency records.** That is a change to every request type, chat included, and not
  Phase 5's to make. The one-day purge bounds how long an answer is kept.
- **Hand a large tenant more than one partition.** Hash partitioning cannot do that. Range or
  list partitioning by tenant, or sub-partitions, would need tenant placement. That waits for
  evidence from a skewed-tenant benchmark.

## Consequences

- Members cannot act for each other's agents. A member who loses a name to another must use
  another name in that task.
- A team key's agents are its team's: every key of the team acts as the same member. This
  matches how a team key's documents and workspaces belong to its team.
- Discovery reports cover the reads that name their task. A client that wants its agents
  counted names the task.
- Workspace writes are bounded:
  - a workspace holds at most 2,000 facts and 10,000 summaries;
  - a task holds at most 100 agents, 5,000 questions and 5,000 remembered searches.

  The fact cap is exact. The other caps can be passed by concurrent first uses, by at most the
  number of concurrent writers.
- Must test or watch:
  - `tests/integration/test_phase5_review.py`: claims, bounds, screening and deletion.
  - `tests/integration/test_migrations.py`: data-moving migrations as `migrator`.
  - The `bench` workflow's vector search p95, which was 41.4 ms at 1M chunks.

## Assumptions

- Members sharing a workspace trust each other with its content. The ACL is the boundary.
- An agent framework names its agents consistently within a run.
- A tenant with far more chunks than the others still searches within target, because it no
  longer skips other orgs' rows. This is not measured. A skewed-tenant benchmark case is the
  check.
