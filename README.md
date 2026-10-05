# GAJAN Message Triage

A small service that reads each inbound customer message for Vitalis Wellness, Hair Studio and Voyage Travel. For each message it decides what the customer wants, what details can be pulled out, what should happen next, and who handles it. Every result carries a confidence score and a clear reason when a human must see it.

**Read [DECISION_LOG.md](DECISION_LOG.md) first.**

![Results of a live run on the 25 supplied messages](docs/screenshots/09-filter-automated.png)

## What it does, in one minute

- **The model labels, code decides.** Gemini reads each message and returns structured labels. Plain code then decides the queue, the priority and whether automation may act. The model has no tools, so no message can make it do anything.
- **Automation is narrow and safe.** It only handles order-status lookups, FAQ and price answers, booking links, thank-yous and marketing opt-outs. Automated replies are fixed templates written in code, never model text.
- **Risky messages always reach a person.** Refunds, cancellations, payments, health questions, emergencies and the injection attempt in the file all go to the right human queue, with a reason.
- **Nothing in the file breaks it.** Null text, Hinglish, a message that is only a phone number, duplicate ids and bad JSON are all handled. One bad record never affects another.

## Quick start: the web app

You need Python 3.10 or newer and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/coder-irwin/gajan-message-triage.git
cd gajan-message-triage
uv sync --extra api
uv run uvicorn triage.api:app --port 8000
```

Open http://localhost:8000, then:

1. Choose how to bring your own key.
2. Click **Load the 25 sample messages**.
3. Click **Run triage**.

Click any row to see why it was routed that way.

## Bring your own key

The app has no key of its own. You bring one, it is used for your request only, and it is never stored, logged or shown back.

| Option | How | When to use it |
|---|---|---|
| Gemini API key | Paste it into the web app, or use `--ask-key` on the command line | You have a key from [AI Studio](https://aistudio.google.com/apikey) |
| Google Cloud project | Type the project id. Run `gcloud auth application-default login` first. | You have Google Cloud with Vertex AI enabled |
| No key | Choose "No key" | Trying it out. Offline keyword rules send every message to a human. |

Two safety rules apply. The server never falls back to a key in its own environment for a web request. On a shared deployment the Google Cloud project option is switched off, so visitors cannot bill the host's project.

## A real run, step by step

These screenshots come from a live run on 6 October 2026 using `gemini-3.5-flash-lite` through Vertex AI.

**1. Bring your own key and load the messages.** The key field is masked, and the note explains where the key goes.

![Choosing a credential and loading messages](docs/screenshots/02-key-and-messages.png)

**2. Run.** All 25 messages took about 10 seconds. 6 were automated and 19 went to people. The run cost under 4 cents, which is $1.55 per 1,000 messages.

![All 25 results](docs/screenshots/03-results.png)

**3. The injection attempt is caught.** MSG-005 pretends to be a system notice and orders a refund. A regex flags it whatever the model says. It goes to trust and safety, no reply is drafted, and the trace shows every step.

![MSG-005 routed to trust and safety](docs/screenshots/04-injection-blocked.png)

**4. The emergency is P1.** A stranded traveller with an elderly passenger goes to the emergency desk with a 15-minute target. The model's draft promises a call, so the agent is warned to check it.

![MSG-016 is P1](docs/screenshots/05-urgent-p1.png)

**5. Automated replies are templates, not model text.** "Are you open on Sundays" is automated, but the reply is a slot for the brand FAQ. In an earlier run the model had written "Yes, we are open on Sundays" with no way of knowing the hours.

![MSG-018 uses a code template](docs/screenshots/06-automated-template.png)

**6. Risky drafts are flagged.** For the refund request, the model wrote "We have initiated your refund". The agent sees a warning before sending.

![MSG-008 draft warnings](docs/screenshots/07-draft-warnings.png)

**7. Health questions never get an automated answer.** The supplement and blood-pressure question goes to qualified staff.

![MSG-019 health question](docs/screenshots/08-health-question.png)

**8. A wrong key fails safely.** Every message goes to a human and the page says why. The key is not shown anywhere.

![Wrong key](docs/screenshots/10-wrong-key-fails-safe.png)

## Command line

```bash
uv sync --extra dev --extra api
uv run python -m triage data/messages.json --ask-key            # hidden key prompt
uv run python -m triage data/messages.json --gcp-project MY_ID  # or a Google Cloud project
uv run python -m triage data/messages.json                      # no key: offline rules
```

Each run writes two files:

| File | What it is |
|---|---|
| `output/OBSERVATION.md` | A readable report: summary, measured cost, where every message went, and a trace for each message |
| `output/results.json` | The full machine-readable result for every message |

You can also put credentials in a git-ignored `.env` file. See `.env.example`. Real runs are saved in [sample_output/](sample_output/).

## How it works

```
messages ─► ingest ─► signals ─► classifier ─► policy ─► decision
            repair    regex IDs  one Gemini    confidence, priority,
            & flag    and risk   call, JSON    queue, human-review
            records   flags      schema        rule, reply template
