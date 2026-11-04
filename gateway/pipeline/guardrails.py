"""Input and output guardrails (spec section 4, stages 5 and 10; US-11, US-12).

Deterministic rules always run and always win; the classifier only advises, and its failure
follows the org's policy (security checks fail closed by default). Every decision is logged and
recorded with its rule, backend and version, never with the matched text.

Text reaches the provider unchanged unless something in it is redacted; it is then sent in the
form that revealed the most values (invisible characters removed, or NFKC-normalised).
"""

import asyncio
import logging
import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Final
from uuid import UUID

from gateway.adapters.classifiers.rules import RulesClassifier
from gateway.adapters.observability.telemetry import Telemetry
from gateway.adapters.postgres.policies import PolicyCache
from gateway.domain.classifiers import INJECTION_QUESTION, ClassifierError
from gateway.domain.classifiers import Decision as Verdict
from gateway.domain.detectors import (
    RULES_VERSION,
    Category,
    Finding,
    Placeholders,
    detect,
    injection_rule,
    normalize,
    redact,
    reveal_hidden,
    strip_invisible,
)
from gateway.domain.errors import DomainError, Unavailable
from gateway.domain.guardrails import GuardrailBlocked, GuardrailPolicy
from gateway.domain.llm import CanonicalRequest, CanonicalResponse, Message, TextPart
from gateway.domain.retrieval import Hit
from gateway.ports.classifiers import Classifier

log = logging.getLogger(__name__)
Decision = dict[str, object]
_UNTRUSTED_ROLES: Final = frozenset({"user", "tool"})  # system prompts come from the developer
_INLINE_CHARS: Final = 64 * 1024  # larger texts are scanned off the event loop
_WINDOW: Final = 100_000  # characters per classifier call (SystemOne takes about 128,000)
_OVERLAP: Final = 1_000
_MAX_WINDOWS: Final = 8


async def _offload[T](size: int, work: Callable[[], T]) -> T:
    """Scanning is CPU-bound: inline for ordinary requests, in a worker thread for large ones so
    the event loop keeps serving everyone else meanwhile."""
    return work() if size <= _INLINE_CHARS else await asyncio.to_thread(work)


def _scan(text: str) -> tuple[str, list[Finding]]:
    """Findings in the text as written. A cleaned form (invisible characters removed, or NFKC)
    wins only when it reveals more, so emoji sequences and scripts that need zero-width joiners
    reach the provider intact."""
    best, found = text, detect(text)
    for variant in (strip_invisible(text), normalize(text)):
        if variant != best and len(more := detect(variant)) > len(found):
            best, found = variant, more
    return best, found


def _windows(state: str) -> list[str]:
    """Overlapping classifier inputs that cover the text; for very long text, the first and the
    last few windows (the rules still read all of it)."""
    if len(state) <= _WINDOW:
        return [state]
    starts = list(range(0, len(state) - _OVERLAP, _WINDOW - _OVERLAP))
    if len(starts) > _MAX_WINDOWS:
        half = _MAX_WINDOWS // 2
        starts = starts[:half] + starts[-half:]
    return [state[s : s + _WINDOW] for s in starts]


@dataclass(frozen=True, slots=True)
class Inspection:
    request: CanonicalRequest  # what continues to the provider
    policy: GuardrailPolicy
    decisions: tuple[Decision, ...]
    blocked: DomainError | None = None


class _Findings:
    """Collects findings across a request and redacts with request-wide placeholders."""

    def __init__(self, policy: GuardrailPolicy) -> None:
        self._policy = policy
        self.counts: Counter[tuple[Category, str]] = Counter()
        self._placeholders = Placeholders()

    def text(self, value: str) -> str:
        working, found = _scan(value)
        for f in found:
            self.counts[(f.category, f.rule)] += 1
        kept = [
            f
            for f in found
            if (f.category == "secret" and self._policy.secrets == "redact")
            or (f.category == "pii" and self._policy.pii == "redact")
        ]
        return redact(working, kept, self._placeholders) if kept else working

    def blocking(self) -> list[str]:
        """Rules whose findings the policy blocks on."""
        return sorted(
            {
                rule
                for (category, rule) in self.counts
                if (category == "secret" and self._policy.secrets == "block")
                or (category == "pii" and self._policy.pii == "block")
            }
        )

    def decisions(self, stage: str) -> list[Decision]:
        out: list[Decision] = []
        for (category, rule), count in sorted(self.counts.items()):
            if category == "secret":
                action = "blocked" if self._policy.secrets == "block" else "redacted"
            else:
                action = {"block": "blocked", "redact": "redacted", "off": "allowed"}[
                    self._policy.pii
                ]
            out.append(
                {
                    "stage": stage,
                    "check": category,
                    "rule": rule,
                    "count": count,
                    "action": action,
                    "backend": "rules",
                    "version": RULES_VERSION,
                }
            )
        return out


