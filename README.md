# Message triage service

Reads inbound customer messages for the three GAJAN brands and decides, for each one:
what the customer wants, what can be extracted, what should happen next, who owns it,
how confident we are, and whether a human must see it before anything happens.

Read `DECISION_LOG.md` first.

## Run it

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/). Without uv, run `pip install google-genai anthropic pydantic pytest fastapi uvicorn` and drop `uv run`.

```bash
uv sync --extra dev --extra api
cp .env.example .env          # then paste your key after GEMINI_API_KEY=
uv run python -m triage data/messages.json
uv run pytest -q              # 26 behaviour tests, no API key needed
```

Each run writes two files:

| File | What it is |
|---|---|
| `output/OBSERVATION.md` | Readable report: run summary, cost, where every message went, and a step-by-step trace per message |
| `output/results.json` | The full machine-readable result for every message |

HTTP service, using the same pipeline:

```bash
uv run uvicorn triage.api:app --port 8000
curl -s localhost:8000/triage -H 'content-type: application/json' \
  -d '{"id":"t1","brand":"hair-studio","channel":"instagram","text":"are you open on sundays"}'
```

### Providers

The service picks a provider automatically. Force one with `--provider gemini|claude|rules`.

| Provider | Used when | Behaviour |
|---|---|---|
| Gemini (default) | `GEMINI_API_KEY` is set | Full classification with `gemini-2.5-flash-lite`. Low-risk messages can be automated. |
| Claude | `ANTHROPIC_API_KEY` is set and no Gemini key | Same prompt, schema and policy, using `claude-opus-5-5` by default |
| Offline rules | No key | Keyword classifier. Everything goes to a human, in roughly the right queue. |

If a single call fails, is refused, or returns invalid output, that message alone falls back to rules and goes to a human. Rate-limit errors are retried with backoff first.

| Variable | Default | Meaning |
|---|---|---|
| `GEMINI_MODEL` | `gemini-2.5-flash-lite` | Any Gemini text model. Priced ones are listed in `triage/gemini.py`. |
| `TRIAGE_MODEL` | `claude-opus-5-5` | Claude model, when using Claude |
| `TRIAGE_PROVIDER` | `auto` | Force `gemini`, `claude` or `rules` |

## How it works

```
file ──► ingest ──► signals ──► classifier ──► policy ──► result
         repair     regex IDs,  Gemini, JSON    confidence,
         & flag     risk flags  JSON schema     routing,
         records    (can only   (or rules       human-review
                    add caution) fallback)      rule
```

1. **Ingest** (`triage/ingest.py`). Accepts an array, a wrapped array, or JSON Lines. Every record is repaired or flagged, never dropped: null text, non-string text, missing ids, duplicate ids, bad timestamps, unknown brands, control characters, oversized bodies.
2. **Signals** (`triage/signals.py`). Deterministic regex for order ids, booking refs, phones, amounts, and risk flags: prompt injection, health topic, chargeback threat, repeat contact, urgency, vulnerable party, payment instructions from the sender, missing attachments. It also resolves relative dates against `received_at`.
3. **Classifier** (`triage/gemini.py`, or `triage/llm.py` for Claude). One model call per message. There are no tools, so the model can only label, never act. Customer text is delimited and declared untrusted. Output is constrained by a JSON schema and validated again with pydantic.
4. **Policy** (`triage/policy.py`). Plain code that reads only enums and booleans. It grounds every model-extracted identifier against the source text, computes confidence, assigns priority and an owning queue, and applies the human-review rule. Risk flags from step 2 can override the model. The model can never clear them.

### Output shape (per message)

`intents` is a list because one message can carry several requests. `routing` names one owning queue so one person is accountable. The other requests become linked tickets in `secondary_queues`.

```json
{
  "message_id": "MSG-008", "status": "ok", "classifier": "llm:gemini-2.5-flash-lite",
  "language": "en", "summary": "...",
  "intents": [{"intent": "refund_request", "detail": "...", "confidence": "high"},
              {"intent": "purchase_request", "detail": "...", "confidence": "high"}],
  "entities": {"order_ids": ["VW-48190"], "dates": [{"text": "22nd", "resolved": "2026-09-22"}], "...": []},
  "risk_flags": [], "confidence": 0.92, "confidence_notes": [],
  "routing": {"handler": "human", "queue": "vitalis-wellness:billing",
              "secondary_queues": ["vitalis-wellness:sales"], "priority": "P2",
              "response_sla": "4 hours", "action": "...", "reasons": ["H3 not on automation allow-list: ..."]},
  "suggested_reply": "draft for the agent", "open_questions": ["..."], "usage": {"cost_usd": 0.0003}
}
```

