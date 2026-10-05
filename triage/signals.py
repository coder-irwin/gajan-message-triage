"""Deterministic signals that run on every message, before and independent of the LLM.

Two jobs:
1. Extract identifiers with regex (order ids, refs, phones, amounts). These are
   also used to *ground* the model: an ID the model returns that does not appear
   in the source text is discarded.
2. Raise risk flags. Flags can only add caution - the model can never clear one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

ORDER_ID = re.compile(r"\b[A-Z]{2,4}-\d{4,}\b")
BOOKING_REF = re.compile(r"\b(?:booking\s*ref(?:erence)?|ref|pnr|confirmation)\s*(?:no\.?|number|#|:)?\s*([A-Z0-9]{5,10})\b", re.I)
PHONE = re.compile(r"(?<![\w-])(\+?\d[\d\s().-]{7,}\d)(?![\w-])")
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
AMOUNT = re.compile(r"(?:INR|Rs\.?|₹|CAD|C\$|USD|US\$|\$|AED|EUR|€)\s?\d[\d,]*(?:\.\d+)?", re.I)

INJECTION = [
    r"ignore (all |any )?(the )?(previous|prior|above) (instructions|prompts?)",
    r"\bsystem (notice|prompt|override)\b",
    r"\b(administrator|admin|developer|god) mode\b",
    r"\byou are now\b",
    r"do not (escalate|forward|involve)",
    r"\breply (with )?['\"]?confirmed\b",
    r"\b(approve|issue|process) (a )?(full )?refund\b.*\b(original payment|immediately)\b",
]
HEALTH = [
    r"\bsafe to (take|use|consume)\b", r"\bmedication\b", r"\bmedicine\b", r"\bdoctor\b",
    r"\bpregnan", r"\bbreastfeed", r"\bside[- ]effects?\b", r"\bdosage\b", r"\bdose\b",
    r"\ballerg", r"\bblood pressure\b", r"\bdiabet", r"\binteract(ion)? with\b",
]
CHARGEBACK_OR_LEGAL = [
    r"\b(raise|raising|report|reporting|complain(ing)?) (it |this )?(with|to) (my )?bank\b",
    r"\bchargeback\b", r"\bconsumer (court|forum)\b", r"\blawyer\b", r"\blegal action\b",
]
REPEAT_CONTACT = [
    r"\b(second|third|fourth|fifth|\d+(st|nd|rd|th)) time\b", r"\bnobody (has )?(responded|replied|got back)\b",
    r"\bno (one|response|reply)\b.*\b(yet|still)\b", r"\bstill (waiting|no)\b",
]
URGENT = [r"\burgent\b", r"\bimmediately\b", r"\basap\b", r"\bemergency\b", r"\bstranded\b", r"\bright now\b"]
VULNERABLE = [r"\belderly\b", r"\bwheelchair\b", r"\bover (6|7|8)\d\b", r"\bdisab", r"\bpregnan", r"\binfant\b", r"\bbaby\b"]
THIRD_PARTY_PAYMENT = [r"\binvoice\b", r"\bprocess (the )?payment\b", r"\bbank details\b", r"\bremit", r"\bwire\b"]
ATTACHMENT = [r"\battached\b", r"\battachment\b", r"\bsee (the )?pdf\b", r"\bin the pdf\b", r"\bresume\b"]
HINGLISH = re.compile(r"\b(meri|mera|mujhe|baje|thi|tha|aur|bata|dena|hai|kya|nahi|kal|ka|ki|ke|bhi)\b", re.I)


def _any(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, re.I) for p in patterns)


@dataclass
class Signals:
    order_ids: list[str] = field(default_factory=list)
    booking_refs: list[str] = field(default_factory=list)
    phone_numbers: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    amounts: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    word_count: int = 0
    language_hint: str = "unknown"


def extract(text: str) -> Signals:
    s = Signals()
    s.word_count = len(re.findall(r"\w+", text))
    if not text:
        s.flags.append("empty_message")
        return s

    s.order_ids = sorted(set(ORDER_ID.findall(text)))
    s.booking_refs = sorted({m.upper() for m in BOOKING_REF.findall(text) if any(c.isdigit() for c in m)})
    s.emails = sorted(set(EMAIL.findall(text)))
    s.phone_numbers = sorted({p.strip() for p in PHONE.findall(text)
                              if len(re.sub(r"\D", "", p)) >= 9 and p.strip() not in s.order_ids})
    s.amounts = sorted(set(m.strip() for m in AMOUNT.findall(text)))

    if _any(INJECTION, text):
        s.flags.append("prompt_injection")
    if _any(HEALTH, text):
        s.flags.append("health_topic")
    if _any(CHARGEBACK_OR_LEGAL, text):
        s.flags.append("chargeback_or_legal_threat")
    if _any(REPEAT_CONTACT, text):
        s.flags.append("repeat_contact")
    if _any(URGENT, text) or (text.isupper() and s.word_count >= 3):
        s.flags.append("urgent_language")
    if _any(VULNERABLE, text):
        s.flags.append("vulnerable_party")
    if _any(THIRD_PARTY_PAYMENT, text) and _any([r"\bbank\b", r"\bpayment\b", r"\binvoice\b"], text):
        s.flags.append("payment_instruction_from_sender")
    if _any(ATTACHMENT, text):
        s.flags.append("attachment_referenced_but_not_received")
    if s.phone_numbers or s.emails:
        s.flags.append("contains_pii")
    if s.word_count <= 3:
        s.flags.append("very_short_message")

    hinglish_hits = len(HINGLISH.findall(text))
    s.language_hint = "hi-en (code-mixed)" if hinglish_hits >= 3 else "en"
    return s


def grounded(values: list[str], text: str) -> tuple[list[str], list[str]]:
    """Split model-extracted identifiers into (present in text, not present)."""
    norm_text = re.sub(r"[\s\-().]", "", text).lower()
    keep, dropped = [], []
    for v in values:
        if re.sub(r"[\s\-().]", "", v).lower() in norm_text:
            keep.append(v)
        else:
            dropped.append(v)
    return keep, dropped


_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def resolve_date(mention: str, received_at: str | None) -> str:
    """Resolve a simple relative or partial date against received_at. Returns ISO date or "".

    Deliberately conservative: weekdays resolve to the *next* occurrence (a customer
    talking about "Saturday" on a Monday almost always means this coming Saturday),
    bare day-of-month to the most recent past or upcoming match is ambiguous, so it
    resolves to the current month only if that day has already passed (order dates)."""
    from datetime import datetime, timedelta
    if not received_at:
        return ""
    try:
        base = datetime.fromisoformat(received_at).date()
    except ValueError:
        return ""
    m = mention.strip().lower()
    if m == "today":
        return base.isoformat()
    if m == "tomorrow":
        return (base + timedelta(days=1)).isoformat()
    if m in _WEEKDAYS:
        delta = (_WEEKDAYS.index(m) - base.weekday()) % 7 or 7
        return (base + timedelta(days=delta)).isoformat()
    md = re.fullmatch(r"([a-z]{3})[a-z]* (\d{1,2})", m)
    if md and md.group(1) in _MONTHS:
        try:
            d = base.replace(month=_MONTHS.index(md.group(1)) + 1, day=int(md.group(2)))
        except ValueError:
            return ""
        if d < base - timedelta(days=60):   # "Jan 5" said in December means next year
            d = d.replace(year=d.year + 1)
        return d.isoformat()
    dm = re.fullmatch(r"(\d{1,2})(st|nd|rd|th)", m)
    if dm:
        try:
            d = base.replace(day=int(dm.group(1)))
        except ValueError:
            return ""
        return d.isoformat() if d <= base else ""
    return ""
