"""Keyword fallback classifier.

Used when no API key is configured, or when the model call fails, refuses, or
returns invalid output. It produces the same Analysis shape so downstream code
has one path. Every intent it produces is marked low confidence, so the policy
layer sends everything it classifies to a human. Its job is to put the message
in roughly the right queue, not to automate anything.
"""
from __future__ import annotations

import re

from .models import Analysis, CustomerState, DateMention, Entities, InboundMessage, Intent, IntentItem
from .signals import Signals

# (intent, patterns). Order matters only for readability; all matches are kept.
RULES: list[tuple[Intent, list[str]]] = [
    (Intent.INSTRUCTION_INJECTION, [r"ignore (all )?previous instructions", r"administrator mode", r"system notice"]),
    (Intent.TRAVEL_DISRUPTION, [r"\b(connection|flight)\b.*\bcancel", r"\bstranded\b", r"\bmissed (my )?connection\b"]),
    (Intent.HEALTH_SAFETY_QUESTION, [r"\bsafe to take\b", r"\bmedication\b", r"\bpregnan", r"\bside effects?\b"]),
    (Intent.BILLING_DISPUTE, [r"\bcharged twice\b", r"\bdouble charged?\b", r"\bcharged .* twice\b", r"\bunauthori[sz]ed charge\b"]),
    (Intent.REFUND_REQUEST, [r"\brefund\b", r"\bmoney back\b"]),
    (Intent.ORDER_PROBLEM, [r"\bwrong item\b", r"\bbroken\b", r"\bdamaged\b", r"\bgot the\b.*\binstead\b", r"\bmissing\b"]),
    (Intent.ORDER_STATUS, [r"\btracking\b", r"\bwhere is my order\b", r"\bnot (yet )?(arrived|delivered)\b", r"\bhasn'?t moved\b"]),
    (Intent.SUBSCRIPTION_CANCEL, [r"\bcancel (my )?subscription\b"]),
    (Intent.MARKETING_UNSUBSCRIBE, [r"^\s*unsubscribe\s*\.?\s*$", r"\bstop (sending|emails)\b"]),
    (Intent.VENDOR_INVOICE, [r"\binvoice\b.*\bpayment\b", r"\bprocess payment\b"]),
    (Intent.JOB_APPLICATION, [r"\bopenings?\b", r"\bresume\b", r"\bcv\b", r"\bvacanc"]),
    (Intent.B2B_WHOLESALE, [r"\bbulk\b", r"\bwholesale\b", r"\bdistribut", r"\bchain of\b"]),
    (Intent.BOOKING_CHANGE, [r"\b(change|move|shift|reschedule)\b.*\b(appointment|booking|return|flight|date)\b",
                             r"\bcan i (move|shift)\b"]),
    (Intent.BOOKING_CANCEL, [r"\bcancel\b.*\b(appointment|booking)\b", r"\bsomething came up\b", r"\bcan'?t make it\b"]),
    (Intent.BOOKING_NEW, [r"\bbook(ing)?\b.*\b(slot|appointment)\b", r"\bwants to book\b"]),
    (Intent.PURCHASE_REQUEST, [r"\bnext step\b", r"\bhow do we pay\b", r"\bi'?d (still )?like to order\b", r"\bwant to (buy|order)\b"]),
    (Intent.TRAVEL_PLANNING, [r"\bfares?\b", r"\bitinerar", r"\bwheelchair\b"]),
    (Intent.PRICING_INQUIRY, [r"\bhow much\b", r"\bprice\b", r"\brate\b", r"\bfee\b", r"\bfares?\b", r"\bcost\b"]),
    (Intent.GENERAL_INFO, [r"\bopen on\b", r"\bopening hours\b", r"\bopen (today|tomorrow)\b", r"\baddress\b"]),
    (Intent.PRODUCT_QUESTION, [r"\bdo you do\b", r"\bdoes the\b", r"\bmembership\b"]),
    (Intent.POSITIVE_FEEDBACK, [r"\bthank you\b", r"\bobsessed\b", r"\bamazing\b", r"\blove (it|my)\b", r"\bmuch better\b"]),
    (Intent.COMPLAINT, [r"\bnobody has responded\b", r"\bthird time\b", r"\bunacceptable\b", r"\bterrible\b"]),
]


def classify(msg: InboundMessage, sig: Signals) -> Analysis:
    text = msg.text
    intents: list[IntentItem] = []
    for intent, pats in RULES:
        if any(re.search(p, text, re.I) for p in pats):
            intents.append(IntentItem(intent=intent, detail=f"keyword match for {intent.value}", confidence="low"))

    if not intents:
        if sig.phone_numbers and sig.word_count <= 6:
            intents = [IntentItem(intent=Intent.CONTACT_DETAILS_ONLY, detail="message is only contact details", confidence="low")]
        elif sig.word_count <= 6:
            intents = [IntentItem(intent=Intent.UNCLEAR_NEEDS_CONTEXT, detail="short message with no recognisable request", confidence="low")]
        else:
            intents = [IntentItem(intent=Intent.OTHER, detail="no rule matched", confidence="low")]

    urgency = "immediate" if "urgent_language" in sig.flags and any(
        i.intent == Intent.TRAVEL_DISRUPTION for i in intents) else "normal"
    return Analysis(
        language=sig.language_hint,
        summary="(rules fallback) " + text[:160],
        intents=intents,
        entities=Entities(order_ids=sig.order_ids, booking_refs=sig.booking_refs,
                          phone_numbers=sig.phone_numbers, emails=sig.emails, amounts=sig.amounts,
                          dates=[DateMention(text=d) for d in re.findall(
                              r"\b(?:today|tomorrow|tonight|(?:mon|tues|wednes|thurs|fri|satur|sun)day|"
                              r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]* \d{1,2}|"
                              r"\d{1,2}(?:st|nd|rd|th))\b", text, re.I)]),
        customer_state=CustomerState(
            sentiment="neutral", urgency=urgency,
            is_repeat_contact="repeat_contact" in sig.flags,
            mentions_attachment="attachment_referenced_but_not_received" in sig.flags,
            vulnerable_party="vulnerable_party" in sig.flags,
        ),
        requires_prior_context=any(i.intent in (Intent.UNCLEAR_NEEDS_CONTEXT, Intent.CONTACT_DETAILS_ONLY) for i in intents),
        contains_instructions_to_system="prompt_injection" in sig.flags,
        suggested_reply="",
        open_questions=["Classified by keyword fallback; a person should read the full message."],
    )
