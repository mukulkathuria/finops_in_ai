// Budget race (spec section 11): 500 requests at once against a team budget that holds about 20
// of them. Every answer is a 200 or a 429 budget refusal, and the budget is never overshot.
import { check, fail, sleep } from "k6";
import { Counter } from "k6/metrics";
import { budget, chat, organisation, setBudget } from "./lib.js";

// Twenty worst-case holds of the 10-token "hi" (tests/integration/test_us10_concurrency.py).
const LIMIT = 20 * 28;
const unexpected = new Counter("unexpected_answers");

export const options = {
  setupTimeout: "5m",
  scenarios: {
    race: { executor: "per-vu-iterations", vus: 500, iterations: 1, maxDuration: "2m" },
  },
  thresholds: {
    unexpected_answers: ["count==0"],
    checks: ["rate==1"],
  },
};

export function setup() {
  const org = organisation(1);
  const made = setBudget(org.token, { scope_type: "team", team_id: org.team, limit_amount: LIMIT });
  return { ...org, budget: made.id };
}

export default function (org) {
  const res = chat(org.keys[0]);
  const refused = res.status === 429 && res.json("error.code") === "budget_exceeded";
  if (res.status !== 200 && !refused) unexpected.add(1);
  check(res, { "admitted, or refused by the budget": () => res.status === 200 || refused });
}

export function teardown(org) {
  sleep(5); // the last settlements land
  const now = budget(org.token, org.budget);
  if (!now || now.committed > LIMIT || now.committed + now.reserved > LIMIT) {
    fail(`the budget was overshot: ${JSON.stringify(now)}`);
  }
  console.log(`budget ${LIMIT}: committed ${now.committed}, reserved ${now.reserved}`);
}
