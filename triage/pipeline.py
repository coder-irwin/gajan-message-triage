"""Orchestration: normalise -> signals -> classify -> policy, one message at a time,
with bounded concurrency. A failure on one message never affects another; the
failed message is emitted as an error result in a human queue.

Every result carries a `trace`: one line per stage saying what happened, so a
run can be audited end to end (see triage/report.py).
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Protocol

from . import rules, signals
from .models import Analysis, InboundMessage, Routing, TraceStep, TriageResult, Usage
from .policy import KNOWN_BRANDS, SLA, decide


def _support_queue(brand: str) -> str:
    return f"{brand}:support" if brand in KNOWN_BRANDS else "group:unrouted"


class Classifier(Protocol):
    name: str
    async def classify(self, msg: InboundMessage) -> tuple[Analysis, Usage]: ...


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env reader so keys never have to be typed into a shell or a chat.
    Existing environment variables win over the file."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip().removeprefix("export ").strip(), v.strip().strip('"').strip("'"))


def make_classifier(provider: str = "auto", api_key: str | None = None,
                    gcp_project: str | None = None) -> Classifier | None:
    """provider: auto | gemini | claude | rules.
    auto picks Gemini if a Gemini key or GCP project is available, else Claude if an
    Anthropic key is set, else rules. api_key / gcp_project are bring-your-own credentials
    supplied by the caller; they take precedence over the environment."""
    load_dotenv()
    provider = os.environ.get("TRIAGE_PROVIDER", provider) if provider == "auto" else provider
    gcp_project = gcp_project or os.environ.get("TRIAGE_GCP_PROJECT")
    if provider in ("auto", "gemini") and (api_key or gcp_project):
        from .gemini import GeminiClassifier
        return GeminiClassifier(api_key=api_key, gcp_project=None if api_key else gcp_project)
    has_gemini = any(os.environ.get(k) for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY"))
    has_claude = any(os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"))
    if provider == "rules":
        return None
    if provider == "gemini" or (provider == "auto" and has_gemini):
        from .gemini import GeminiClassifier
        return GeminiClassifier()
    if provider == "claude" or (provider == "auto" and has_claude):
        from .llm import ClaudeClassifier
        return ClaudeClassifier()
    return None


def _error_result(msg: InboundMessage, err: str, started: float, trace: list[TraceStep]) -> TriageResult:
    trace.append(TraceStep(stage="decision", detail=f"internal error ({err}); sent to a human"))
    return TriageResult(
        message_id=msg.id, brand=msg.brand, channel=msg.channel, received_at=msg.received_at,
        status="error", classifier="none", confidence=0.0,
        confidence_notes=[f"internal error: {err}"],
        routing=Routing(handler="human", queue=_support_queue(msg.brand), action="Agent reads and routes manually",
                        priority="P3", response_sla=SLA["P3"], reasons=["H1 processing error"]),
        input_problems=msg.problems, latency_ms=int((time.monotonic() - started) * 1000),
        text=msg.text, trace=trace,
    )


def _fmt_intents(a: Analysis) -> str:
    return ", ".join(f"{i.intent.value} ({i.confidence})" for i in a.intents)


async def triage_one(msg: InboundMessage, clf: Classifier | None) -> TriageResult:
    started = time.monotonic()
    trace: list[TraceStep] = []
    try:
        trace.append(TraceStep(stage="ingest", detail="; ".join(msg.problems) if msg.problems else "record clean"))
        sig = signals.extract(msg.text)
        found = {k: v for k, v in dict(order_ids=sig.order_ids, booking_refs=sig.booking_refs,
                                       phones=sig.phone_numbers, amounts=sig.amounts).items() if v}
        trace.append(TraceStep(stage="signals", detail=f"flags={sig.flags or 'none'}; regex entities={found or 'none'}; "
                                                       f"words={sig.word_count}; language hint={sig.language_hint}"))

        # Empty text: no model call. On WhatsApp this is usually an image, voice note or sticker
        # whose media we were not given.
        if not msg.text:
            trace.append(TraceStep(stage="classifier", detail="skipped: nothing to classify, no model call made"))
            routing = Routing(handler="human", queue=_support_queue(msg.brand),
                              action="Fetch any media attachment from the channel API; if none, ask the customer to resend",
                              priority="P3", response_sla=SLA["P3"], reasons=["H2 hard-stop flags: empty_message"])
            trace.append(TraceStep(stage="decision", detail=f"human -> {routing.queue} ({routing.priority})"))
            return TriageResult(
                message_id=msg.id, brand=msg.brand, channel=msg.channel, received_at=msg.received_at,
                status="ok", classifier="none (empty message)", confidence=1.0,
                confidence_notes=["no text to classify; routing is deterministic"],
                risk_flags=sig.flags, routing=routing, input_problems=msg.problems,
                latency_ms=int((time.monotonic() - started) * 1000), text=msg.text, trace=trace,
            )

        usage, degraded, status = Usage(), None, "ok"
        if clf is None:
            analysis, classifier_name = rules.classify(msg, sig), "rules (offline mode)"
            status = "degraded"
            trace.append(TraceStep(stage="classifier", detail=f"offline keyword rules -> {_fmt_intents(analysis)}"))
        else:
            t0 = time.monotonic()
            try:
                analysis, usage = await clf.classify(msg)
                classifier_name = clf.name
                trace.append(TraceStep(stage="classifier", detail=(
                    f"{clf.name} in {int((time.monotonic() - t0) * 1000)} ms, "
                    f"tokens in={usage.input_tokens + usage.cache_read_input_tokens} "
                    f"(cached {usage.cache_read_input_tokens}) out={usage.output_tokens}, "
                    f"${usage.cost_usd:.6f} -> {_fmt_intents(analysis)}; urgency={analysis.customer_state.urgency}; "
                    f"needs history={analysis.requires_prior_context}; "
                    f"injection seen={analysis.contains_instructions_to_system}")))
            except Exception as exc:  # ClassifierError or anything unexpected from the SDK
                analysis, classifier_name = rules.classify(msg, sig), "rules (llm failed)"
                degraded, status = str(exc)[:200], "degraded"
                trace.append(TraceStep(stage="classifier", detail=(
                    f"{clf.name} FAILED ({degraded}); fell back to keyword rules -> {_fmt_intents(analysis)}")))

        d = decide(msg, sig, analysis, classifier_name, degraded)
        model_intents = {i.intent for i in analysis.intents}
        added = [i.intent.value for i in d["intents"] if i.intent not in model_intents]
        policy_bits = []
        if added:
            policy_bits.append(f"added intents the classifier missed: {added}")
        if d["confidence_notes"]:
            policy_bits.append("confidence adjustments: " + "; ".join(d["confidence_notes"]))
        policy_bits.append(f"final confidence={d['confidence']}")
        rules_fired = [r for r in d["routing"].reasons if r[:2] in {f"H{n}" for n in range(1, 9)}]
        policy_bits.append("human-review rules fired: " + ("; ".join(rules_fired) if rules_fired else "none"))
        trace.append(TraceStep(stage="policy", detail=" | ".join(policy_bits)))
        r = d["routing"]
        trace.append(TraceStep(stage="decision", detail=(
            f"{r.handler} -> {r.queue} ({r.priority}, SLA {r.response_sla})"
            + (f"; linked tickets: {r.secondary_queues}" if r.secondary_queues else "")
            + f"; action: {r.action}")))
        return TriageResult(
            message_id=msg.id, brand=msg.brand, channel=msg.channel, received_at=msg.received_at,
            status=status, classifier=classifier_name, language=analysis.language, summary=analysis.summary,
            open_questions=analysis.open_questions, input_problems=msg.problems, usage=usage,
            latency_ms=int((time.monotonic() - started) * 1000), text=msg.text, trace=trace, **d,
        )
    except Exception as exc:  # last line of defence: never let one record kill the batch
        return _error_result(msg, f"{type(exc).__name__}: {exc}"[:300], started, trace)


async def triage_all(msgs: list[InboundMessage], clf: Classifier | None, concurrency: int = 8) -> list[TriageResult]:
    sem = asyncio.Semaphore(concurrency)

    async def guarded(m: InboundMessage) -> TriageResult:
        async with sem:
            return await triage_one(m, clf)

    return list(await asyncio.gather(*(guarded(m) for m in msgs)))
