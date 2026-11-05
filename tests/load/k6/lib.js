// Shared by the load profiles (spec section 11): an org with a team and its API keys, made through
// the gateway's own API, and the chat request every profile sends to the mock provider.
import http from "k6/http";
import { fail } from "k6";
import { Rate, Trend } from "k6/metrics";

export const BASE = __ENV.BASE_URL || "http://127.0.0.1:8080";
// The mock provider answers in a fixed delay (MOCK_LATENCY_MS_MIN = MAX): the gateway's overhead
// is a request's time beyond it.
export const PROVIDER_MS = Number(__ENV.PROVIDER_MS || 50);
export const overhead = new Trend("gateway_overhead_ms", true);
export const errors = new Rate("errors");

const JSON_HEADERS = { "Content-Type": "application/json" };
// Must not contain a word of the org name or the email name (gateway/domain/identity.py).
const PASSWORD = "Steady-Harbor-Lantern-58!";

function authorised(token) {
  return { headers: { ...JSON_HEADERS, Authorization: `Bearer ${token}` } };
}

function expect(res, what, statuses) {
  if (!statuses.includes(res.status)) fail(`${what}: ${res.status} ${res.body}`);
  return res;
}

/** A new org (the stack runs with open signup), one team and `keys` API keys of it. */
export function organisation(keys) {
  const email = `load-${Date.now()}-${Math.floor(Math.random() * 1e9)}@example.com`;
  const signup = JSON.stringify({ org_name: "Load profile", email, password: PASSWORD });
  const token = expect(
    http.post(`${BASE}/v1/auth/signup`, signup, { headers: JSON_HEADERS }),
    "signup",
    [201],
  ).json("access_token");
  const team = expect(
    http.post(`${BASE}/v1/admin/teams`, JSON.stringify({ name: "load" }), authorised(token)),
    "team",
    [201],
  ).json("id");
  const made = [];
  for (let i = 0; i < keys; i++) {
    const body = JSON.stringify({ name: `load-${i}` });
    const created = http.post(`${BASE}/v1/admin/teams/${team}/api-keys`, body, authorised(token));
    made.push(expect(created, "api key", [201]).json("key"));
  }
  return { token, team, keys: made };
}

/** One short chat request to the mock provider. */
export function chat(key) {
  const body = JSON.stringify({
    model: "mock/mock-small",
    messages: [{ role: "user", content: "hi" }],
    max_tokens: 10,
  });
  return http.post(`${BASE}/v1/chat/completions`, body, {
    ...authorised(key),
    tags: { name: "chat" },
  });
}

/** Count the answer in `errors` and, when it succeeded, the gateway's overhead. */
export function measured(res) {
  errors.add(res.status !== 200);
  if (res.status === 200) overhead.add(res.timings.duration - PROVIDER_MS);
  return res;
}

export function setBudget(token, body) {
  const json = JSON.stringify({ period: "day", unit: "tokens", ...body });
  return expect(http.put(`${BASE}/v1/admin/budgets`, json, authorised(token)), "budget", [200, 201]).json();
}

export function budget(token, id) {
  const list = expect(http.get(`${BASE}/v1/admin/budgets`, authorised(token)), "budgets", [200]);
  return list.json("data").find((b) => b.id === id);
}
