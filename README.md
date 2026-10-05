# Message triage service

Reads inbound customer messages for the three GAJAN brands and decides, for each one:
what the customer wants, what can be extracted, what should happen next, who owns it,
how confident we are, and whether a human must see it before anything happens.

Read `DECISION_LOG.md` first.

## Run it

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/). Without uv, run `pip install google-genai anthropic pydantic pytest fastapi uvicorn` and drop `uv run`.

```bash
uv sync --extra dev --extra api
uv run pytest -q                                   # 28 behaviour tests, no key needed
uv run python -m triage data/messages.json --ask-key
```

`--ask-key` prompts for your Gemini API key with hidden input. Press Enter without a key to run offline.

Each run writes two files:

| File | What it is |
|---|---|
| `output/OBSERVATION.md` | Readable report: run summary, measured cost, where every message went, and a step-by-step trace per message |
| `output/results.json` | The full machine-readable result for every message |

`sample_output/` holds real runs on the supplied file: the default `gemini-3.5-flash-lite`, the cheaper `gemini-2.5-flash-lite` for comparison, and the offline rules mode.

### Bring your own key

The service has no key of its own. Supply one of these. A key is held in memory for the run and is never written to output or logs.

| Option | How |
|---|---|
| Hidden prompt | `--ask-key` |
| Environment or `.env` file | `cp .env.example .env`, then set `GEMINI_API_KEY`. `.env` is git-ignored. |
| Google Cloud project instead of a key | `--gcp-project YOUR_PROJECT`, or `TRIAGE_GCP_PROJECT` in `.env`. This calls Gemini through Vertex AI and needs `gcloud auth application-default login` first. |
| Per request on the HTTP service | Send the header `X-Gemini-Api-Key` |

With no key at all, the offline rules classifier runs and every message goes to a human.

HTTP service, using the same pipeline:

```bash
uv run uvicorn triage.api:app --port 8000
curl -s localhost:8000/triage -H 'content-type: application/json' \
  -H "X-Gemini-Api-Key: $GEMINI_API_KEY" \
  -d '{"id":"t1","brand":"hair-studio","channel":"instagram","text":"are you open on sundays"}'
```

### Providers and models

| Provider | Used when | Behaviour |
|---|---|---|
| Gemini (default) | A Gemini key or Google Cloud project is supplied | Full classification with `gemini-3.5-flash-lite`. Low-risk messages can be automated. |
| Claude | `ANTHROPIC_API_KEY` is set and no Gemini credentials | Same prompt, schema and policy, using `claude-opus-5-5` |
| Offline rules | No credentials | Keyword classifier. Everything goes to a human, in roughly the right queue. |

If a single call fails, is refused, or returns invalid output, that message alone falls back to rules and goes to a human. Rate-limit errors are retried with backoff first.

| Variable | Default | Meaning |
|---|---|---|
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` | Any Gemini text model. Priced ones are listed in `triage/gemini.py`. |
| `TRIAGE_PROVIDER` | `auto` | Force `gemini`, `claude` or `rules`. Same as `--provider`. |
| `TRIAGE_MODEL` | `claude-opus-5-5` | Claude model, when using Claude |

The cheaper `gemini-2.5-flash-lite` is closed to new Gemini API keys, so it cannot be the default. It still works through Vertex AI.

## How it works

```
file ──► ingest ──► signals ──► classifier ──► policy ──► result
         repair     regex IDs,  Gemini call,    confidence,
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
  "message_id": "MSG-008", "status": "ok", "classifier": "llm:gemini-3.5-flash-lite",
  "language": "en", "summary": "...",
  "intents": [{"intent": "refund_request", "detail": "...", "confidence": "high"},
              {"intent": "purchase_request", "detail": "...", "confidence": "high"}],
  "entities": {"order_ids": ["VW-48190"], "dates": [{"text": "22nd", "resolved": "2026-09-22"}], "...": []},
  "risk_flags": [], "confidence": 0.92, "confidence_notes": [],
  "routing": {"handler": "human", "queue": "vitalis-wellness:billing",
              "secondary_queues": ["vitalis-wellness:sales"], "priority": "P2",
              "response_sla": "4 hours", "action": "...", "reasons": ["H3 not on automation allow-list: ..."]},
  "suggested_reply": "draft for the agent", "reply_source": "model_draft_for_agent",
  "draft_warnings": ["promises a refund"], "open_questions": ["..."], "usage": {"cost_usd": 0.0016}
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

The automation allow-list is deliberately narrow and covers only reversible, low-risk actions. These are an order-status lookup, an answer from the brand FAQ or price list, a booking link, a thank-you, and a marketing opt-out. Refunds, cancellations, booking changes, payments and health questions always go to a person. Automation never sends model-written text. It sends a fixed template owned by code, with slots filled from the carrier API or the brand knowledge base. In the live run the model drafted "Yes, we are open on Sundays" with no knowledge of the salon's hours. Model drafts only ever go to agents, and drafts that promise refunds, timelines or calls are flagged for checking.

In the live run, 5 of 25 messages were automated: the tracking question with an order id, the two thank-yous, the bare unsubscribe, and the opening-hours question. The balayage price question went to a person because the model was only medium-confident it was also a booking request. Everything else needs a person by design.

## Cost per 1,000 messages

These are measured, not estimated. They come from the API's own token counts on the supplied 25 messages. The empty message skips the model, so there were 24 calls.

| Model | Avg input tokens | Avg output tokens | Per 1,000 messages | Per day at 10k |
|---|---|---|---|---|
| `gemini-3.5-flash-lite` (default) | 2,079 | 373 | $1.55 | $15.50 |
| `gemini-2.5-flash-lite`, Vertex AI only | about 1,000 | about 390 | $0.25 | $2.50 |

**How the default's number is built**, at $0.30 input and $2.50 output per million tokens:

```
2,079 × $0.30/M + 373 × $2.50/M
= $0.00062 + $0.00093 = $0.00155 per message
```

**Assumptions.**
- Real messages are about as long as these. Input is mostly the fixed system prompt and schema, at about 1,950 tokens, so longer messages barely move the cost.
- Output is 60 percent of the cost. Shorter output, such as dropping the agent draft, is the main lever after model choice.
- Thinking is set to minimal. Classification does not need reasoning.
- Prices are from ai.google.dev/gemini-api/docs/pricing, checked 6 October 2026.

**Throughput.** 10,000 a day averages about 7 messages a minute. Median latency was 2.4 seconds per message, with 4 in flight at once. One worker therefore handles about 100 a minute. Free-tier keys have low per-minute and per-day limits, so production needs a paid key. Rate-limit errors are retried with backoff.

**Why the cheapest model is acceptable.** The model only labels messages. Refunds, health questions, injection attempts, payments, emergencies and every outgoing automated reply are controlled by code. A weaker model can misfile a message, but it cannot send an invented answer or approve anything.

The Batch API halves the price but is not real time. Batch is suitable only for backfills.

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
