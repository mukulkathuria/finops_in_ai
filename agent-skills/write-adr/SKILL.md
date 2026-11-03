---
name: write-adr
description: Record an architecture decision before coding a module or when a choice is costly to reverse.
---
# write-adr

**Trigger:** before coding a new module; when picking between options; when an assumption is made;
when the spec is ambiguous or contradicted by evidence (spec section 1, rules 1 and 9).

**Inputs:** the user story, the relevant spec section, any evidence (benchmarks, docs, probes).

**Steps**
1. Copy `docs/adr/0000-template.md` to the next free number with a kebab-case title.
2. Context: state the forces and link the evidence. Quote probe output when facts were checked.
3. Decision: write it so a reviewer can check the code against it (names, files, values).
4. Alternatives: each option and the reason it lost.
5. Consequences and assumptions: list what must now be tested or watched.
6. Link the ADR from the code comment or test that depends on it, and from `PROGRESS.md`.

**Done when**
- [ ] Status and date set, stories listed
- [ ] Every assumption is phrased as something checkable
- [ ] The decision is reversible, or the user approved it (data model, tenancy, auth)