```

1. **Ingest.** Repairs or flags every record and never drops one.
2. **Signals.** Regex finds order ids, booking refs, phone numbers and amounts. It raises risk flags for injection, health, chargeback threats, repeat contact, urgency, vulnerable people, payment instructions and missing attachments. It also resolves dates such as "tomorrow" against the received time. These flags can only add caution.
3. **Classifier.** One Gemini call per message. The customer text is marked as untrusted. The answer must match a JSON schema and is validated again. If the call fails, that message falls back to keyword rules and goes to a person.
4. **Policy.** Plain code. Any order id the model returns that is not in the text is dropped. It then sets confidence and priority, picks one owning queue with linked tickets for other requests, and applies the human-review rule.

## When a human sees the message

A message goes to a person if any rule below applies. Otherwise automation acts.

| Rule | Trigger |
|---|---|
| H1 | The model call failed, or no key was given |
| H2 | A hard-stop flag: injection, health, chargeback or legal threat, payment instructions, empty message, invented identifier |
| H3 | Any request outside the automation allow-list |
| H4 | Confidence below 0.80 |
| H5 | It depends on an earlier conversation we cannot see |
| H6 | The model returned an identifier that is not in the text |
| H7 | The automated action needs an identifier the message lacks |
| H8 | The record itself is damaged, such as an unknown brand or truncated text |

Confidence is the model's lowest rating across the message's requests: high 0.92, medium 0.70, low 0.40. Code then lowers it for very short messages, the offline fallback, truncated text and invented identifiers. In practice the model rates almost everything "high", so H2 and H3 do most of the work. The decision log covers this.

## Cost per 1,000 messages

Measured from the API's own token counts. The default model ran four times on the 25 messages and the cheaper one twice. Results agreed to within a cent per 1,000.

| Model | Avg input tokens | Avg output tokens | Per 1,000 messages | Per day at 10,000 |
|---|---|---|---|---|
| `gemini-3.5-flash-lite` (default) | 2,079 | 373 | $1.55 | $15.50 |
| `gemini-2.5-flash-lite`, Vertex AI only | about 1,000 | about 390 | $0.25 | $2.50 |

How the default's number is built, at $0.30 input and $2.50 output per million tokens:

```
2,079 × $0.30/M + 373 × $2.50/M = $0.00062 + $0.00093 = $0.00155 per message
```

**Assumptions:**
- Real messages are about as long as these. Most input is the fixed prompt and schema, at about 1,950 tokens, so longer messages barely change the cost.
- Thinking is set to minimal, because classification does not need reasoning.
- Prices are from the [Gemini pricing page](https://ai.google.dev/gemini-api/docs/pricing), checked 6 October 2026.
- The cheaper 2.5 Flash-Lite is closed to new Gemini API keys, so it cannot be the default.

**Scale.** 10,000 messages a day averages about 7 a minute. With 8 calls in flight, the 25 messages took about 10 seconds, so one small server has a lot of headroom. Free-tier keys have low limits, so production needs a paid key. Rate-limit errors are retried with backoff.

## Run in Docker or on Cloud Run

```bash
docker build -t gajan-triage .
docker run -p 8080:8080 gajan-triage        # open http://localhost:8080
```

The image contains no credentials. Visitors bring their own key. The Google Cloud project option is off by default in the container. The same image can be deployed to Cloud Run.

## Tests

```bash
uv run pytest -q     # 30 tests, no key needed
```

The tests cover broken files, hostile records and a model that obeys the injection attack. They also cover a model that invents order ids, API failures and rate limits, automated replies never using model text, and a server-side key never being used for a caller who brought none.

## Project layout

```
triage/ingest.py     loads the file and repairs records
triage/signals.py    regex extraction, risk flags, date resolution
triage/gemini.py     Gemini call, retries, pricing (default provider)
triage/llm.py        shared prompt and JSON schema, Claude option
triage/rules.py      offline keyword fallback
triage/policy.py     confidence, priority, routing, human-review rule, reply templates
triage/pipeline.py   runs each message through the stages, keeps the trace
triage/report.py     writes the observation report
triage/api.py        web app and HTTP API with bring-your-own-key
triage/web/          the single-page web interface
tests/               behaviour tests and adversarial input files
sample_output/       saved live runs and their reports
docs/screenshots/    the screenshots above
```

## AI tools used

See the last section of [DECISION_LOG.md](DECISION_LOG.md) and the full account in [AI_USAGE.md](AI_USAGE.md).
