"""Data shapes for the triage service.

Three layers, three shapes:
  InboundMessage  - one normalised input record (whatever the file gave us)
  Analysis        - what the classifier (Claude or the rules fallback) thinks
  TriageResult    - the final, policy-checked decision we emit
"""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class Intent(str, Enum):
    ORDER_STATUS = "order_status"
    ORDER_PROBLEM = "order_problem"                  # damaged, wrong item, missing item
    REFUND_REQUEST = "refund_request"
    BILLING_DISPUTE = "billing_dispute"              # double charge, unknown charge
    SUBSCRIPTION_CANCEL = "subscription_cancel"
    MARKETING_UNSUBSCRIBE = "marketing_unsubscribe"
    PURCHASE_REQUEST = "purchase_request"            # wants to buy / pay / accept a quote
    PRICING_INQUIRY = "pricing_inquiry"
    PRODUCT_QUESTION = "product_question"            # incl. membership / service coverage
    HEALTH_SAFETY_QUESTION = "health_safety_question"
    BOOKING_NEW = "booking_new"
    BOOKING_CHANGE = "booking_change"
    BOOKING_CANCEL = "booking_cancel"
    TRAVEL_DISRUPTION = "travel_disruption"          # customer is mid-trip and stuck
    TRAVEL_PLANNING = "travel_planning"
    GENERAL_INFO = "general_info"                    # hours, location, policies
    B2B_WHOLESALE = "b2b_wholesale"
    JOB_APPLICATION = "job_application"
    VENDOR_INVOICE = "vendor_invoice"
    POSITIVE_FEEDBACK = "positive_feedback"
    COMPLAINT = "complaint"
    CONTACT_DETAILS_ONLY = "contact_details_only"
    UNCLEAR_NEEDS_CONTEXT = "unclear_needs_context"
    INSTRUCTION_INJECTION = "instruction_injection"  # message tries to command the system
    OTHER = "other"


Confidence = Literal["high", "medium", "low"]


class InboundMessage(BaseModel):
    """A record after normalisation. `problems` lists anything we had to repair."""
    id: str
    brand: str
    channel: str
    received_at: str | None
    text: str
    problems: list[str] = Field(default_factory=list)
    truncated: bool = False


class DateMention(BaseModel):
    text: str
    resolved: str = ""   # ISO date/datetime, "" when it cannot be resolved safely


class Entities(BaseModel):
    order_ids: list[str] = Field(default_factory=list)
    booking_refs: list[str] = Field(default_factory=list)
    phone_numbers: list[str] = Field(default_factory=list)
    emails: list[str] = Field(default_factory=list)
    amounts: list[str] = Field(default_factory=list)
    dates: list[DateMention] = Field(default_factory=list)
    people: list[str] = Field(default_factory=list)
    products_or_services: list[str] = Field(default_factory=list)
    other: list[str] = Field(default_factory=list)


class IntentItem(BaseModel):
    intent: Intent
    detail: str
    confidence: Confidence


class CustomerState(BaseModel):
    sentiment: Literal["positive", "neutral", "frustrated", "angry", "distressed"]
    urgency: Literal["immediate", "today", "normal", "none"]
    is_repeat_contact: bool
    mentions_attachment: bool
    vulnerable_party: bool


class Analysis(BaseModel):
    """Classifier output. The LLM fills this through a strict JSON schema."""
    language: str
    summary: str
    intents: list[IntentItem]
    entities: Entities
    customer_state: CustomerState
    requires_prior_context: bool
    contains_instructions_to_system: bool
    suggested_reply: str
    open_questions: list[str]


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cost_usd: float = 0.0


class Routing(BaseModel):
    handler: Literal["automation", "human"]
    queue: str
    action: str
    priority: Literal["P1", "P2", "P3", "P4"]
    response_sla: str
    secondary_queues: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class TraceStep(BaseModel):
    stage: str      # ingest | signals | classifier | policy | decision
    detail: str


class TriageResult(BaseModel):
    message_id: str
    brand: str
    channel: str
    received_at: str | None
    status: Literal["ok", "degraded", "error"]
    classifier: str
    language: str = "unknown"
    summary: str = ""
    intents: list[IntentItem] = Field(default_factory=list)
    entities: Entities = Field(default_factory=Entities)
    risk_flags: list[str] = Field(default_factory=list)
    confidence: float
    confidence_notes: list[str] = Field(default_factory=list)
    routing: Routing
    suggested_reply: str = ""
    open_questions: list[str] = Field(default_factory=list)
    input_problems: list[str] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    latency_ms: int = 0
    text: str = ""
    trace: list[TraceStep] = Field(default_factory=list)