@dataclass(frozen=True, slots=True)
class _ChatScan:
    request: CanonicalRequest
    findings: _Findings
    untrusted: str  # user and tool text, for the injection check
    rule: Verdict | None  # the rules' injection verdict; None when the check does not apply


def _screen(policy: GuardrailPolicy, hits: Sequence[Hit]) -> tuple[list[Hit], list[dict[str, str]]]:
    kept: list[Hit] = []
    dropped: list[dict[str, str]] = []
    for hit in hits:
        findings = _Findings(policy)
        findings.text(hit.content)
        rules = findings.blocking()
        rule = rules[0] if rules else None
        if rule is None and policy.injection == "block":
            rule = injection_rule(hit.content)
        if rule is None:
            kept.append(hit)
        else:
            dropped.append({"chunk_id": str(hit.chunk_id), "rule": rule})
    return kept, dropped


def _chat_chars(req: CanonicalRequest) -> int:
    return sum(
        sum(len(p.text) for p in m.content if isinstance(p, TextPart))
        + sum(len(c.arguments) for c in m.tool_calls)
        for m in req.messages
    )


# --------------------------------------------------------------------------------------- output

# A stream is released only where no value can be cut in two: after a line or sentence end; in a
# long run without one, at a separator at least _TAIL characters before the end; and never inside
# a value or before one that may still be growing (it touches the end, or a private key block has
# no END line yet).
_BOUNDARY: Final = re.compile(r"\n|[.!?;:](?=\s)")
_SEPARATOR: Final = re.compile(r'[\s",{}\[\]]')
_SOFT_HOLD: Final = 512
_HARD_HOLD: Final = 16_384
_TAIL: Final = 64  # longer than any value with spaces in it (IBAN, card number, phone)


class OutputFilter:
    """Redacts secrets (and PII, unless the org allows it) in model output: whole texts with
    `text`, streamed text through `stream()`. One filter per response, so placeholder numbers
    agree across the content and every tool call."""

    def __init__(self, categories: frozenset[Category]) -> None:
        self.categories = categories
        self._placeholders = Placeholders()
        self.counts: Counter[tuple[Category, str]] = Counter()

    def apply(self, text: str, found: Sequence[Finding]) -> str:
        for f in found:
            self.counts[(f.category, f.rule)] += 1
        return redact(text, list(found), self._placeholders) if found else text

    def text(self, value: str) -> str:
        return self.apply(value, detect(value, categories=self.categories))

    def stream(self) -> OutputStream:
        return OutputStream(self)

    def decisions(self) -> list[Decision]:
        return [
            {
                "stage": "output",
                "check": category,
                "rule": rule,
                "count": count,
                "action": "redacted",
                "backend": "rules",
                "version": RULES_VERSION,
            }
            for (category, rule), count in sorted(self.counts.items())
        ]


