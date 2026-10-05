# Decision log

**Principle.** The model labels. Plain code decides. The model has no tools, so no message can make it do anything. Deterministic checks can add caution but never remove it.

## What the data told me

- **One message, several requests.** MSG-008 wants a refund and a new order. Intents are a list, one queue owns the message, and the rest become linked tickets.
- **Not everything is customer service.** MSG-021 is an invoice with "bank details in the PDF", a payment-fraud pattern. MSG-024 is a CV and MSG-013 a wholesale lead. These go to finance, recruiting and B2B.
- **One message is an attack.** MSG-005 orders a refund and no escalation. A regex flags it whatever the model says, and it goes to trust and safety with no reply. A test where the model obeys the attack shows policy still overrules it.
- **Some messages are meaningless alone.** MSG-007, MSG-012 and MSG-020 depend on earlier conversations, and the file has no sender or thread id. They go to a human.
- **Urgency is not uniform.** MSG-016 is a stranded traveller with an elderly passenger, emailed at 04:22 Toronto time. It is P1.
- **Similar words, different risk.** A bare "Unsubscribe" is automated. "Cancel my subscription" touches billing and goes to a human. Automation never answers health questions such as MSG-019.
- **Messy input.** Null text skips the model. Hinglish is handled. Relative dates resolve against `received_at`.

## What I chose not to build

- **Acting on decisions.** No refunds, CRM writes or sent replies. The output is a decision record. Wiring it in depends on your stack.
- **A knowledge base.** FAQ answers are automated only if a brand KB fills the template. Otherwise the message escalates.
- **Thread and identity linking.** The data has no sender id.
- **Attachments and media.** I flag that they are missing but do not fetch them.
- **Queue infrastructure, a dashboard, and authentication.**
- **The Batch API.** It halves cost, but it is not real time, which P1 messages need.

## Where this breaks

Evidence from four live runs of `gemini-3.5-flash-lite` on the 25 messages.

- **Model confidence is nearly useless.** It rated 30 of 31 intents "high". Safety comes from the allow-list and hard-stop flags, not the score.
- **The model invents facts in drafts,** such as "Yes, we are open on Sundays". Automated replies are now code templates. Agent drafts are flagged, but a careless agent could still send one.
- **Routing is not fully stable between runs.** "Booked for tomorrow but something came up" (MSG-022) was read as a P2 cancellation in one run of four, and a P3 change in the others. The quote acceptance (MSG-020) reached sales once in four. Risky messages routed identically every time.
- **Regex flags are English-only.** Hindi or Punjabi injections rely on the model alone.
- **Outages flood the human queue.** That is safe, but needs staffing. There is no deduplication or business-hours logic.

## With another day

1. **Label data and measure.** Label a few hundred real messages and measure routing accuracy per intent. Replace self-reported confidence with agreement between two cheap runs or models. Compare models on accuracy against cost.
2. **Shadow mode.** Run for a week where automation proposes and humans act, then compare.
3. **A date rule.** Raise priority when a booking date falls within 24 hours.
4. **Thread linking and deduplication** by sender id.
5. **Brand knowledge bases** to fill the reply templates.

## AI tools used

- **Claude Code** (Anthropic's coding agent, on Claude Opus 5.5) wrote the code, tests, web app and first drafts of these documents. It also ran the live tests and reviewed every result. That review caught the invented replies, a cost-counting bug and broken warning patterns, all fixed.
- **Gemini** is the classifier inside the product only.
- **I** chose Gemini and the cheapest workable model, the pipeline-not-agent design and bring-your-own-key. I also asked for a manual check of every result.
- The full list of what was reviewed, fixed and not verified is in [AI_USAGE.md](AI_USAGE.md).
