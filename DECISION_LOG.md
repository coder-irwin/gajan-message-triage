# Decision log

**Principle.** The model labels. Plain code decides. The model has no tools, so no message can make it do anything. Code checks can add caution but never remove it.

## What the data told me

- **One message, several requests.** MSG-008 wants a refund and a new order. Intents are a list. One queue owns the message and the rest become linked tickets.
- **Not everything is customer service.** MSG-021 is an invoice with "bank details in the PDF", a fraud pattern. MSG-024 is a CV and MSG-013 a wholesale lead.
- **One message is an attack.** MSG-005 orders a refund and no escalation. A regex flags it whatever the model says, and it goes to trust and safety with no reply.
- **Some messages are meaningless alone.** MSG-007, MSG-012 and MSG-020 depend on earlier conversations, and the file has no sender or thread id. They go to a human.
- **Urgency varies.** MSG-016, a stranded traveller with an elderly passenger, is P1.
- **Similar words, different risk.** A bare "Unsubscribe" is automated. "Cancel my subscription" goes to billing. Health questions such as MSG-019 are never automated.
- **Messy input.** Null text skips the model, Hinglish is handled, and relative dates resolve against `received_at`.

## What I chose not to build

- **Acting on decisions.** No refunds, CRM writes or sent replies. Wiring that in depends on your stack.
- **Brand knowledge bases.** Automated FAQ replies are templates that a KB would fill.
- **Identity, threads, attachments.** The data has no sender id or files.
- **Queues and authentication.** The web app is a demo viewer, not an agent console.
- **The Batch API.** It halves cost but is not real time, which P1 messages need.

## Where this breaks

Evidence from four live runs of `gemini-3.5-flash-lite`.

- **Model confidence is nearly useless.** In one run it rated 30 of 31 intents "high". Safety comes from the allow-list and hard-stop flags, not the score.
- **The model invents facts in drafts,** such as "Yes, we are open on Sundays". Automated replies are now code templates. Agent drafts are flagged, but a careless agent could still send one.
- **Routine routing varies between runs.** MSG-022, a cancellation for tomorrow, was read correctly in one run of four. Risky messages routed identically every time.
- **Regex flags are English-only.** Hindi or Punjabi injections rely on the model alone.
- **An outage sends everything to humans.** That is safe, but needs staffing.

## With another day

1. **Measure.** Label a few hundred real messages, measure accuracy per intent, and replace self-reported confidence with agreement between two cheap runs.
2. **Shadow mode** for a week: automation proposes, humans act.
3. **A date rule** that raises priority when a booking is within 24 hours.
4. **Deduplication and thread linking** by sender id.

## AI tools used

- **Claude Code**, Anthropic's coding agent on Claude Opus 5.5, wrote the code, tests, web app and first drafts of these documents. It ran the live tests and reviewed every result, catching invented replies, a cost bug and broken warning patterns. **Gemini** is only the classifier inside the product.
- **I** chose Gemini, the cheapest workable model, a pipeline rather than an agent, and bring-your-own-key. Full details are in [AI_USAGE.md](AI_USAGE.md).