class OutputStream:
    """One streamed text (the content, or one tool call's arguments), filtered incrementally."""

    def __init__(self, owner: OutputFilter) -> None:
        self._owner = owner
        self._pending = ""
        self._boundary = 0  # end of the last line or sentence end in _pending
        self._separator = 0  # end of the last separator found before _scanned
        self._scanned = 0  # separators were searched up to here
        self._tried = 0  # the last cut attempted; retried only once text past it can go

    def feed(self, piece: str) -> str:
        if not piece:
            return ""
        start = max(len(self._pending) - 1, 0)  # a sentence end needs the space after it
        self._pending += piece
        for m in _BOUNDARY.finditer(self._pending, start):
            self._boundary = m.end()
        size, cut = len(self._pending), self._boundary
        if size - cut > _SOFT_HOLD:  # a long run without a line or sentence end
            limit = size - _TAIL
            for m in _SEPARATOR.finditer(self._pending, max(self._scanned, cut), limit):
                self._separator = m.end()
            self._scanned = limit
            cut = max(cut, self._separator)
            if size - cut > _HARD_HOLD:  # one enormous token: cut it anyway
                cut = limit
        forced = size > _HARD_HOLD
        if cut <= self._tried and not forced:
            return ""
        return self._release(cut, forced=forced)

    def _release(self, cut: int, *, forced: bool) -> str:
        self._tried = cut
        size = len(self._pending)
        kept: list[Finding] = []
        for f in detect(self._pending, categories=self._owner.categories):
            if f.start >= cut:
                break
            growing = f.end == size or (
                f.rule == "private_key" and "-----END" not in self._pending[f.start : f.end]
            )
            if f.end > cut or growing:
                if not forced:
                    cut = f.start  # hold the value back whole
                    break
                cut = max(cut, f.end)  # past the hold limit: release the value, redacted
            kept.append(f)
        if cut == 0:
            return ""
        text, self._pending = self._pending[:cut], self._pending[cut:]
        self._boundary = max(self._boundary - cut, 0)
        self._separator = max(self._separator - cut, 0)
        self._scanned = max(self._scanned - cut, 0)
        self._tried = max(self._tried - cut, 0)
        return self._owner.apply(text, kept)

    def flush(self) -> str:
        """Everything still held back (the stream has ended)."""
        rest, self._pending = self._pending, ""
        self._boundary = self._separator = self._scanned = self._tried = 0
        return self._owner.text(rest) if rest else ""


# ------------------------------------------------------------------------------------ guardrails


