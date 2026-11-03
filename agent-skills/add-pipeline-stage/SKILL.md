---
name: add-pipeline-stage
description: Add an optimization or guardrail as a pipeline Stage with shadow mode and an eval gate.
---
# add-pipeline-stage

**Trigger:** a new optimization technique or guardrail.

**Inputs:** the technique, its expected token or cost effect, an eval set that measures quality.

**Steps**
1. Implement the `Stage` protocol (`name`, `version`, `phase`, `process(ctx) -> StageReport`) in
   `gateway/pipeline/stages/<name>.py`. Fill every `StageReport` field honestly.
2. Register it by name; enable it in `config/gateway.yaml` with `mode: shadow` first.
3. In shadow mode the stage runs on a copy and its output is discarded; reports are still written.
4. Run the eval gate (`eval_runs`): quality within tolerance at equal or lower cost.
5. Only then flip to `mode: enabled` (the control plane refuses without a passing eval run).
6. A stage that throws, hangs or returns an invalid report is isolated by the runner; add a test.

**Done when**
- [ ] Appears in savings reports with no dashboard change
- [ ] Failure isolation test green
- [ ] Eval run recorded
