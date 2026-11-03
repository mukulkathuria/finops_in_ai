---
name: add-classifier-backend
description: Add a guardrail or routing classifier behind the Classifier port with a defined failure policy.
---
# add-classifier-backend

**Trigger:** a new classifier model or hosted service.

**Inputs:** API or model reference, data-handling terms, adversarial and benign corpora.

**Steps**
1. Implement `Classifier.decide(state, questions)` in `gateway/adapters/classifiers/<name>.py`.
2. Hosted backends: fail closed with a configuration error when the key is missing. Never fall
   back silently to sending data elsewhere.
3. Data boundary checklist: redact before sending, honour the per-org disable flag, log only the
   backend name, version, threshold and probability (never the text).
4. Declare timeouts; the calling route decides fail-open or fail-closed.
5. Run the classifier contract suite plus the injection and benign corpora; record the false
   positive rate.

**Done when**
- [ ] Contract suite and corpora green; false-positive rate under the agreed limit
- [ ] Rules still enforce when this backend is down
- [ ] No unredacted text leaves the process when the backend is disabled (test proves it)
