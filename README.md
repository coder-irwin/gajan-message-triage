# Message triage service

Reads inbound customer messages for the three GAJAN brands and decides, for each one:
what the customer wants, what can be extracted, what should happen next, who owns it,
how confident we are, and whether a human must see it before anything happens.

Read `DECISION_LOG.md` first.

## Run it

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/) (or plain pip, see below).

```bash
uv sync --extra dev --extra api
export ANTHROPIC_API_KEY=sk-ant-...            # optional, see "Modes"
uv run python -m triage data/messages.json     # prints a summary table
                                               # full results: output/results.json
uv run pytest -q                               # 19 behaviour tests, no API key needed
```

Without uv: `pip install anthropic pydantic pytest fastapi uvicorn`, then the same commands without `uv run`.

HTTP service (same pipeline):

```bash
uv run uvicorn triage.api:app --port 8000
curl -s localhost:8000/triage -H 'content-type: application/json' \
  -d '{"id":"t1","brand":"hair-studio","channel":"instagram","text":"are you open on sundays"}'
```

### Modes

| Mode | When | Behaviour |
|---|---|---|
| Claude | `ANTHROPIC_API_KEY` is set | Full classification. Low-risk messages can be automated. |
| Offline rules | No key, or `--mode rules` | Keyword classifier. Everything goes to a human, in roughly the right queue. |
| Degraded | Key set but a call fails, refuses, or returns invalid output | That message alone falls back to rules and goes to a human. |

`sample_output/results_offline_rules.json` is the offline run on the supplied file.
Run with a key to produce the Claude version.

Configuration through environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `TRIAGE_MODEL` | `claude-opus-5-5` | Also priced: `claude-sonnet-5-5`, `claude-haiku-4-5` |
| `TRIAGE_EFFORT` | `low` | Reasoning effort. Classification does not need more. |
| `TRIAGE_FALLBACKS` | `1` | Server-side refusal fallback on Opus/Sonnet. Set `0` to disable. |

## How it works

```
file ──► ingest ──► signals ──► classifier ──► policy ──► result
         repair     regex IDs,  Claude, strict  confidence,
         & flag     risk flags  JSON schema     routing,
         records    (can only   (or rules       human-review
                    add caution) fallback)      rule
```

1. **Ingest** (`triage/ingest.py`). Accepts an array, a wrapped array, or JSON Lines. Every record is repaired or flagged, never dropped: null text, non-string text, missing ids, duplicate ids, bad timestamps, unknown brands, control characters, oversized bodies.
2. **Signals** (`triage/signals.py`). Deterministic regex for order ids, booking refs, phones, amounts, and risk flags: prompt injection, health topic, chargeback threat, repeat contact, urgency, vulnerable party, payment instructions from the sender, missing attachments. It also resolves relative dates against `received_at`.
3. **Classifier** (`triage/llm.py`). One Claude call per message. There are no tools, so the model can only label, never act. Customer text is delimited and declared untrusted. Output is constrained by a strict JSON schema and validated again with pydantic. The system prompt is cached.
4. **Policy** (`triage/policy.py`). Plain code that reads only enums and booleans. It grounds every model-extracted identifier against the source text, computes confidence, assigns priority and an owning queue, and applies the human-review rule. Risk flags from step 2 can override the model. The model can never clear them.

### Output shape (per message)

`intents` is a list because one message can carry several requests. `routing` names one owning queue so one person is accountable. The other requests become linked tickets in `secondary_queues`.

```json
{
  "message_id": "MSG-008", "status": "ok", "classifier": "llm:claude-opus-5-5",
  "language": "en", "summary": "...",
  "intents": [{"intent": "refund_request", "detail": "...", "confidence": "high"},
              {"intent": "purchase_request", "detail": "...", "confidence": "high"}],
  "entities": {"order_ids": ["VW-48190"], "dates": [{"text": "22nd", "resolved": "2026-09-22"}], "...": []},
  "risk_flags": [], "confidence": 0.92, "confidence_notes": [],
  "routing": {"handler": "human", "queue": "vitalis-wellness:billing",
              "secondary_queues": ["vitalis-wellness:sales"], "priority": "P2",
              "response_sla": "4 hours", "action": "...", "reasons": ["H3 not on automation allow-list: ..."]},
  "suggested_reply": "draft for the agent", "open_questions": ["..."], "usage": {"cost_usd": 0.011}
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

At 10,000 messages a day, traffic is about 7 messages a minute. One small worker handles it with 8 concurrent calls. The SDK retries 429 and 5xx errors with backoff.

**Assumptions** come from measuring the prompt in this repo. The tokenizer ratio is estimated, so treat these as plus or minus 30 percent.

| Item | Tokens per message | Notes |
|---|---|---|
| Cached prefix: system prompt and schema | ~2,200 | About 6,500 characters. At 7 messages a minute the 5-minute cache stays warm, so writes are negligible. |
| Uncached input: metadata and message | ~150 | Messages in this file average 295 characters with wrapper. |
| Output: JSON and low-effort thinking | ~500 | The JSON is about 250 tokens. Thinking cannot be disabled on Opus 5.5, so I assumed 250 more. This is the biggest uncertainty. |

**Arithmetic for Opus 5.5** at $4 input, $20 output and $0.20 cache read per million tokens:

```
2,200 × $0.20/M + 150 × $4/M + 500 × $20/M
= $0.00044 + $0.00060 + $0.01000 = $0.0110 per message
```

| Model | Per 1,000 messages | Per day at 10k | Notes |
|---|---|---|---|
| Claude Opus 5.5 (default) | ~$11.0 | ~$110 | Output tokens are 90 percent of cost |
| Claude Sonnet 5.5 | ~$5.7 | ~$57 | Same prompt. Needs a quality check on a labelled set |
| Claude Haiku 4.5 | ~$3.9 | ~$39 | Prefix is below Haiku's 4,096-token cache minimum, so no caching. Assumes 300 output tokens without thinking |

Empty messages skip the model and cost nothing. Output tokens dominate, so the main lever is model choice, not prompt length. The Batch API halves the price but can take up to 24 hours. A stranded traveller cannot wait that long, so batch is suitable only for backfills.

**Measured cost.** When run with a key, the CLI prints actual token usage and cost per 1,000 from the API's usage fields. Use that number over the estimate above.

## Layout

```
triage/ingest.py    file loading and record repair
triage/signals.py   regex extraction, risk flags, date resolution
triage/llm.py       Claude call, prompt, JSON schema, pricing
triage/rules.py     offline keyword fallback
triage/policy.py    confidence, priority, routing, human-review rule
triage/pipeline.py  orchestration and per-message isolation
triage/api.py       FastAPI wrapper
tests/              behaviour tests and adversarial fixtures
```
