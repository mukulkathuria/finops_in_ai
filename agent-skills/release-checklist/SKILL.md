---
name: release-checklist
description: Gates, rollback plan, versioning and changelog rules for a release.
---
# release-checklist

**Trigger:** cutting a release candidate or a production release.

**Steps**
1. All CI gates green on the release commit (unit, integration, contract, security, audits).
2. Full load, stress and soak profiles pass (load-test skill); restore drill done; ledger totals
   verified after restore.
3. PostgreSQL: re-run the full suite on the target server version; `pg_upgrade` rehearsal on a copy
   (dump and restore as fallback); see `docs/runbooks/postgres-upgrade.md`.
4. Audit: traceability matrix (every story to code and tests, every threat to a control and test),
   automated audit scripts, git history secret scan; publish `docs/audits/<date>.md`.
5. Configuration review: debug off, CORS locked, admin bootstrap disabled, secrets from env or KMS.
6. Version with SemVer; update `CHANGELOG.md`; tag; build, scan and sign images.
7. Rollback plan: previous image tags pinned; migrations are backward compatible for one release.

**Done when**
- [ ] No open Critical or High findings; signed audit report linked in the release notes
