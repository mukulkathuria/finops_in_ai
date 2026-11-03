# ADR 0008: Org provider keys with envelope encryption; native and LiteLLM adapters

- Status: accepted
- Date: 2026-11-03
- Stories: US-06 (spec sections 3, 5, 10)

## Decision
- Each org may store one active key per configured provider. The key is sealed with AES-256-GCM
  under a fresh 256-bit data key; the data key is wrapped by the master key (`MASTER_KEY`, behind
  the `KeyManager` port so a KMS can replace it). Associated data binds the ciphertext to org,
  provider and row id. The API returns only a 4-character hint; adding a new key revokes the old
  one (rotation); every change is audited without key material.
- Requests from an org use the org's own key for a provider when one exists, otherwise the
  operator key from the environment. Decrypted keys live only inside the adapters built for that
  org (cached for a short TTL).
- Adapter selection is configuration: `PROVIDER_ADAPTER_DEFAULT` and a per-provider `adapter:`
  value of `native` or `litellm`. Choosing `litellm` without the optional extra installed fails at
  startup with a clear error (fail closed).

## Finding: LiteLLM cannot be installed next to the OpenAI 3.x SDK (2026-11-03)
LiteLLM 1.106.6 requires `openai<3.0.0`; the official OpenAI 3.x SDK is what the US-05 suite uses
to prove compatibility. A resolver silently falls back to an old LiteLLM that never declared the
bound, which would be an untested pin. The LiteLLM adapter therefore ships as the switch point
plus a fail-closed check; the extra is published once a LiteLLM release supports openai 3.x, and
the shared contract suite must pass on it before it is enabled for any provider.
