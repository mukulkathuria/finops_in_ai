---
name: update-price-catalog
description: Add models and prices as data with an effective date, never by repricing old events.
---
# update-price-catalog

**Trigger:** a provider changes prices, or a new model is enabled.

**Steps**
1. Prices are data in `model_catalog`, in micro-USD per one million tokens (ADR 0003).
2. Never edit a row in place. Insert a new row with a later `effective_from`; the old row keeps
   pricing historical events through their stored `catalog_version`.
3. Seed or sync: edit `config/model_catalog.yaml` (curated) or run the price sync job, which reads
   the pinned community price list and upserts only changed prices as new versions.
4. Check capabilities (context window, max output, vision, tools, caching) against the provider docs.
5. Run the pricing unit tests and the catalog integration test.

**Done when**
- [ ] New rows have an effective date and source; no historical event changed cost
