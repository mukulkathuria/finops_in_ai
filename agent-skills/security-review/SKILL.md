---
name: security-review
description: Apply the security checklist and scanners to a change touching auth, crypto, tenancy, files, caching or keys.
---
# security-review

**Trigger:** any change to auth, crypto, tenancy, input parsing, file handling, caching or
provider keys; before every release.

**Steps**
1. Walk `docs/threat-model.md`; for each threat touched, confirm the control and its test exist.
2. Run `uv run bandit -q -r gateway control_plane worker`, `uv run pip-audit`, `gitleaks detect`,
   Semgrep with `.semgrep/rules.yml`, and Trivy on built images.
3. Check: deny-by-default authorization on every route and object; no secrets in code, logs,
   errors or fixtures; parameterised SQL only; timeouts on every outbound call; SSRF guard on any
   server-side fetch; no `verify=False`; no `eval`; no shell with user data.
4. Triage findings by the severity scale (spec section 12). Critical and High block merge.
5. Each fix adds a test that fails before and passes after.

**Done when**
- [ ] Scanners clean or findings triaged with an accepted-risk ADR
- [ ] No open Critical or High findings
