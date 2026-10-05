# Decision log

**Principle.** The model labels. Plain code decides. The model has no tools, so no message can make it do anything. Deterministic checks can add caution but never remove it.

## What the data told me

- **One message, several requests.** MSG-008 asks for a refund and places a new order. MSG-014 asks for three things. So intents are a list, one queue owns the message, and the rest become linked tickets.
- **Not everything is customer service.** MSG-021 is a vendor invoice with "bank details in the PDF", which fits a payment-fraud pattern. MSG-024 is a CV and MSG-013 is a wholesale lead. These go to finance, recruiting and B2B, and the invoice is flagged.
- **One message is an attack.** MSG-005 tells the system to approve a refund and skip escalation. The prompt tells the model to label it, and a regex flags it independently. It routes to trust and safety with no draft reply. A test simulates a model that obeys the attack, and policy still overrules it.
- **Some messages are meaningless alone.** MSG-007 is just a phone number, MSG-012 says "what about the other one", and MSG-020 refers to "the quote you sent". The file has no sender or thread id, so history cannot be joined. These go to a human with that reason.
- **Urgency is not uniform.** MSG-016 is a stranded traveller with an elderly passenger, sent by email at 04:22 Toronto time. It is P1, meaning page someone, not join a queue.
- **Similar words, different risk.** A bare "Unsubscribe" (MSG-017) is a marketing opt-out and is automated. "Cancel my subscription" (MSG-002) touches billing and goes to a human. MSG-019 asks if a supplement is safe with blood-pressure medication, and automation never answers that.
- **Messy input.** Null text (MSG-025) is likely a WhatsApp media message, so it skips the model. Hinglish (MSG-006) is handled by the model. Relative dates resolve against `received_at`.

## What I chose not to build

- **Acting on decisions.** No refunds, CRM writes or sent replies. The output is a decision record, and wiring it in depends on your stack.
- **A knowledge base.** FAQ and price answers are automation-eligible only if a brand KB exists. With no answer in the KB, the message escalates.
- **Thread and identity linking.** The data has no sender id.
- **Attachments and media.** I flag that they are missing but do not fetch them.
- **Queue infrastructure, a dashboard, and authentication.**
- **The Batch API.** It halves cost, but a 24-hour turnaround is unacceptable for P1 messages.

## Where this breaks

- **Confidence is uncalibrated.** It is the model's self-report, capped by rules. The 0.80 threshold is a starting guess, not a measured value.
- **Regex flags are English-only.** An injection or health question in Hindi or Punjabi relies on the model alone.
- **Outages flood the human queue.** If the API fails, every message goes to humans. That is safe, but a peak-hour outage needs staffing.
- **No deduplication.** MSG-011 says this is their third message, and each copy becomes its own ticket.
- **No business-hours awareness.** Priority ignores the customer's time zone and when staff are on shift.
- **The cheapest model will misread more often.** Code catches the dangerous cases, but misfiled routine messages still cost agent time.

## With another day

1. **Label data and calibrate.** Label these 25 plus a few hundred real messages, measure precision per intent, and set a threshold per intent. Compare Gemini Flash-Lite with a larger model on accuracy against cost.
2. **Shadow mode.** Run for a week where automation proposes and humans act, then compare.
3. **Thread linking and deduplication** by sender id.
4. **Brand knowledge bases** so FAQ answers can actually send.
5. **Multilingual test cases.**

## AI tools used

- **Claude Code** (Anthropic's coding agent, running Claude Opus 5.5) wrote the code, the tests and the adversarial fixtures. It also drafted the README and this log from my instructions.
- **[Akaash: before sending, describe what you reviewed or changed yourself, and list any other tools you used, such as the assistant you used to plan the task.]**
