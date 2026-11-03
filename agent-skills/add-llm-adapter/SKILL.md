---
name: add-llm-adapter
description: Implement a new LLM provider adapter behind the LLMAdapter port and prove it with the contract suite.
---
# add-llm-adapter

**Trigger:** a new provider or API version must be supported.

**Inputs:** official API reference for the installed version, sample responses for chat, stream,
embeddings and errors, price list with an effective date.

**Steps**
1. Create `gateway/adapters/llm/<name>.py` implementing `LLMAdapter` (`complete`, `stream`, `embed`,
   `capabilities`, `normalize_usage`, `classify_error`). Vendor types never leave the file.
2. Map errors to `GatewayErrorKind` (rate_limit, timeout, content_filter, ctx_overflow, auth, other).
3. Normalise usage to `Usage` including cached and reasoning tokens; mark `estimated` when missing.
4. Register it in `gateway/adapters/llm/registry.py` with one line.
5. Add recorded fixtures under `tests/fixtures/providers/<name>/` and run the shared suite in
   `tests/contract/test_llm_contract.py` (it parametrises over every registered adapter).
6. Add catalog rows with the `update-price-catalog` skill.

**Done when**
- [ ] Contract suite green, including streaming, tool calls, cached tokens and every error kind
- [ ] No vendor import outside the adapter (`lint-imports` green)
- [ ] Retries with jitter honour idempotency; timeouts set on every call
