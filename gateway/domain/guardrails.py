"""Guardrail policy (spec sections 3, 4 and 10; US-11, US-12): what happens to PII, secrets and
prompt injections, and what happens when a classifier cannot answer. One policy per org."""

from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from typing import Any, Final, Literal, get_args

from gateway.domain.errors import DomainError, InvalidInput

PiiAction = Literal["redact", "block", "off"]
SecretAction = Literal["block", "redact"]
InjectionAction = Literal["block", "flag", "off"]
FailurePolicy = Literal["closed", "open"]
OutputAction = Literal["redact", "off"]

_CHOICES: Final[dict[str, tuple[str, ...]]] = {
    "pii": get_args(PiiAction),
    "secrets": get_args(SecretAction),
    "injection": get_args(InjectionAction),
    "on_classifier_error": get_args(FailurePolicy),
    "output": get_args(OutputAction),
}


class GuardrailBlocked(DomainError):
    """A request stopped by policy. The message names the rule, never the matched text."""

    kind = "blocked"
    default_code = "guardrail_blocked"


@dataclass(frozen=True, slots=True)
class GuardrailPolicy:
    pii: PiiAction = "redact"
    secrets: SecretAction = "block"
    injection: InjectionAction = "block"
    injection_threshold: float = 0.8
    on_classifier_error: FailurePolicy = "closed"  # security checks fail closed by default
    output: OutputAction = "redact"
    hosted_classifier: bool = True  # may prompt text reach a hosted classifier (Jev) at all?

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def parse(cls, raw: Mapping[str, Any] | None, *, strict: bool = True) -> GuardrailPolicy:
        """Defaults for missing keys. `strict` (input from an admin) rejects unknown keys and bad
        values; a stored policy is read leniently, so an older shape never breaks requests."""
        if not raw:
            return cls()
        names = {f.name for f in fields(cls)}
        if strict and (unknown := sorted(set(raw) - names)):
            raise InvalidInput(f"unknown guardrail setting: {unknown[0]}", param=unknown[0])
        values: dict[str, Any] = {}
        for key, value in raw.items():
            if key not in names:
                continue
            ok = True
            if key in _CHOICES:
                ok = value in _CHOICES[key]
            elif key == "injection_threshold":
                ok = (
                    isinstance(value, int | float)
                    and not isinstance(value, bool)
                    and 0.5 <= value <= 0.99
                )
                value = float(value) if ok else value
            elif key == "hosted_classifier":
                ok = isinstance(value, bool)
            if not ok:
                if strict:
                    allowed = _CHOICES.get(key)
                    hint = f"one of {', '.join(allowed)}" if allowed else "a valid value"
                    raise InvalidInput(f"{key} must be {hint}", param=key)
                continue
            values[key] = value
        return cls(**values)
