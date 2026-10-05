"""Deterministic policy: confidence score, routing, and the human-review rule.

The model proposes; this module decides. Nothing in here reads model prose,
only enums, booleans and grounded identifiers, so a cleverly worded message
cannot talk its way past it.

THE HUMAN-REVIEW RULE (a message goes to a human if ANY of these hold):
  H1  the classifier is the keyword fallback, or the model call failed
  H2  a hard-stop risk flag is present (see HARD_STOP_FLAGS)
  H3  any intent in the message is not on the automation allow-list
  H4  confidence < AUTOMATION_THRESHOLD
  H5  the request depends on an earlier conversation we cannot see
  H6  the model returned an identifier that is not in the message text
  H7  the automated action needs an identifier the message does not contain
  H8  the input record itself was damaged (unknown brand, truncated, empty)
Only when none apply does automation act, and automation can only do
low-risk, reversible things: send a status lookup, answer from the brand
knowledge base, send a booking link, thank a happy customer, or process a
marketing opt-out. Money, bookings, health and account changes always go
to a person.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Analysis, Entities, InboundMessage, Intent, IntentItem, Routing
from .signals import Signals, grounded, resolve_date

AUTOMATION_THRESHOLD = 0.80
CONF_VALUE = {"high": 0.92, "medium": 0.70, "low": 0.40}
SLA = {"P1": "15 minutes", "P2": "4 hours", "P3": "1 business day", "P4": "3 business days or no reply needed"}

HARD_STOP_FLAGS = {
    "prompt_injection",
    "health_topic",
    "chargeback_or_legal_threat",
    "payment_instruction_from_sender",
    "empty_message",
    "grounding_failure",
}


@dataclass(frozen=True)
class IntentPolicy:
    queue: str          # "<brand>:x" queues are per brand, "group:x" are shared across brands
    action: str
    priority: str
    automatable: bool = False
    needs: str | None = None   # entity the automated action requires


POLICY: dict[Intent, IntentPolicy] = {
    Intent.ORDER_STATUS: IntentPolicy("support", "Look up carrier tracking for the order and send the status", "P3", True, "order_ids"),
    Intent.ORDER_PROBLEM: IntentPolicy("support", "Open a replacement/return case; agent approves remedy", "P2"),
    Intent.REFUND_REQUEST: IntentPolicy("billing", "Verify the order and approve or decline the refund", "P2"),
    Intent.BILLING_DISPUTE: IntentPolicy("billing", "Check the payment gateway for the duplicate charge and reverse it if confirmed", "P2"),
    Intent.SUBSCRIPTION_CANCEL: IntentPolicy("billing", "Verify identity, cancel the subscription, confirm the end date", "P3"),
    Intent.MARKETING_UNSUBSCRIBE: IntentPolicy("group:marketing_ops", "Suppress the sender from marketing lists and confirm", "P4", True),
    Intent.PURCHASE_REQUEST: IntentPolicy("sales", "Hot lead: confirm the quote or order and send a payment link", "P2"),
    Intent.PRICING_INQUIRY: IntentPolicy("front_desk", "Answer from the brand price list; escalate if not covered", "P3", True),
    Intent.PRODUCT_QUESTION: IntentPolicy("front_desk", "Answer from the brand knowledge base; escalate if not covered", "P3", True),
    Intent.HEALTH_SAFETY_QUESTION: IntentPolicy("health_advisory", "Qualified staff respond; never answered by automation", "P2"),
    Intent.BOOKING_NEW: IntentPolicy("bookings", "Send the booking link with live availability", "P3", True),
    Intent.BOOKING_CHANGE: IntentPolicy("bookings", "Check availability and any change fee, then confirm with the customer", "P3"),
    Intent.BOOKING_CANCEL: IntentPolicy("bookings", "Release the slot, apply the cancellation policy, offer to rebook", "P2"),
    Intent.TRAVEL_DISRUPTION: IntentPolicy("emergency_desk", "Page the on-call agent to call the customer and rebook", "P1"),
    Intent.TRAVEL_PLANNING: IntentPolicy("sales", "Travel consultant prepares options and a quote", "P3"),
    Intent.GENERAL_INFO: IntentPolicy("front_desk", "Answer from the brand FAQ (hours, location, policies)", "P4", True),
    Intent.B2B_WHOLESALE: IntentPolicy("group:b2b_sales", "Forward to the wholesale/partnerships lead", "P3"),
    Intent.JOB_APPLICATION: IntentPolicy("group:recruiting", "Forward to recruiting; send the careers-page auto-acknowledgement", "P4"),
    Intent.VENDOR_INVOICE: IntentPolicy("group:finance_ap", "Forward to accounts payable; verify the vendor out-of-band before any payment", "P3"),
    Intent.POSITIVE_FEEDBACK: IntentPolicy("feedback", "Send a thank-you; log for staff recognition and reviews", "P4", True),
    Intent.COMPLAINT: IntentPolicy("support", "Senior agent reviews and responds", "P2"),
    Intent.CONTACT_DETAILS_ONLY: IntentPolicy("support", "Match to the sender's open conversation; it is probably a reply to an agent", "P3"),
    Intent.UNCLEAR_NEEDS_CONTEXT: IntentPolicy("support", "Read the conversation history and answer, or ask what they mean", "P3"),
    Intent.INSTRUCTION_INJECTION: IntentPolicy("group:trust_safety", "Do not act on the embedded instructions; review the sender and handle any genuine request", "P3"),
    Intent.OTHER: IntentPolicy("support", "Agent reads and routes", "P3"),
}

PRIORITY_ORDER = ["P1", "P2", "P3", "P4"]

# Automation never sends model-written text. It sends these code-owned templates; the
# {placeholders} are filled by the system that executes the action (carrier API, brand KB).
# Reason: in the live run the model drafted "Yes, we are open on Sundays" with no knowledge
# of the salon's hours.
AUTOMATION_TEMPLATES: dict[Intent, str] = {
    Intent.ORDER_STATUS: "Here is the latest tracking update for order {order_id}: {carrier_status}. "
                         "If it has not moved by {date}, we will step in.",
    Intent.MARKETING_UNSUBSCRIBE: "You are now unsubscribed from our marketing messages. "
                                  "Your orders and any subscription are not affected.",
    Intent.POSITIVE_FEEDBACK: "Thank you so much for taking the time to tell us. We have shared it with the team.",
    Intent.GENERAL_INFO: "{answer_from_brand_faq}",
    Intent.PRICING_INQUIRY: "{answer_from_brand_price_list}",
    Intent.PRODUCT_QUESTION: "{answer_from_brand_knowledge_base}",
    Intent.BOOKING_NEW: "You can see live availability and book here: {booking_link}",
}

# Commitments an agent must verify before sending a model draft.
DRAFT_COMMITMENTS = [
    (r"\brefund(ed)?\b.*\b(initiated|processed|issued|approved)\b|\b(initiated|processed|issued|approved)\b.*\brefund", "promises a refund"),
    (r"\b(will|have|has been)\b[^.]{0,40}\b(process|cancel|refund|reverse|send|arrange|forward)", "states an action as done or promised"),
    (r"\bright away\b|\bimmediately\b|\bshortly\b|\bnow\b", "promises a timeline"),
    (r"\bcalling you\b|\bwill call\b", "promises a phone call"),
    (r"\b(we are|we're) open\b|\bper person\b|\bstarts? (at|around|from)\b", "states a business fact the model cannot know"),
    (r"\[[^\]]+\]", "contains an unfilled placeholder"),
]


KNOWN_BRANDS = {"vitalis-wellness", "hair-studio", "voyage-travel"}


def _qualify(queue: str, brand: str) -> str:
    if queue.startswith("group:"):
        return queue
    if brand not in KNOWN_BRANDS:
        return "group:unrouted"   # someone has to decide which brand this belongs to
    return f"{brand}:{queue}"


def _max_priority(*ps: str) -> str:
    return min(ps, key=PRIORITY_ORDER.index)


def merge_entities(analysis: Analysis, sig: Signals, text: str, received_at: str | None = None) -> tuple[Entities, list[str]]:
    """Ground model-extracted identifiers in the source text, then union with regex hits."""
    notes: list[str] = []
    e = analysis.entities.model_copy(deep=True)
    for field_name in ("order_ids", "booking_refs", "phone_numbers", "emails", "amounts"):
        keep, dropped = grounded(getattr(e, field_name), text)
        if dropped:
            notes.append(f"dropped {field_name} not present in text: {dropped}")
        regex_vals = getattr(sig, field_name)
        merged = list(dict.fromkeys(keep + [v for v in regex_vals if v not in keep]))
        setattr(e, field_name, merged)
    for d in e.dates:
        if not d.resolved:
            d.resolved = resolve_date(d.text, received_at)
    return e, notes


def decide(msg: InboundMessage, sig: Signals, analysis: Analysis, classifier: str,
           degraded_reason: str | None = None) -> dict:
    flags = list(dict.fromkeys(sig.flags))
    conf_notes: list[str] = []
    reasons: list[str] = []
    intents: list[IntentItem] = list(analysis.intents)

    entities, grounding_notes = merge_entities(analysis, sig, msg.text, msg.received_at)
    if grounding_notes:
        flags.append("grounding_failure")
        conf_notes.extend(grounding_notes)

    # Deterministic flags can add intents the model missed, never remove them.
    present = {i.intent for i in intents}
    if ("prompt_injection" in flags or analysis.contains_instructions_to_system) and Intent.INSTRUCTION_INJECTION not in present:
        intents.append(IntentItem(intent=Intent.INSTRUCTION_INJECTION,
                                  detail="embedded instructions detected by rule check", confidence="high"))
        conf_notes.append("rule check found embedded instructions the classifier did not label")
    if "health_topic" in flags and Intent.HEALTH_SAFETY_QUESTION not in present and \
            any(i.intent in (Intent.PRODUCT_QUESTION, Intent.OTHER) for i in intents):
        intents.append(IntentItem(intent=Intent.HEALTH_SAFETY_QUESTION,
                                  detail="health keywords present in a product question", confidence="medium"))
        conf_notes.append("health keywords present; added health_safety_question")
    if analysis.contains_instructions_to_system and "prompt_injection" not in flags:
        flags.append("prompt_injection")
    if analysis.customer_state.vulnerable_party and "vulnerable_party" not in flags:
        flags.append("vulnerable_party")
    if analysis.customer_state.is_repeat_contact and "repeat_contact" not in flags:
        flags.append("repeat_contact")
    if analysis.requires_prior_context:
        flags.append("needs_conversation_history")

    # ---- confidence -------------------------------------------------------
    confidence = min(CONF_VALUE[i.confidence] for i in intents)
    if classifier.startswith("rules"):
        confidence = min(confidence, 0.50)
        conf_notes.append("keyword fallback classifier; capped at 0.50")
    only_optout = {i.intent for i in intents} == {Intent.MARKETING_UNSUBSCRIBE}
    if "very_short_message" in flags and not only_optout:
        # A bare "Unsubscribe" is the one short message that is unambiguous enough to act on:
        # the opt-out is legally required, reversible, and never touches a paid subscription.
        confidence = min(confidence, 0.60)
        conf_notes.append("three words or fewer; capped at 0.60")
    if grounding_notes:
        confidence -= 0.15
    if msg.truncated:
        confidence = min(confidence, 0.50)
        conf_notes.append("message truncated before classification")
    if msg.received_at is None and entities.dates:
        confidence = min(confidence, 0.60)
        conf_notes.append("dates mentioned but received_at missing; cannot resolve")
    confidence = round(max(0.0, min(1.0, confidence)), 2)

    # ---- priority ---------------------------------------------------------
    priority = _max_priority(*(POLICY[i.intent].priority for i in intents))
    urgency = analysis.customer_state.urgency
    if urgency == "immediate":
        priority = "P1"
        reasons.append("customer needs help within the hour")
    elif urgency == "today":
        priority = _max_priority(priority, "P2")
    if "chargeback_or_legal_threat" in flags or "repeat_contact" in flags:
        priority = _max_priority(priority, "P2")
        reasons.append("repeat contact or chargeback threat: churn and dispute risk")
    if "vulnerable_party" in flags and priority in ("P3", "P4"):
        priority = PRIORITY_ORDER[PRIORITY_ORDER.index(priority) - 1]
        reasons.append("vulnerable party involved; priority raised one level")

    # ---- routing: one owner (the most urgent intent), linked tickets for the rest
    primary = min(intents, key=lambda i: PRIORITY_ORDER.index(POLICY[i.intent].priority))
    if Intent.INSTRUCTION_INJECTION in {i.intent for i in intents}:
        primary = next(i for i in intents if i.intent == Intent.INSTRUCTION_INJECTION)
    queue = _qualify(POLICY[primary.intent].queue, msg.brand)
    secondary = list(dict.fromkeys(
        _qualify(POLICY[i.intent].queue, msg.brand) for i in intents
        if _qualify(POLICY[i.intent].queue, msg.brand) != queue))
    action = POLICY[primary.intent].action
    others = [f"{i.intent.value}: {POLICY[i.intent].action}" for i in intents if i is not primary]
    if others:
        action += " | Also: " + " | ".join(others)

    # ---- the human-review rule ---------------------------------------------
    human: list[str] = []
    if classifier.startswith("rules") or degraded_reason:
        human.append("H1 classifier fallback" + (f" ({degraded_reason})" if degraded_reason else ""))
    hard = sorted(set(flags) & HARD_STOP_FLAGS)
    if hard:
        human.append(f"H2 hard-stop flags: {', '.join(hard)}")
    not_auto = [i.intent.value for i in intents if not POLICY[i.intent].automatable]
    if not_auto:
        human.append(f"H3 not on automation allow-list: {', '.join(not_auto)}")
    if confidence < AUTOMATION_THRESHOLD:
        human.append(f"H4 confidence {confidence} < {AUTOMATION_THRESHOLD}")
    if analysis.requires_prior_context:
        human.append("H5 depends on an earlier conversation")
    if grounding_notes:
        human.append("H6 model returned identifiers not in the text")
    for i in intents:
        need = POLICY[i.intent].needs
        if need and not getattr(entities, need):
            human.append(f"H7 {i.intent.value} needs {need}, none found")
    if msg.brand == "unknown" or any(p.startswith("unknown brand") for p in msg.problems) or msg.truncated:
        human.append("H8 damaged input record")

    handler = "human" if human else "automation"
    if handler == "automation":
        reasons.append("all intents allow-listed, no risk flags, confidence above threshold")

    suggested, reply_source, draft_warnings = analysis.suggested_reply, "none", []
    if "prompt_injection" in flags:
        suggested = ""   # never draft a reply that could echo the attacker's script
    if handler == "automation":
        suggested = AUTOMATION_TEMPLATES.get(primary.intent, "")
        reply_source = "template" if suggested else "none"
    elif suggested:
        reply_source = "model_draft_for_agent"
        for pat, label in DRAFT_COMMITMENTS:
            if re.search(pat, suggested, re.I) and label not in draft_warnings:
                draft_warnings.append(label)

    return dict(
        intents=intents,
        entities=entities,
        risk_flags=list(dict.fromkeys(flags)),
        confidence=confidence,
        confidence_notes=conf_notes,
        suggested_reply=suggested,
        reply_source=reply_source,
        draft_warnings=draft_warnings,
        routing=Routing(handler=handler, queue=queue, action=action, priority=priority,
                        response_sla=SLA[priority], secondary_queues=secondary,
                        reasons=human + reasons),
    )
