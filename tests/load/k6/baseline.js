// Baseline (spec section 11): ramp to VUS users over RAMP and hold for HOLD. Each user has an API
// key of its own and asks a short question, THINK seconds apart. A soak is the same profile held
// for hours at half the load: VUS=500 HOLD=8h.
import { sleep } from "k6";
import exec from "k6/execution";
import { Rate, Trend } from "k6/metrics";
import { PROVIDER_MS, chat, measured, organisation } from "./lib.js";

const VUS = Number(__ENV.VUS || 100);
const RAMP = __ENV.RAMP || "1m";
const RAMP_MS = (([, n, unit]) => Number(n) * { s: 1e3, m: 6e4, h: 3.6e6 }[unit])(
  /^(\d+)([smh])$/.exec(RAMP) || [null, 1, "m"],
);
// The criterion is "at the target load": the ramp's cold start is reported, not judged.
const atTarget = new Trend("gateway_overhead_at_target_ms", true);
const errorsAtTarget = new Rate("errors_at_target");

export const options = {
  setupTimeout: "10m",
  scenarios: {
    baseline: {
      executor: "ramping-vus",
      stages: [
        { duration: RAMP, target: VUS },
        { duration: __ENV.HOLD || "3m", target: VUS },
      ],
      gracefulRampDown: "10s",
    },
  },
  thresholds: {
    gateway_overhead_at_target_ms: ["p(95)<=100", "p(99)<=250"],
    errors_at_target: ["rate<0.001"],
    errors: ["rate<0.001"], // the ramp may be slower, never failing
    "http_reqs{name:chat}": ["count>0"], // a run that sent nothing passes nothing
  },
};

export function setup() {
  return organisation(VUS);
}

export default function (org) {
  const res = measured(chat(org.keys[(__VU - 1) % org.keys.length]));
  if (Date.now() - exec.scenario.startTime >= RAMP_MS) {
    errorsAtTarget.add(res.status !== 200);
    if (res.status === 200) atTarget.add(res.timings.duration - PROVIDER_MS);
  }
  sleep(Number(__ENV.THINK || 1));
}
