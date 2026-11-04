/** Shapes returned by the gateway API (see control_plane/api and gateway/api). */

export type Unit = "usd" | "tokens" | "images";
export type Period = "day" | "week" | "month";
export type Scope = "org" | "team" | "member";

export interface Session {
  org: { id: string; name: string; slug: string };
  user: { id: string; email: string; roles: string[] };
  access_token: string;
  token_type: "Bearer";
  expires_in: number;
  refresh_token: string;
  refresh_expires_in: number;
}

export interface ListOf<T> {
  data: T[];
}

export interface PeriodUsage {
  period: Period;
  period_start: string;
  period_end: string;
  requests: number;
  cost_micro_usd: number;
  cost_usd: string;
  input_tokens: number;
  output_tokens: number;
  images: number;
}

export interface MyBudget {
  id: string;
  team_id: string | null;
  period: Period;
  unit: Unit;
  limit_amount: number;
  granted: number;
  soft_pct: number;
  period_start: string;
  committed: number;
  reserved: number;
  remaining: number;
}

export interface MyUsage {
  user_id: string;
  timezone: string;
  usage: PeriodUsage[];
  budgets: MyBudget[];
}

export interface Budget {
  id: string;
  scope_type: Scope;
  team_id: string | null;
  user_id: string | null;
  period: Period;
  unit: Unit;
  limit_amount: number;
  soft_pct: number;
  period_start: string;
  committed: number;
  reserved: number;
  granted: number;
  remaining: number;
}

export interface BudgetAlert {
  id: string;
  budget_id: string;
  scope_type: Scope;
  team_id: string | null;
  user_id: string | null;
  period: Period;
  unit: Unit;
  period_start: string;
  kind: string;
  committed: number;
  limit_amount: number;
  created_at: string;
}

export interface BudgetSettings {
  timezone: string;
  allocation: "overcommit" | "strict";
}

export interface BudgetRequest {
  id: string;
  budget_id: string;
  team_id: string;
  user_id: string;
  amount: number;
  unit: Unit;
  reason: string;
  status: "pending" | "approved" | "denied";
  grant_type: "one_time" | "permanent" | null;
  decided_by: string | null;
  decided_at: string | null;
  decision_note: string | null;
  created_at: string;
}

export interface Team {
  id: string;
  name: string;
  created_at: string;
  member_count: number;
}

export interface Member {
  user_id: string;
  email: string;
  display_name: string;
  is_lead: boolean;
}

export interface ApiKey {
  id: string;
  team_id: string;
  user_id: string | null;
  name: string;
  prefix: string;
  scopes: string[];
  created_by: string;
  created_at: string;
  expires_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
}

export interface ApiKeyCreated extends ApiKey {
  key: string;
}

export interface InvitationCreated {
  id: string;
  email: string;
  invite_url: string;
  expires_at: string;
}

export interface AuditEntry {
  seq: number;
  ts: string;
  actor_id: string | null;
  actor_type: string;
  action: string;
  target_type: string | null;
  target_id: string | null;
  details: Record<string, unknown>;
  ip: string | null;
  request_id: string | null;
  hash: string;
}

export interface AuditPage {
  data: AuditEntry[];
  next_before_seq: number | null;
}

export interface Verification {
  ok: boolean;
  checked: number;
  first_bad_seq: number | null;
}

export interface SavingsRow {
  key: string;
  label: string | null;
  requests: number;
  input_tokens: number;
  output_tokens: number;
  cost_micro_usd: number;
  baseline_micro_usd: number;
  overhead_micro_usd: number;
  savings_micro_usd: number;
}

export interface SavingsTechnique {
  technique: string;
  applied: number;
  shadow_runs: number;
  tokens_saved: number;
  saved_micro_usd: number;
  shadow_saved_micro_usd: number;
}

export interface SavingsReport {
  object: "savings_report";
  start: string;
  end: string;
  team_id: string | null;
  totals: SavingsRow;
  rows: SavingsRow[];
  techniques: SavingsTechnique[];
}
