---
name: add-document-parser
description: Add an OCR or document parsing backend behind the DocumentParser port.
---
# add-document-parser

**Trigger:** a new OCR model, a new file format, or a parser version upgrade.

**Inputs:** the backend sidecar or library, its license, an evaluation set (50 to 100 files per
language and layout), target confidence threshold.

**Steps**
1. OCR models run as sidecars implementing `POST /v1/parse` (see `docs/adr` for the contract).
   Add `<NAME>_BASE_URL` and `<NAME>_MODEL` to `.env.example` and to the allowed `OCR_BACKEND` values.
2. Non-OCR formats implement `DocumentParser` in `gateway/adapters/parsers/<name>.py`.
3. Return per-page confidence; below `OCR_MIN_CONFIDENCE` the original page must be kept and flagged.
4. Cache key is `(content_hash, parser, parser_version, options)`; never mix backends.
5. Run `tests/contract/test_parser_contract.py` and the evaluation script; record text error,
   table fidelity and cost per page in an ADR.

**Done when**
- [ ] Parser contract suite green; blank, rotated, mixed-script and low-resolution pages handled
- [ ] Backend name and version stored per derived artifact
- [ ] Evaluation report committed
