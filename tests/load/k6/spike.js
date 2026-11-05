// Spike (spec section 11): from 30% to 150% of RATE requests a second within a minute, held, then
// RATE again. The spike may slow requests but not fail them; the phase after it must be back
// within the SLO, two minutes being the recovery the spec allows.
import { chat, measured, organisation } from "./lib.js";

const RATE = Number(__ENV.RATE || 60);
const LOW = Math.max(1, Math.round(RATE * 0.3));
const HIGH = Math.round(RATE * 1.5);

export const options = {
  setupTimeout: "10m",
  scenarios: {
    spike: {
      executor: "ramping-arrival-rate",
      startRate: LOW,
      timeUnit: "1s",
      preAllocatedVUs: 50,
      maxVUs: 500,
      stages: [
        { duration: "30s", target: LOW },
        { duration: "1m", target: HIGH },
        { duration: "2m", target: HIGH },
      ],
    },
    recovered: {
      executor: "constant-arrival-rate",
      rate: RATE,
      timeUnit: "1s",
      duration: "2m",
      startTime: "3m30s",
      preAllocatedVUs: 50,
      maxVUs: 500,
    },
  },
  thresholds: {
    "errors{scenario:spike}": ["rate<0.01"],
    "errors{scenario:recovered}": ["rate<0.001"],
    "gateway_overhead_ms{scenario:recovered}": ["p(95)<=100", "p(99)<=250"],
  },
};

export function setup() {
  return organisation(Number(__ENV.KEYS || 100));
}

export default function (org) {
  measured(chat(org.keys[Math.floor(Math.random() * org.keys.length)]));
}