## Confidence and the human-review rule

Confidence is the lowest per-intent rating from the model, mapped high 0.92, medium 0.70, low 0.40. Deterministic checks then cap it: keyword fallback 0.50, three words or fewer 0.60, truncated text 0.50. An invented identifier costs 0.15.

A message goes to a human if any of these hold. Otherwise automation acts.

| Rule | Trigger |
|---|---|
| H1 | Keyword fallback, or the model call failed |
| H2 | Hard-stop flag: prompt injection, health topic, chargeback or legal threat, payment instructions from the sender, empty message, invented identifier |
| H3 | Any intent outside the automation allow-list |
| H4 | Confidence below 0.80 |
| H5 | Needs an earlier conversation we cannot see |
| H6 | The model returned an identifier that is not in the text |
| H7 | The automated action needs an identifier the message lacks |
| H8 | Damaged input record: unknown brand, truncated |

The automation allow-list is deliberately narrow and covers only reversible, low-risk actions. These are an order-status lookup, an answer from the brand FAQ or price list, a booking link, a thank-you, and a marketing opt-out. Refunds, cancellations, booking changes, payments and health questions always go to a person. On this file only a handful of messages are even eligible: the tracking question with an order id, the opening-hours and price questions, the two thank-yous, and the bare unsubscribe. Everything else needs a person by design.

## Cost per 1,000 messages

At 10,000 messages a day, traffic averages about 7 messages a minute. Peaks will be higher. One small worker handles this with a few concurrent calls. A free-tier key will hit per-minute and per-day limits, so production needs a paid key.

**Why the cheapest model is acceptable here.** The model only labels messages. Every risky decision is made by code: refunds, health questions, injection attempts, payments and emergencies all go to a human whatever the model says. A weaker model can therefore misfile a message, but it cannot cause harm. It still needs checking against labelled examples before automation is trusted.

**Assumptions** come from measuring this repo's prompt, at roughly 4 characters per token.

| Item | Tokens per message | Notes |
|---|---|---|
| System prompt and schema | ~1,650 | About 6,500 characters. Gemini may cache it implicitly. I assumed no caching. |
| Message and metadata | ~100 | Messages in this file average 295 characters with wrapper |
| Output JSON | ~300 | Thinking is switched off for 2.5 Flash-Lite |

**Arithmetic for Gemini 2.5 Flash-Lite**, at $0.10 input and $0.40 output per million tokens:

```
1,750 × $0.10/M + 300 × $0.40/M
= $0.000175 + $0.000120 = $0.0003 per message
```

| Model | Per 1,000 messages | Per day at 10k |
|---|---|---|
| Gemini 2.5 Flash-Lite (default) | ~$0.30 | ~$3 |
| Gemini 2.5 Flash | ~$1.30 | ~$13 |
| Claude Haiku 4.5 | ~$3.90 | ~$39 |
| Claude Opus 5.5 | ~$11 | ~$110 |

Gemini prices are from ai.google.dev/gemini-api/docs/pricing, checked 6 October 2026. Empty messages skip the model and cost nothing. The Batch API halves the price but is not real time, so a stranded traveller could wait hours. Batch is suitable only for backfills.

**Measured cost.** With a key, the run prints actual tokens and cost per 1,000 from the API's usage data. The same figures appear in `output/OBSERVATION.md`. Use them over the estimate above.

## Layout

```
triage/ingest.py    file loading and record repair
triage/signals.py   regex extraction, risk flags, date resolution
triage/gemini.py    Gemini call, retries, pricing (default)
triage/llm.py       shared prompt and JSON schema; Claude call
triage/report.py    observation report
triage/rules.py     offline keyword fallback
triage/policy.py    confidence, priority, routing, human-review rule
triage/pipeline.py  orchestration and per-message isolation
triage/api.py       FastAPI wrapper
tests/              behaviour tests and adversarial fixtures
```
