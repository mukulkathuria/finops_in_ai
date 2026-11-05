// Failure under load (spec section 11): requests flow at RATE a second while the workflow
// restarts PostgreSQL about a minute in (tests/load/k6/README.md). Requests may fail while it is
// down; from two minutes after the restart they must succeed again, within the SLO.
import { chat, measured, organisation } from "./lib.js";

const RATE = Number(__ENV.RATE || 40);

export const options = {
  setupTimeout: "10m",
  scenarios: {
    load: {
      executor: "constant-arrival-rate",
      rate: RATE,
      timeUnit: "1s",
      duration: "3m",
      preAllocatedVUs: 50,
      maxVUs: 400,
    },
    recovered: {
      executor: "constant-arrival-rate",
      rate: RATE,
      timeUnit: "1s",
      duration: "1m30s",
      startTime: "3m",
      preAllocatedVUs: 50,
      maxVUs: 400,
    },
  },
  thresholds: {
    "errors{scenario:recovered}": ["rate<0.001"],
    "gateway_overhead_ms{scenario:recovered}": ["p(95)<=100", "p(99)<=250"],
    "http_reqs{scenario:recovered}": ["count>0"], // a run that sent nothing passes nothing
  },
};

export function setup() {
  return organisation(Number(__ENV.KEYS || 100));
}

export default function (org) {
  measured(chat(org.keys[Math.floor(Math.random() * org.keys.length)]));
}
