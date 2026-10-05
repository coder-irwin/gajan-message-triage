"""Write a human-readable observation report of one run: what happened to every
message at every stage, what action it triggered, and what it cost."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from statistics import median

from .models import TriageResult


def _cell(s: str, n: int = 70) -> str:
    s = (s or "").replace("|", "/").replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def build(results: list[TriageResult], classifier_label: str, input_path: str, file_problems: list[str]) -> str:
    n = len(results)
    handler = Counter(r.routing.handler for r in results)
    prio = Counter(r.routing.priority for r in results)
    status = Counter(r.status for r in results)
    queues = Counter(r.routing.queue for r in results)
    flags = Counter(f for r in results for f in r.risk_flags)
    calls = [r for r in results if r.usage.input_tokens or r.usage.output_tokens or r.usage.cache_read_input_tokens]
    cost = sum(r.usage.cost_usd for r in results)
    tin = sum(r.usage.input_tokens + r.usage.cache_read_input_tokens for r in calls)
    tcached = sum(r.usage.cache_read_input_tokens for r in calls)
    tout = sum(r.usage.output_tokens for r in calls)
    lat = [r.latency_ms for r in calls]

    L: list[str] = []
    L.append("# Observation report: message triage run\n")
    L.append(f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} from `{input_path}`.\n")
    L.append(f"Classifier: **{classifier_label}**.\n")
    if file_problems:
        L.append("File-level problems: " + "; ".join(file_problems) + "\n")

    L.append("## How a message moves through the pipeline\n")
    L.append("1. **Ingest** repairs or flags the record. Nothing is dropped.")
    L.append("2. **Signals** run regex checks for IDs, amounts and risk flags. These flags can only add caution.")
    L.append("3. **Classifier** is one model call per message. It labels intents and extracts details. It has no tools and cannot act.")
    L.append("4. **Policy** is plain code. It checks the model's IDs against the text, sets confidence and priority, and applies the human-review rules H1 to H8.")
    L.append("5. **Decision** is automation or a human queue, with an action and a response time.\n")

    L.append("## Run summary\n")
    L.append("| Measure | Value |\n|---|---|")
    L.append(f"| Messages | {n} |")
    L.append(f"| Sent to automation | {handler['automation']} |")
    L.append(f"| Sent to a human | {handler['human']} |")
    L.append(f"| Status ok / degraded / error | {status['ok']} / {status['degraded']} / {status['error']} |")
    L.append(f"| Priority P1 / P2 / P3 / P4 | {prio['P1']} / {prio['P2']} / {prio['P3']} / {prio['P4']} |")
    L.append(f"| Model calls | {len(calls)} |")
    if calls:
        L.append(f"| Input tokens, of which cached | {tin:,}, {tcached:,} |")
        L.append(f"| Output tokens | {tout:,} |")
        L.append(f"| Total cost | ${cost:.5f} |")
        L.append(f"| Cost per 1,000 messages at this mix | ${cost / len(calls) * 1000:.3f} |")
        L.append(f"| Cost per day at 10,000 messages | ${cost / len(calls) * 10000:.2f} |")
        L.append(f"| Latency per message, median / max | {int(median(lat))} ms / {max(lat)} ms |")
    L.append("")

    L.append("### Where the messages went\n")
    L.append("| Queue | Messages |\n|---|---|")
    for q, c in queues.most_common():
        L.append(f"| {q} | {c} |")
    L.append("")
    if flags:
        L.append("### Risk flags raised\n")
        L.append("| Flag | Messages |\n|---|---|")
        for f, c in flags.most_common():
            L.append(f"| {f} | {c} |")
        L.append("")

    L.append("## Worth a look\n")
    notable = []
    for r in results:
        if r.routing.priority == "P1":
            notable.append(f"- **{r.message_id} is P1.** {r.routing.action}")
        if "prompt_injection" in r.risk_flags:
            notable.append(f"- **{r.message_id} tried to instruct the system.** It went to {r.routing.queue} and no reply was drafted.")
        if r.status == "degraded" and not r.classifier.startswith("rules (offline"):
            notable.append(f"- **{r.message_id}: the model call failed,** so it fell back to rules and a human. "
                           + next((t.detail for t in r.trace if t.stage == "classifier"), ""))
        pol = next((t.detail for t in r.trace if t.stage == "policy"), "")
        if "added intents" in pol:
            notable.append(f"- **{r.message_id}: policy overruled the model.** {pol.split(' | ')[0]}")
        if "grounding_failure" in r.risk_flags:
            notable.append(f"- **{r.message_id}: the model invented an identifier,** which was dropped.")
        if r.routing.handler == "automation":
            notable.append(f"- **{r.message_id} was automated.** {r.routing.action}")
    L.extend(notable or ["- Nothing unusual."])
    L.append("")

    L.append("## All messages at a glance\n")
    L.append("| ID | Brand | Message | Intents | Conf | Pri | Handler | Queue |")
    L.append("|---|---|---|---|---|---|---|---|")
    for r in results:
        intents = ", ".join(i.intent.value for i in r.intents) or "none"
        L.append(f"| {r.message_id} | {r.brand} | {_cell(r.text, 60) or '(empty)'} | {intents} | "
                 f"{r.confidence} | {r.routing.priority} | {r.routing.handler} | {r.routing.queue} |")
    L.append("")

    L.append("## Message by message\n")
    for r in results:
        L.append(f"### {r.message_id}: {r.brand}, {r.channel}\n")
        L.append(f"> {_cell(r.text, 600) or '(empty message)'}\n")
        if r.summary:
            L.append(f"**Summary.** {r.summary}\n")
        L.append(f"**Decision.** {r.routing.handler.upper()} to `{r.routing.queue}`, {r.routing.priority}, "
                 f"respond within {r.routing.response_sla}. Confidence {r.confidence}.\n")
        L.append(f"**Action triggered.** {r.routing.action}\n")
        if r.routing.secondary_queues:
            L.append(f"**Linked tickets.** {', '.join(r.routing.secondary_queues)}\n")
        ents = {k: v for k, v in r.entities.model_dump().items() if v}
        if ents:
            parts = []
            for k, v in ents.items():
                if k == "dates":
                    v = [f"{d['text']} = {d['resolved'] or '?'}" for d in v]
                parts.append(f"{k}: {', '.join(map(str, v))}")
            L.append("**Extracted.** " + "; ".join(parts) + "\n")
        if r.risk_flags:
            L.append(f"**Risk flags.** {', '.join(r.risk_flags)}\n")
        if r.suggested_reply:
            L.append(f"**Draft reply for the agent.** {r.suggested_reply}\n")
        if r.open_questions:
            L.append("**Still to find out.** " + " ".join(r.open_questions) + "\n")
        L.append("**Trace.**\n")
        for t in r.trace:
            L.append(f"- `{t.stage}`: {t.detail}")
        L.append("")
    return "\n".join(L)
