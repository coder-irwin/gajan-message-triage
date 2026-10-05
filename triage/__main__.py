"""CLI: uv run python -m triage data/messages.json [--provider auto|gemini|claude|rules]"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

from .ingest import load_records, normalise
from . import report
from .pipeline import make_classifier, triage_all


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="triage", description="Triage inbound customer messages.")
    ap.add_argument("input", help="path to messages JSON (array, wrapped array, or JSON Lines)")
    ap.add_argument("--out", default="output/results.json", help="where to write the full results")
    ap.add_argument("--provider", choices=["auto", "gemini", "claude", "rules"], default="auto",
                    help="auto: Gemini if GEMINI_API_KEY is set, else Claude if ANTHROPIC_API_KEY is set, else offline rules")
    ap.add_argument("--report", default="output/OBSERVATION.md", help="where to write the readable observation report")
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args(argv)

    try:
        raw, file_problems = load_records(args.input)
    except OSError as exc:
        print(f"cannot read {args.input}: {exc}", file=sys.stderr)
        return 2
    msgs = normalise(raw)
    clf = make_classifier(args.provider)
    if clf is None:
        print("No GEMINI_API_KEY or ANTHROPIC_API_KEY found (or --provider rules): running the offline keyword classifier. "
              "Everything will be routed to humans.\n", file=sys.stderr)
    results = asyncio.run(triage_all(msgs, clf, args.concurrency))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"file_problems": file_problems,
                               "results": [r.model_dump(mode="json") for r in results]},
                              indent=2, ensure_ascii=False))

    label = getattr(clf, "name", "rules (offline mode)")
    rep = Path(args.report)
    rep.parent.mkdir(parents=True, exist_ok=True)
    rep.write_text(report.build(results, label, args.input, file_problems))

    for p in file_problems:
        print(f"[file] {p}")
    print(f"{'id':<10} {'pri':<3} {'handler':<10} {'conf':<5} {'queue':<34} intents")
    for r in results:
        intents = ",".join(i.intent.value for i in r.intents) or "-"
        print(f"{r.message_id:<10} {r.routing.priority:<3} {r.routing.handler:<10} {r.confidence:<5} "
              f"{r.routing.queue:<34} {intents}")

    n = len(results)
    handlers = Counter(r.routing.handler for r in results)
    statuses = Counter(r.status for r in results)
    cost = sum(r.usage.cost_usd for r in results)
    llm_calls = sum(1 for r in results if r.usage.input_tokens)
    print(f"\n{n} messages | automation {handlers['automation']} | human {handlers['human']} | "
          f"status {dict(statuses)}")
    if llm_calls:
        tin = sum(r.usage.input_tokens + r.usage.cache_read_input_tokens + r.usage.cache_creation_input_tokens for r in results)
        tout = sum(r.usage.output_tokens for r in results)
        print(f"model calls {llm_calls} | input tokens {tin} | output tokens {tout} | "
              f"cost ${cost:.5f} | ${cost / llm_calls * 1000:.3f} per 1,000 messages at this mix")
    print(f"full results written to {out}\nobservation report written to {rep}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
