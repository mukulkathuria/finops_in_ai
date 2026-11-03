# ADR 0003: Money as integer micro-USD, prices per million tokens

- Status: accepted
- Date: 2026-11-03
- Stories: US-05, US-07, US-09, US-10, US-17 (spec sections 5, 6)

## Context
Spend accounting must be exact, associative and safe under concurrency. The spec mandates bigint
micro-USD and bigint tokens, and forbids floats.

## Decision
- All money is `bigint` micro-USD (1 USD = 1,000,000). Python uses `int` only; no `float` or
  `Decimal` enters budget or ledger code.
- Catalog prices are stored as micro-USD per one million tokens (USD 2.50 per 1M tokens is
  `2_500_000`). Cost of `n` tokens = `ceil(n * price / 1_000_000)`.
- Rounding is always up (ceil) per component, so the gateway never under-charges. The error is at
  most one micro-USD per component per request.
- Reservation estimates are strict upper bounds: input tokens are bounded by the UTF-8 byte length
  of everything sent plus a per-message and per-tool overhead; output by the enforced `max_tokens`
  (the gateway sets one when the client omits it); prompt-cache writes priced at the provider
  write multiplier. With these bounds actual cost cannot exceed the reservation, which is what makes
  "zero overshoot" provable.
- Each usage event stores the `catalog_version` it was priced with. Prices are never rewritten.

## Alternatives considered
- `numeric` money: exact but slower, and mixing it with Python `Decimal` invites float leaks.
- Price per token in nano-USD: avoids the per-million division but makes catalog data unreadable.

## Consequences
- Byte-length bounds over-reserve English text by roughly 4x on the input side. Input is usually
  the cheaper side and the reservation is released at commit, so the cost is only head-room.
- Savings use the same integer math, so ledger totals and reports reconcile exactly.
