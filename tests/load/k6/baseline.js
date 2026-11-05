// Baseline (spec section 11): ramp to VUS users over RAMP and hold for HOLD. Each user has an API
// key of its own and asks a short question, THINK seconds apart. A soak is the same profile held
// for hours at half the load: VUS=500 HOLD=8h.
import { sleep } from "k6";
import { chat, measured, organisation } from "./lib.js";

const VUS = Number(__ENV.VUS || 100);

export const options = {
  setupTimeout: "10m",
  scenarios: {
    baseline: {
      executor: "ramping-vus",
      stages: [
        { duration: __ENV.RAMP || "1m", target: VUS },
        { duration: __ENV.HOLD || "3m", target: VUS },
      ],
      gracefulRampDown: "10s",
    },
  },
  thresholds: {
    gateway_overhead_ms: ["p(95)<=100", "p(99)<=250"],
    errors: ["rate<0.001"],
  },
};

export function setup() {
  return organisation(VUS);
}

export default function (org) {
  measured(chat(org.keys[(__VU - 1) % org.keys.length]));
  sleep(Number(__ENV.THINK || 1));
}