class Guardrails:
    def __init__(
        self, *, classifier: Classifier, policies: PolicyCache, telemetry: Telemetry
    ) -> None:
        self._classifier = classifier
        self._rules = RulesClassifier()
        self._policies = policies
        self._telemetry = telemetry

    async def policy(self, org_id: UUID) -> GuardrailPolicy:
        return await self._policies.get(org_id)

    def _record(self, decisions: Sequence[Decision], request_id: str) -> None:
        for d in decisions:
            self._telemetry.metrics.guardrail_decisions.labels(
                str(d["stage"]), str(d["check"]), str(d["action"]), str(d["backend"])
            ).inc()
            log.info(
                "guardrail decision",
                extra={
                    "request_id": request_id,
                    "stage": d["stage"],
                    "check": d["check"],
                    "rule": d.get("rule"),
                    "action": d["action"],
                    "backend": d["backend"],
                    "probability": d.get("probability"),
                },
            )

    def _scan_chat(self, policy: GuardrailPolicy, req: CanonicalRequest) -> _ChatScan:
        findings = _Findings(policy)
        messages: list[Message] = []
        untrusted: list[str] = []
        for m in req.messages:
            content = tuple(
                replace(p, text=findings.text(p.text)) if isinstance(p, TextPart) else p
                for p in m.content
            )
            calls = tuple(replace(c, arguments=findings.text(c.arguments)) for c in m.tool_calls)
            messages.append(replace(m, content=content, tool_calls=calls))
            if m.role in _UNTRUSTED_ROLES:
                untrusted += [p.text for p in content if isinstance(p, TextPart)]
        state = "\n\n".join(untrusted)
        rule = None
        if policy.injection != "off" and state:
            (rule,) = self._rules.answer(state, [INJECTION_QUESTION])
        return _ChatScan(replace(req, messages=tuple(messages)), findings, state, rule)

    async def inspect_chat(
        self, org_id: UUID, req: CanonicalRequest, *, request_id: str
    ) -> Inspection:
        policy = await self._policies.get(org_id)
        scan = await _offload(_chat_chars(req), lambda: self._scan_chat(policy, req))
        decisions = scan.findings.decisions("input")
        blocked: DomainError | None = None
        if rules := scan.findings.blocking():
            blocked = GuardrailBlocked(
                "the request contains sensitive data the organisation's policy blocks"
                f" ({', '.join(rules)}); remove it and retry",
                details={"rules": rules},
            )
        elif scan.rule is not None:
            verdict, blocked = await self._injection(policy, scan.untrusted, scan.rule)
            decisions += verdict
        self._record(decisions, request_id)
        return Inspection(scan.request, policy, tuple(decisions), blocked)

    async def _injection(
        self, policy: GuardrailPolicy, state: str, rule: Verdict
    ) -> tuple[list[Decision], DomainError | None]:
        """The rules' verdict always counts; the classifier is asked only when they found
        nothing."""
        decisions: list[Decision] = []
        candidates = [rule]
        if not rule.value and self._classifier.name != "rules":
            if self._classifier.hosted and not policy.hosted_classifier:
                decisions.append(
                    {
                        "stage": "input",
                        "check": "injection",
                        "action": "skipped",
                        "backend": self._classifier.name,
                        "reason": "hosted classifier disabled for this organisation",
                    }
                )
            else:
                try:
                    candidates.append(await self._classify(state))
                except ClassifierError as exc:
                    closed = policy.on_classifier_error == "closed"
                    decisions.append(
                        {
                            "stage": "input",
                            "check": "injection",
                            "action": "failed_closed" if closed else "failed_open",
                            "backend": exc.backend,
                            "error": exc.kind.value,
                        }
                    )
                    if closed:
                        return decisions, Unavailable(
                            "the safety check is unavailable; try again shortly",
                            code="guardrail_unavailable",
                        )
        blocked: DomainError | None = None
        for d in candidates:
            hit = d.probability >= policy.injection_threshold
            action = (
                "passed" if not hit else ("blocked" if policy.injection == "block" else "flagged")
            )
            decisions.append(
                {
                    "stage": "input",
                    "check": "injection",
                    "action": action,
                    "backend": d.backend,
                    "version": d.version,
                    "probability": round(d.probability, 4),
                    "threshold": policy.injection_threshold,
                }
            )
            if hit and policy.injection == "block" and blocked is None:
                blocked = GuardrailBlocked(
                    "the request looks like a prompt-injection attempt and was blocked by policy",
                    code="prompt_injection",
                    details={"backend": d.backend},
                )
        return decisions, blocked

    async def _classify(self, state: str) -> Verdict:
        """The classifier's most confident injection verdict over all of the text, with text
        hidden in Unicode tag characters spelled out."""
        results = await asyncio.gather(
            *(
                self._classifier.decide(window, [INJECTION_QUESTION])
                for window in _windows(reveal_hidden(state))
            ),
            return_exceptions=True,
        )
        verdicts: list[Verdict] = []
        for result in results:
            if isinstance(result, BaseException):
                raise result
            verdicts += result
        return max(verdicts, key=lambda d: d.probability)

    async def screen_passages(
        self, org_id: UUID, hits: Sequence[Hit]
    ) -> tuple[list[Hit], list[dict[str, str]]]:
        """Retrieved passages the policy blocks on are dropped instead of blocking the request: a
        document holding a blocked value or an injection must not stop every question that
        retrieves it. Returns the passages kept and, for the ledger, those dropped and why.
        Redacting policies apply later, with the rest of the request."""
        policy = await self._policies.get(org_id)
        return await _offload(sum(len(h.content) for h in hits), lambda: _screen(policy, hits))

    async def inspect_inputs(
        self, org_id: UUID, inputs: Sequence[str], *, request_id: str
    ) -> tuple[tuple[str, ...], tuple[Decision, ...], DomainError | None]:
        """Embedding inputs: PII and secrets only (injection does not apply to embeddings)."""
        policy = await self._policies.get(org_id)
        findings = _Findings(policy)
        cleaned = await _offload(sum(map(len, inputs)), lambda: tuple(map(findings.text, inputs)))
        decisions = findings.decisions("input")
        blocked: DomainError | None = None
        if rules := findings.blocking():
            blocked = GuardrailBlocked(
                "the input contains sensitive data the organisation's policy blocks"
                f" ({', '.join(rules)})",
                details={"rules": rules},
            )
        self._record(decisions, request_id)
        return cleaned, tuple(decisions), blocked

    @staticmethod
    def output_filter(policy: GuardrailPolicy) -> OutputFilter | None:
        if policy.output == "off":
            return None
        categories: set[Category] = {"secret"}
        if policy.pii != "off":
            categories.add("pii")
        return OutputFilter(frozenset(categories))

    async def filter_response(
        self, policy: GuardrailPolicy, response: CanonicalResponse, *, request_id: str
    ) -> tuple[CanonicalResponse, list[Decision]]:
        out = self.output_filter(policy)
        if out is None:
            return response, []
        size = len(response.content or "") + sum(len(c.arguments) for c in response.tool_calls)
        filtered = await _offload(
            size,
            lambda: replace(
                response,
                content=out.text(response.content) if response.content else response.content,
                tool_calls=tuple(
                    replace(c, arguments=out.text(c.arguments)) for c in response.tool_calls
                ),
            ),
        )
        decisions = out.decisions()
        self.record_output(decisions, request_id)
        return filtered, decisions

    def record_output(self, decisions: Sequence[Decision], request_id: str) -> None:
        self._record(decisions, request_id)
