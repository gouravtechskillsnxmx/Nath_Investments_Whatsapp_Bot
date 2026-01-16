import os
import re
import uuid
from datetime import datetime, date
from typing import Optional

import requests
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import (
    create_engine,
    Column,
    String,
    Date,
    DateTime,
    Boolean,
    Numeric,
    ForeignKey,
    Text,
    select,
    desc,
    asc,
    func,
)
from sqlalchemy.orm import sessionmaker, DeclarativeBase, relationship, Session
from fastapi.responses import HTMLResponse
from fastapi import Request, Depends
from sqlalchemy.orm import Session
from sqlalchemy import select, desc
import re
from datetime import datetime, timezone, timedelta
from io import BytesIO

import openpyxl
from fastapi import UploadFile, File, Form

# =========================================================
# CONFIG
# =========================================================

# If DATABASE_URL not set, uses local SQLite file.
# For Render Postgres, set DATABASE_URL = postgres://...
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./insure.db")

connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

app = FastAPI(title="Policy Lookup + Dashboard + Team Inbox + WhatsApp Delivery (Single File)")

POLICY_RE = re.compile(r"^[A-Za-z0-9\-\/]+$")  # allow common formats
PHONE_RE = re.compile(r"^\+?[0-9]{10,15}$")

# WhatsApp Cloud API (Meta Graph)
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_API_VERSION = os.getenv("WHATSAPP_API_VERSION", "v17.0")

# OpenAI (optional auto-replies)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
OPENAI_TIMEOUT = float(os.getenv("OPENAI_TIMEOUT", "25"))



class Base(DeclarativeBase):
    pass


IST = timezone(timedelta(hours=5, minutes=30))

def now_utc() -> datetime:
    # Return IST time instead of UTC (name unchanged to avoid breaking anything)
    return datetime.now(IST)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def send_whatsapp_text(to_number: str, body: str) -> dict:
    """
    Sends WhatsApp text message using Meta WhatsApp Cloud API.
    Returns JSON response from Graph API.
    """
    if not (WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID):
        raise RuntimeError("Missing WhatsApp env vars: WHATSAPP_ACCESS_TOKEN / WHATSAPP_PHONE_NUMBER_ID")

    url = f"https://graph.facebook.com/{WHATSAPP_API_VERSION}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": body},
    }

    r = requests.post(url, headers=headers, json=payload, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"WhatsApp send failed: {r.status_code} {r.text}")
    return r.json()


def openai_generate_reply(*, customer_phone: str, customer_name: str | None, user_text: str, policy_number: str | None, db: Session) -> str:
    """Generate a safe WhatsApp reply using OpenAI. Never invent policy facts; use DB lookup when possible."""

    # Deterministic greeting + menu (ensures menu appears even if the model ignores instructions)
    txt = (user_text or "").strip().lower()

    # Option routing: "2" = policy details (use extracted/known policy number)
    if txt in ("2", "option 2", "press 2", "policy details", "policy detail"):
        # Try to reuse known policy number (from extraction or last open conversation)
        pn = (policy_number or "").strip() if policy_number else ""
        if not pn:
            try:
                conv = db.execute(
                    select(InboxConversation)
                    .where(
                        InboxConversation.channel == "WHATSAPP",
                        InboxConversation.customer_phone == customer_phone,
                        InboxConversation.status != "CLOSED",
                    )
                    .order_by(desc(InboxConversation.last_message_at))
                    .limit(1)
                ).scalars().first()
                if conv and conv.policy_number:
                    pn = str(conv.policy_number).strip()
            except Exception:
                pn = ""

        if not pn:
            return "Sure. Please share your policy number (6–20 digits) to check your policy details."

        # Lookup policy
        pol = db.execute(select(Policy).where(Policy.policy_number == pn)).scalars().first()
        if not pol:
            return f"Sorry, no policy found for this policy number: {pn}. Please re-check and send the correct policy number."

        # Start date (best-effort: policy.start_date if exists else created_at)
        start_date = None
        try:
            start_date = getattr(pol, "start_date", None) or getattr(pol, "policy_start_date", None)
        except Exception:
            start_date = None
        if not start_date:
            try:
                start_date = getattr(pol, "created_at", None)
            except Exception:
                start_date = None

        # Total premium paid (sum of successful/paid payments)
        total_paid = 0.0
        try:
            paid_rows = db.execute(
                select(Payment).where(Payment.policy_id == pol.id).order_by(desc(Payment.paid_on), desc(Payment.created_at))
            ).scalars().all()
            for p in paid_rows:
                st = (getattr(p, "status", "") or "").upper()
                if st in ("PAID", "SUCCESS", "COMPLETED") or st == "":
                    try:
                        total_paid += float(getattr(p, "amount", 0) or 0)
                    except Exception:
                        pass
        except Exception:
            total_paid = 0.0

        # Remaining premium: sum of unpaid schedule amounts (fallback: count unpaid * premium_amount)
        remaining = None
        next_due = None
        try:
            unpaid = db.execute(
                select(PremiumSchedule)
                .where(PremiumSchedule.policy_id == pol.id, PremiumSchedule.is_paid == False)  # noqa: E712
                .order_by(asc(PremiumSchedule.due_date))
            ).scalars().all()
            if unpaid:
                try:
                    remaining = sum(float(getattr(u, "amount", 0) or 0) for u in unpaid)
                except Exception:
                    remaining = None
                next_due = unpaid[0]
        except Exception:
            remaining = None
            next_due = None

        if remaining is None:
            try:
                prem_amt = float(getattr(pol, "premium_amount", 0) or 0)
                if prem_amt and next_due is not None:
                    # if we have unpaid rows but amounts missing, approximate
                    remaining = prem_amt * len(unpaid)
            except Exception:
                pass

        maturity_date = getattr(pol, "maturity_date", None)
        status = getattr(pol, "status", None) or "ACTIVE"

        def _fmt(d):
            try:
                if hasattr(d, "date"):
                    # datetime
                    d2 = d.date() if hasattr(d, "hour") else d
                    return d2.strftime("%d-%b-%Y")
                return d.strftime("%d-%b-%Y")
            except Exception:
                return str(d) if d else "Not available"

        msg = [
            f"✅ Policy *{pol.policy_number}* details:",
            f"• Status: *{status}*",
            f"• Start date: *{_fmt(start_date)}*",
            f"• Total premium paid: *₹{total_paid:,.2f}*",
        ]
        if remaining is not None:
            msg.append(f"• Remaining premium: *₹{float(remaining):,.2f}*")
        else:
            msg.append("• Remaining premium: *Not available*")
        msg.append(f"• Maturity date: *{_fmt(maturity_date)}*")
        msg.append("Reply *3* to talk to our human agent.")
        return "\n".join(msg)


    # Deterministic mutual fund help (avoid incorrectly asking for policy number)
    if re.search(r"\b(mutual\s*funds?|mf|sip|lumpsum|nav|folio|kyc|redemption|switch|stp|swp)\b", txt, flags=re.I):
        return (
            "Mutual Funds (MF) are pooled investments managed by professional fund managers.\n"
            "You can invest via SIP (monthly) or Lumpsum (one-time). Returns depend on market performance (no guarantees).\n\n"
            "If you tell me your goal (saving/tax/wealth), time horizon, and whether you prefer SIP or lumpsum, I can guide you on the process and documents (KYC)."
        )

    # Deterministic menu routing for option selections (1/2/3)
    # Normalize common inputs like "1", "1.", "press 1", "option 1"
    opt = None
    m_opt = re.search(r"\b([123])\b", txt)
    if m_opt and txt in {m_opt.group(1), f"{m_opt.group(1)}.", f"press {m_opt.group(1)}", f"option {m_opt.group(1)}"}:
        opt = m_opt.group(1)
    # Also accept single-character inputs with whitespace
    if txt in {"1", "2", "3"}:
        opt = txt

    if opt == "1":
        name = (customer_name or "").strip()
        prefix = f"Hi {name}, " if name else "Hi, "
        return (
            f"{prefix}Nath Investment is a financial firm offering services in LIC and Mutual Funds.\n\n"
            "✅ LIC Services: New policy guidance, premium due reminders, policy status help, revival support, maturity/claim assistance.\n"
            "✅ Mutual Funds: SIP & lumpsum guidance, KYC support, portfolio review and general fund selection guidance (no guaranteed returns).\n\n"
            "If you want, tell me what you’re looking for (LIC or Mutual Funds) and I’ll guide you."
        )

    if opt == "2":
        # If policy number wasn't extracted earlier, ask for it.
        # (Keep policy_number variable usage unchanged by setting it only for this reply path.)
        if not policy_number:
            return "Sure. Please share your policy number (6–20 digits) to check your policy details."
        # If policy_number exists, let the existing DB-first logic handle it.
        # No return here; continue below into the existing policy lookup logic.

    if opt == "3":
        # Human handoff phrase; existing handoff logic in webhook can also detect keywords.
        return "Sure — connecting you to a human advisor now. Please wait, our team will reply shortly."

    if txt in {"hi", "hello", "hey", "hii", "hiii", "good morning", "good afternoon", "good evening", "namaste"}:
        name = (customer_name or "").strip()
        prefix = f"Hi {name}, " if name else "Hi, "
        return (
            f"👋 {prefix}welcome to *Nath Investment*! I am *Shashinath Thakur*. How can I help you today?\n\n"
            "Please choose an option 👇\n\n"
            "🟢 1️⃣  *About Nath Investments & our services*\n"
            "🔵 2️⃣  *Know your policy details*\n"
            "🟠 3️⃣  *Talk to our human agent*"
        )
    # If no key, skip auto-reply
    if not OPENAI_API_KEY:
        return ""

    # If message contains a policy number, try a DB lookup and craft a deterministic reply (no hallucinations)
    if policy_number:
        policy = db.execute(select(Policy).where(Policy.policy_number == policy_number)).scalars().first()
        if policy:
            # Reuse the same factual composition used by /policy/lookup
            last_payment = db.execute(
                select(Payment)
                .where(Payment.policy_id == policy.id)
                .order_by(desc(Payment.paid_on), desc(Payment.created_at))
                .limit(1)
            ).scalars().first()

            next_due = db.execute(
                select(PremiumSchedule)
                .where(PremiumSchedule.policy_id == policy.id, PremiumSchedule.is_paid == False)  # noqa: E712
                .order_by(asc(PremiumSchedule.due_date))
                .limit(1)
            ).scalars().first()

            next_due_date = next_due.due_date if next_due else policy.next_premium_due_date

            parts = [f"Policy {policy.policy_number} status: {policy.status}."]

            if next_due_date:
                parts.append(f"Next premium due date: {next_due_date.isoformat()}.")
            if policy.premium_amount is not None:
                parts.append(f"Premium amount: ₹{float(policy.premium_amount):,.2f}.")
            if policy.maturity_date:
                parts.append(f"Maturity date: {policy.maturity_date.isoformat()}.")
            if policy.maturity_amount_expected is not None:
                parts.append(f"Expected maturity amount: ₹{float(policy.maturity_amount_expected):,.2f}.")
            if last_payment:
                parts.append(
                    f"Last payment: {last_payment.paid_on.isoformat()} ({last_payment.status}, ₹{float(last_payment.amount):,.2f})."
                )

            # Add a short closing line
            parts.append("If you want, share your registered phone number for verification.")
            return " ".join(parts)

        # If a policy number was provided but not found, reply clearly (no hallucinations)
        return f"Sorry, no policy found for this policy number: {policy_number}. Please re-check and send the correct policy number." 

    # Otherwise: OpenAI for general guidance + clarification questions (no personalized facts)
    system = (
        "You are the official WhatsApp assistant for Nath Investments, a financial firm offering services in LIC and mutual funds. More details may be provided later (e.g., via a PPT). "
        "Be concise and WhatsApp-friendly (1-4 short lines). "
        "Answer questions about Nath Investments: services, onboarding, documents needed, office hours, process, fees in general terms. "
        "Never fabricate policy status, due dates, maturity amounts, NAVs, returns, guarantees, or any personalized facts. "
        "If the user asks for policy-specific info and policy number is missing, ask for the policy number. "
        "If the user asks for investment advice, give general educational guidance and suggest speaking to a human advisor; no guarantees. "
        "If the user asks to talk to a human/agent, confirm handoff and tell them an agent will reply shortly."
    )

    # Provide a little context to reduce hallucinations
    context = f"Customer phone: {customer_phone}. Customer name: {customer_name or 'Unknown'}. Extracted policy number: {policy_number or 'None'}."  # not sensitive beyond what WhatsApp already provides

    payload = {
        "model": OPENAI_MODEL,
        "input": [
            {"role": "system", "content": system},
            {"role": "developer", "content": "If the user says 'hi' or greetings, greet back and ask how you can help."},
            {"role": "user", "content": f"{context}\n\nUser message: {user_text}"},
        ],
        "max_output_tokens": 220,
    }

    try:
        r = requests.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json=payload,
            timeout=OPENAI_TIMEOUT,
        )
        if r.status_code >= 400:
            return ""
        data = r.json()
        # Responses API returns output_text in some SDKs; with raw HTTP, parse safely
        out_text = ""
        if isinstance(data, dict):
            # try common shapes
            if "output_text" in data and isinstance(data["output_text"], str):
                out_text = data["output_text"]
            else:
                # traverse output -> content -> text
                for item in data.get("output", []) or []:
                    for c in item.get("content", []) or []:
                        if c.get("type") == "output_text" and isinstance(c.get("text"), str):
                            out_text += c.get("text")
                        elif c.get("type") == "text" and isinstance(c.get("text"), str):
                            out_text += c.get("text")
        out_text = (out_text or "").strip()
        return out_text[:1200]
    except Exception:
        return ""


# =========================================================
# DB MODELS

# =========================================================

class Customer(Base):
    __tablename__ = "customers"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    full_name = Column(Text, nullable=False)
    phone_e164 = Column(Text, unique=True, index=True, nullable=True)
    email = Column(Text, nullable=True)
    dob = Column(Date, nullable=True)
    pan_last4 = Column(String(4), nullable=True)

    created_at = Column(DateTime, default=now_utc, nullable=False)
    updated_at = Column(DateTime, default=now_utc, nullable=False)

    policies = relationship("Policy", back_populates="customer")


class Policy(Base):
    __tablename__ = "policies"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    carrier = Column(Text, nullable=False, default="LIC")
    policy_number = Column(Text, unique=True, nullable=False, index=True)

    customer_id = Column(String, ForeignKey("customers.id"), nullable=False)

    plan_name = Column(Text, nullable=True)
    plan_code = Column(Text, nullable=True)
    status = Column(Text, nullable=False, default="ACTIVE")  # ACTIVE/LAPSED/MATURED/etc.

    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=True)
    maturity_date = Column(Date, nullable=True)

    sum_assured = Column(Numeric(14, 2), nullable=True)
    maturity_amount_expected = Column(Numeric(14, 2), nullable=True)

    premium_amount = Column(Numeric(14, 2), nullable=True)
    premium_frequency = Column(Text, nullable=False, default="YEARLY")  # MONTHLY/...
    next_premium_due_date = Column(Date, nullable=True)
    grace_period_days = Column(String, nullable=True, default="30")

    nominee_name = Column(Text, nullable=True)
    nominee_relation = Column(Text, nullable=True)

    created_at = Column(DateTime, default=now_utc, nullable=False)
    updated_at = Column(DateTime, default=now_utc, nullable=False)

    customer = relationship("Customer", back_populates="policies")
    payments = relationship("Payment", back_populates="policy", cascade="all, delete-orphan")
    schedule = relationship("PremiumSchedule", back_populates="policy", cascade="all, delete-orphan")


class PremiumSchedule(Base):
    __tablename__ = "premium_schedule"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    policy_id = Column(String, ForeignKey("policies.id"), nullable=False, index=True)

    due_date = Column(Date, nullable=False)
    amount = Column(Numeric(14, 2), nullable=False)
    is_paid = Column(Boolean, nullable=False, default=False)
    paid_on = Column(Date, nullable=True)

    created_at = Column(DateTime, default=now_utc, nullable=False)

    policy = relationship("Policy", back_populates="schedule")


class Payment(Base):
    __tablename__ = "payments"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    policy_id = Column(String, ForeignKey("policies.id"), nullable=False, index=True)

    paid_on = Column(Date, nullable=False)
    amount = Column(Numeric(14, 2), nullable=False)
    status = Column(Text, nullable=False, default="SUCCESS")  # SUCCESS/FAILED/PENDING/REVERSED

    reference_id = Column(Text, nullable=True)
    method = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)

    created_at = Column(DateTime, default=now_utc, nullable=False)

    policy = relationship("Policy", back_populates="payments")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    actor_user_id = Column(Text, nullable=True)
    channel = Column(Text, nullable=False, default="WHATSAPP")  # WHATSAPP/WEB/VOICE
    request_id = Column(Text, nullable=True)
    action = Column(Text, nullable=False, default="POLICY_LOOKUP")
    policy_number = Column(Text, nullable=True)
    customer_phone = Column(Text, nullable=True)
    success = Column(Boolean, nullable=False, default=False)
    reason = Column(Text, nullable=True)
    created_at = Column(DateTime, default=now_utc, nullable=False)


# -------- Team Inbox: users, conversations, messages --------

class TeamUser(Base):
    __tablename__ = "team_users"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    role = Column(Text, nullable=False, default="agent")  # admin/agent
    full_name = Column(Text, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=now_utc, nullable=False)


class InboxConversation(Base):
    __tablename__ = "inbox_conversations"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    channel = Column(Text, nullable=False, default="WHATSAPP")

    customer_phone = Column(Text, nullable=False, index=True)
    customer_name = Column(Text, nullable=True)

    policy_number = Column(Text, nullable=True, index=True)

    status = Column(Text, nullable=False, default="OPEN")  # OPEN/PENDING/CLOSED
    priority = Column(Text, nullable=False, default="NORMAL")  # LOW/NORMAL/HIGH

    assigned_to_user_id = Column(String, ForeignKey("team_users.id"), nullable=True)

    last_message_at = Column(DateTime, default=now_utc, nullable=False, index=True)
    created_at = Column(DateTime, default=now_utc, nullable=False)
    updated_at = Column(DateTime, default=now_utc, nullable=False)

    assigned_to = relationship("TeamUser")
    messages = relationship("InboxMessage", back_populates="conversation", cascade="all, delete-orphan")


class InboxMessage(Base):
    __tablename__ = "inbox_messages"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    conversation_id = Column(String, ForeignKey("inbox_conversations.id"), nullable=False, index=True)

    direction = Column(Text, nullable=False, default="IN")  # IN/OUT/NOTE
    body = Column(Text, nullable=False)

    actor_user_id = Column(String, ForeignKey("team_users.id"), nullable=True)  # for OUT/NOTE
    created_at = Column(DateTime, default=now_utc, nullable=False, index=True)

    conversation = relationship("InboxConversation", back_populates="messages")
    actor = relationship("TeamUser")


# =========================================================
# Pydantic Schemas
# =========================================================

class PolicyLookupRequest(BaseModel):
    policy_number: str = Field(..., min_length=4, max_length=40)
    customer_phone_e164: Optional[str] = Field(default=None, max_length=20)
    channel: str = Field(default="WHATSAPP")
    request_id: Optional[str] = None


class PolicyLookupResponse(BaseModel):
    found: bool
    policy_number: Optional[str] = None
    carrier: Optional[str] = None
    status: Optional[str] = None
    plan_name: Optional[str] = None

    premium_amount: Optional[float] = None
    next_premium_due_date: Optional[date] = None
    grace_period_days: Optional[int] = None

    maturity_date: Optional[date] = None
    maturity_amount_expected: Optional[float] = None
    sum_assured: Optional[float] = None

    last_payment_date: Optional[date] = None
    last_payment_amount: Optional[float] = None
    last_payment_status: Optional[str] = None

    message: str


class InboxSendRequest(BaseModel):
    actor_user_id: Optional[str] = None
    direction: str = Field(default="OUT")  # OUT or NOTE
    body: str = Field(..., min_length=1, max_length=4000)


class InboxAssignRequest(BaseModel):
    assigned_to_user_id: Optional[str] = None  # None = unassign
    status: Optional[str] = None  # OPEN/PENDING/CLOSED
    priority: Optional[str] = None  # LOW/NORMAL/HIGH


# =========================================================
# Helpers
# =========================================================

def audit(
    db: Session,
    *,
    channel: str,
    request_id: Optional[str],
    action: str,
    policy_number: Optional[str],
    customer_phone: Optional[str],
    success: bool,
    reason: Optional[str],
    actor_user_id: Optional[str] = None,
):
    db.add(
        AuditLog(
            channel=channel,
            request_id=request_id,
            action=action,
            policy_number=policy_number,
            customer_phone=customer_phone,
            success=success,
            reason=reason,
            actor_user_id=actor_user_id,
        )
    )
    db.commit()


def money(v):
    return float(v) if v is not None else None


def seed_team_if_empty(db: Session):
    count = db.execute(select(func.count(TeamUser.id))).scalar_one()
    if count and int(count) > 0:
        return
    admin = TeamUser(role="admin", full_name="Admin", is_active=True)
    a1 = TeamUser(role="agent", full_name="Agent 1", is_active=True)
    a2 = TeamUser(role="agent", full_name="Agent 2", is_active=True)
    db.add_all([admin, a1, a2])
    db.commit()


# =========================================================
# Startup: create tables + seed team
# =========================================================

@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        seed_team_if_empty(db)


# =========================================================
# WhatsApp Webhook (Meta Cloud API)
# =========================================================

@app.get("/webhook")
def whatsapp_verify(request: Request):
    """
    Meta webhook verification handshake:
    GET /webhook?hub.mode=subscribe&hub.verify_token=...&hub.challenge=...
    """
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode == "subscribe" and token and token == WHATSAPP_VERIFY_TOKEN:
        return HTMLResponse(content=str(challenge), status_code=200)

    return HTMLResponse(content="Verification failed", status_code=403)


@app.post("/webhook")
async def whatsapp_incoming(request: Request, db: Session = Depends(get_db)):
    """
    Receives WhatsApp webhook payloads and ingests messages into Team Inbox.
    Auto-reply (OpenAI) is enabled below; everything else stays the same.
    """
    print(">>> META POST /webhook HIT <<<")

    data = await request.json()

    handled = 0

    try:
        # WhatsApp payloads can contain multiple entries/changes/messages
        for _entry in (data.get("entry") or []):
            for _change in (_entry.get("changes") or []):
                entry = (_change.get("value") or {})  # keep variable name "entry"

                # Ignore status callbacks; only handle inbound messages
                if "messages" not in entry:
                    continue

                for msg in (entry.get("messages") or []):  # keep variable name "msg"
                    from_number = msg.get("from")  # often without "+"
                    if not from_number:
                        continue

                    if msg.get("type") == "text":
                        text_body = msg.get("text", {}).get("body", "")
                    else:
                        text_body = f"[{msg.get('type', 'unknown')} message received]"

                    # Normalize to E.164-like: add + if missing
                    customer_phone = from_number if from_number.startswith("+") else f"+{from_number}"

                    # Try to pull name if provided in contacts
                    customer_name = None
                    if "contacts" in entry and entry["contacts"]:
                        customer_name = entry["contacts"][0].get("profile", {}).get("name")

                    # OPTIONAL: extract policy number from message text (basic)
                    policy_number = None
                    m = re.search(r"\b(\d{6,20})\b", text_body or "")
                    if m:
                        policy_number = m.group(1)

                    # find existing conversation by channel + phone that is not CLOSED (prefer open)
                    conv = db.execute(
                        select(InboxConversation)
                        .where(
                            InboxConversation.channel == "WHATSAPP",
                            InboxConversation.customer_phone == customer_phone,
                            InboxConversation.status != "CLOSED",
                        )
                        .order_by(desc(InboxConversation.last_message_at))
                        .limit(1)
                    ).scalars().first()

                    if not conv:
                        conv = InboxConversation(
                            channel="WHATSAPP",
                            customer_phone=customer_phone,
                            customer_name=customer_name,
                            policy_number=policy_number,
                            status="OPEN",
                            priority="NORMAL",
                            last_message_at=now_utc(),
                            updated_at=now_utc(),
                        )
                        db.add(conv)
                        db.commit()
                        db.refresh(conv)

                    # update conv fields if new info appears
                    if customer_name and not conv.customer_name:
                        conv.customer_name = customer_name
                    if policy_number and not conv.policy_number:
                        conv.policy_number = policy_number

                    conv.last_message_at = now_utc()
                    conv.updated_at = now_utc()

                    db.add(
                        InboxMessage(
                            conversation_id=conv.id,
                            direction="IN",
                            body=text_body or "[empty]",
                            actor_user_id=None,
                            created_at=now_utc(),
                        )
                    )

                    audit(
                        db,
                        channel="WHATSAPP",
                        request_id=None,
                        action="INBOX_INGEST",
                        policy_number=policy_number,
                        customer_phone=customer_phone,
                        success=True,
                        reason=None,
                    )

                    db.commit()
                    handled += 1

                    # Auto-reply (OpenAI) - keep everything else the same
                    try:
                        if (msg.get("type") == "text") and (text_body or "").strip():
                            user_clean = (text_body or "").strip()

                            # Human handoff: if user asks for an agent/human, mark conversation PENDING and notify
                            if re.search(r"(agent|human|representative|advisor|support|call me|callback|talk to|speak to)", user_clean, flags=re.I):
                                try:
                                    conv.status = "PENDING"
                                    conv.updated_at = now_utc()
                                    conv.last_message_at = now_utc()
                                    handoff_msg = "Sure — I’m connecting you to a human advisor at Nath Investments. An agent will reply shortly."

                                    # Deliver to WhatsApp
                                    send_whatsapp_text(customer_phone, handoff_msg)

                                    # Store OUT message in the same conversation thread
                                    db.add(
                                        InboxMessage(
                                            conversation_id=conv.id,
                                            direction="OUT",
                                            body=handoff_msg,
                                            actor_user_id=None,
                                            created_at=now_utc(),
                                        )
                                    )
                                    conv.last_message_at = now_utc()
                                    conv.updated_at = now_utc()

                                    audit(
                                        db,
                                        channel="WHATSAPP",
                                        request_id=None,
                                        action="HUMAN_HANDOFF",
                                        policy_number=policy_number,
                                        customer_phone=customer_phone,
                                        success=True,
                                        reason=None,
                                    )
                                    db.commit()
                                except Exception:
                                    # Never fail the webhook for handoff
                                    try:
                                        db.commit()
                                    except Exception:
                                        pass
                            else:
                                reply = openai_generate_reply(
                                customer_phone=customer_phone,
                                customer_name=customer_name,
                                user_text=(text_body or "").strip(),
                                policy_number=policy_number,
                                db=db,
                            )
                            if reply:
                                # Deliver to WhatsApp
                                send_whatsapp_text(customer_phone, reply)

                                # Store OUT message in the same conversation thread
                                db.add(
                                    InboxMessage(
                                        conversation_id=conv.id,
                                        direction="OUT",
                                        body=reply,
                                        actor_user_id=None,
                                        created_at=now_utc(),
                                    )
                                )
                                conv.last_message_at = now_utc()
                                conv.updated_at = now_utc()

                                audit(
                                    db,
                                    channel="WHATSAPP",
                                    request_id=None,
                                    action="AUTO_REPLY",
                                    policy_number=policy_number,
                                    customer_phone=customer_phone,
                                    success=True,
                                    reason=None,
                                )
                                db.commit()
                    except Exception as _e:
                        # Never fail the webhook; just record a short audit note
                        try:
                            audit(
                                db,
                                channel="WHATSAPP",
                                request_id=None,
                                action="AUTO_REPLY",
                                policy_number=policy_number,
                                customer_phone=customer_phone,
                                success=False,
                                reason=str(_e)[:250],
                            )
                        except Exception:
                            pass
                        try:
                            db.commit()
                        except Exception:
                            pass

        # Even if we handled nothing (statuses-only payload), return OK
        return {"ok": True, "handled": handled}

    except Exception as e:
        # log failure but don't error to Meta
        try:
            audit(
                db,
                channel="WHATSAPP",
                request_id=None,
                action="INBOX_INGEST",
                policy_number=None,
                customer_phone=None,
                success=False,
                reason=str(e)[:250],
            )
            try:
                db.commit()
            except Exception:
                pass
        except Exception:
            pass
        return {"ok": True, "handled": handled}


# =========================================================
# API: Policy Lookup (DB-only facts + audit)
# =========================================================

@app.post("/policy/lookup", response_model=PolicyLookupResponse)
def policy_lookup(payload: PolicyLookupRequest, db: Session = Depends(get_db)):
    pn = payload.policy_number.strip()

    if not POLICY_RE.match(pn):
        audit(
            db,
            channel=payload.channel,
            request_id=payload.request_id,
            action="POLICY_LOOKUP",
            policy_number=pn,
            customer_phone=payload.customer_phone_e164,
            success=False,
            reason="INVALID_POLICY_FORMAT",
        )
        raise HTTPException(status_code=400, detail="Invalid policy number format.")

    policy = db.execute(select(Policy).where(Policy.policy_number == pn)).scalars().first()

    if not policy:
        audit(
            db,
            channel=payload.channel,
            request_id=payload.request_id,
            action="POLICY_LOOKUP",
            policy_number=pn,
            customer_phone=payload.customer_phone_e164,
            success=False,
            reason="NOT_FOUND",
        )
        return PolicyLookupResponse(found=False, message="Policy not found. Please verify the policy number.")

    if payload.customer_phone_e164 and policy.customer and policy.customer.phone_e164:
        if payload.customer_phone_e164.strip() != policy.customer.phone_e164.strip():
            audit(
                db,
                channel=payload.channel,
                request_id=payload.request_id,
                action="POLICY_LOOKUP",
                policy_number=pn,
                customer_phone=payload.customer_phone_e164,
                success=False,
                reason="PHONE_MISMATCH",
            )
            raise HTTPException(status_code=403, detail="Verification failed (phone mismatch).")

    last_payment = db.execute(
        select(Payment)
        .where(Payment.policy_id == policy.id)
        .order_by(desc(Payment.paid_on), desc(Payment.created_at))
        .limit(1)
    ).scalars().first()

    next_due = db.execute(
        select(PremiumSchedule)
        .where(PremiumSchedule.policy_id == policy.id, PremiumSchedule.is_paid == False)  # noqa: E712
        .order_by(asc(PremiumSchedule.due_date))
        .limit(1)
    ).scalars().first()

    next_due_date = next_due.due_date if next_due else policy.next_premium_due_date

    msg_parts = [f"Policy {policy.policy_number} status: {policy.status}."]
    if next_due_date:
        msg_parts.append(f"Next premium due date: {next_due_date.isoformat()}.")
    if policy.premium_amount is not None:
        msg_parts.append(f"Premium amount: ₹{float(policy.premium_amount):,.2f}.")
    if policy.maturity_date:
        msg_parts.append(f"Maturity date: {policy.maturity_date.isoformat()}.")
    if policy.maturity_amount_expected is not None:
        msg_parts.append(f"Expected maturity amount: ₹{float(policy.maturity_amount_expected):,.2f}.")
    if last_payment:
        msg_parts.append(
            f"Last payment: {last_payment.paid_on.isoformat()} "
            f"({last_payment.status}, ₹{float(last_payment.amount):,.2f})."
        )

    audit(
        db,
        channel=payload.channel,
        request_id=payload.request_id,
        action="POLICY_LOOKUP",
        policy_number=pn,
        customer_phone=payload.customer_phone_e164,
        success=True,
        reason=None,
    )

    return PolicyLookupResponse(
        found=True,
        policy_number=policy.policy_number,
        carrier=policy.carrier,
        status=policy.status,
        plan_name=policy.plan_name,
        premium_amount=money(policy.premium_amount),
        next_premium_due_date=next_due_date,
        grace_period_days=int(policy.grace_period_days) if str(policy.grace_period_days).isdigit() else None,
        maturity_date=policy.maturity_date,
        maturity_amount_expected=money(policy.maturity_amount_expected),
        sum_assured=money(policy.sum_assured),
        last_payment_date=last_payment.paid_on if last_payment else None,
        last_payment_amount=money(last_payment.amount) if last_payment else None,
        last_payment_status=last_payment.status if last_payment else None,
        message=" ".join(msg_parts),
    )


# =========================================================
# ADMIN: Policies + Audit
# =========================================================

@app.get("/admin/policies")
def admin_list_policies(q: Optional[str] = None, limit: int = 50, db: Session = Depends(get_db)):
    limit = min(max(limit, 1), 200)
    stmt = select(Policy).order_by(desc(Policy.created_at)).limit(limit)
    if q:
        qq = f"%{q.strip()}%"
        stmt = (
            select(Policy)
            .where(Policy.policy_number.like(qq))
            .order_by(desc(Policy.created_at))
            .limit(limit)
        )

    items = db.execute(stmt).scalars().all()
    data = []
    for p in items:
        data.append({
            "policy_number": p.policy_number,
            "carrier": p.carrier,
            "status": p.status,
            "plan_name": p.plan_name,
            "premium_amount": money(p.premium_amount),
            "next_premium_due_date": p.next_premium_due_date.isoformat() if p.next_premium_due_date else None,
            "maturity_date": p.maturity_date.isoformat() if p.maturity_date else None,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        })
    return {"items": data}




@app.post("/admin/policies/upload")
async def admin_upload_policies_excel(
    file: UploadFile = File(...),
    dry_run: bool = Form(False),
    db: Session = Depends(get_db),
):
    """Upload policy + customer details from an Excel (.xlsx) sheet.

    This is additive and does NOT change any existing endpoints/logic.

    Expected headers (case-insensitive):
      - customer_full_name (required)
      - customer_phone_e164 (recommended) OR customer_email
      - policy_number (required)
      - start_date (required)
    Optional headers (map to DB fields):
      - carrier, plan_name, plan_code, status, end_date, maturity_date,
        sum_assured, maturity_amount_expected, premium_amount, premium_frequency,
        next_premium_due_date, grace_period_days, nominee_name, nominee_relation,
        customer_email, customer_dob, customer_pan_last4
    Optional schedule/payment (one row can include one schedule/payment record):
      - schedule_due_date, schedule_amount, schedule_is_paid, schedule_paid_on
      - payment_paid_on, payment_amount, payment_status, payment_reference_id, payment_method, payment_notes

    Rows with an existing policy_number are skipped (to avoid unexpected overwrites).
    """

    content = await file.read()

    def _norm(s: str) -> str:
        return re.sub(r"\s+", "_", (s or "").strip().lower())

    def _as_date(v):
        if v is None or v == "":
            return None
        if isinstance(v, datetime):
            return v.date()
        if isinstance(v, date):
            return v
        s = str(v).strip()
        # try common formats
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d-%B-%Y"):
            try:
                return datetime.strptime(s, fmt).date()
            except Exception:
                pass
        return None

    def _as_num(v):
        if v is None or v == "":
            return None
        try:
            return float(v)
        except Exception:
            try:
                return float(str(v).replace(",", ""))
            except Exception:
                return None

    # Load workbook safely from bytes
    try:
        wb = openpyxl.load_workbook(filename=BytesIO(content), data_only=True)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid Excel file (.xlsx required): {e}")

    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise HTTPException(status_code=400, detail="Excel sheet is empty")

    header = [(_norm(str(c)) if c is not None else "") for c in rows[0]]
    if not any(header):
        raise HTTPException(status_code=400, detail="Header row is empty")

    # Build row dicts
    created_customers = 0
    created_policies = 0
    created_schedule = 0
    created_payments = 0
    skipped_existing_policy = 0
    errors = []

    def get(d, *keys):
        for k in keys:
            if k in d and d[k] not in (None, ""):
                return d[k]
        return None

    for r_idx, row in enumerate(rows[1:], start=2):
        d = {header[i]: row[i] for i in range(min(len(header), len(row)))}
        if not any(v not in (None, "") for v in d.values()):
            continue

        try:
            customer_full_name = get(d, "customer_full_name", "full_name", "name")
            customer_phone = get(d, "customer_phone_e164", "phone_e164", "customer_phone", "phone")
            customer_email = get(d, "customer_email", "email")

            policy_number = get(d, "policy_number", "policy", "policy_no", "policyno")
            start_date = _as_date(get(d, "start_date", "policy_start_date"))

            if not customer_full_name:
                raise ValueError("Missing customer_full_name")
            if not policy_number:
                raise ValueError("Missing policy_number")
            if not start_date:
                raise ValueError("Missing/invalid start_date (use YYYY-MM-DD or a real Excel date)")

            policy_number = str(policy_number).strip()
            if not policy_number:
                raise ValueError("Empty policy_number")

            # Upsert customer by phone_e164 (preferred), else email
            cust = None
            if customer_phone:
                phone = str(customer_phone).strip()
                if phone and not phone.startswith("+") and phone.isdigit():
                    phone = "+" + phone
                cust = db.execute(select(Customer).where(Customer.phone_e164 == phone)).scalars().first()
            if not cust and customer_email:
                cust = db.execute(select(Customer).where(Customer.email == str(customer_email).strip())).scalars().first()

            if not cust:
                if dry_run:
                    created_customers += 1
                    cust_id = str(uuid.uuid4())
                else:
                    cust = Customer(
                        full_name=str(customer_full_name).strip(),
                        phone_e164=(phone if customer_phone else None),
                        email=(str(customer_email).strip() if customer_email else None),
                        dob=_as_date(get(d, "customer_dob", "dob")),
                        pan_last4=(str(get(d, "customer_pan_last4", "pan_last4")).strip() if get(d, "customer_pan_last4", "pan_last4") else None),
                        created_at=now_utc(),
                        updated_at=now_utc(),
                    )
                    db.add(cust)
                    db.flush()
                    created_customers += 1
            else:
                # fill missing optional fields only (no overwrites)
                if not dry_run:
                    if customer_full_name and not cust.full_name:
                        cust.full_name = str(customer_full_name).strip()
                    if customer_email and not cust.email:
                        cust.email = str(customer_email).strip()
                    if customer_phone and not cust.phone_e164:
                        cust.phone_e164 = phone
                    if get(d, "customer_dob", "dob") and not cust.dob:
                        cust.dob = _as_date(get(d, "customer_dob", "dob"))
                    if get(d, "customer_pan_last4", "pan_last4") and not cust.pan_last4:
                        cust.pan_last4 = str(get(d, "customer_pan_last4", "pan_last4")).strip()[:4]
                    cust.updated_at = now_utc()

            # Skip existing policy to avoid overwrites
            existing = db.execute(select(Policy).where(Policy.policy_number == policy_number)).scalars().first()
            if existing:
                skipped_existing_policy += 1
                continue

            # Build policy
            policy_kwargs = dict(
                carrier=str(get(d, "carrier") or "LIC").strip(),
                policy_number=policy_number,
                plan_name=(str(get(d, "plan_name")).strip() if get(d, "plan_name") else None),
                plan_code=(str(get(d, "plan_code")).strip() if get(d, "plan_code") else None),
                status=str(get(d, "status") or "ACTIVE").strip().upper(),
                start_date=start_date,
                end_date=_as_date(get(d, "end_date")),
                maturity_date=_as_date(get(d, "maturity_date")),
                sum_assured=_as_num(get(d, "sum_assured")),
                maturity_amount_expected=_as_num(get(d, "maturity_amount_expected")),
                premium_amount=_as_num(get(d, "premium_amount")),
                premium_frequency=str(get(d, "premium_frequency") or "YEARLY").strip().upper(),
                next_premium_due_date=_as_date(get(d, "next_premium_due_date")),
                grace_period_days=str(get(d, "grace_period_days") or "30").strip(),
                nominee_name=(str(get(d, "nominee_name")).strip() if get(d, "nominee_name") else None),
                nominee_relation=(str(get(d, "nominee_relation")).strip() if get(d, "nominee_relation") else None),
                created_at=now_utc(),
                updated_at=now_utc(),
            )

            if dry_run:
                created_policies += 1
                policy_id = str(uuid.uuid4())
            else:
                # ensure we have a customer object
                if not cust:
                    raise ValueError("Internal error: customer not resolved")
                pol = Policy(customer_id=cust.id, **policy_kwargs)
                db.add(pol)
                db.flush()
                created_policies += 1
                policy_id = pol.id

                # Optional schedule record
                sched_due = _as_date(get(d, "schedule_due_date"))
                sched_amt = _as_num(get(d, "schedule_amount"))
                if sched_due and (sched_amt is not None):
                    is_paid = str(get(d, "schedule_is_paid") or "false").strip().lower() in ("1","true","yes","y")
                    db.add(
                        PremiumSchedule(
                            policy_id=policy_id,
                            due_date=sched_due,
                            amount=sched_amt,
                            is_paid=is_paid,
                            paid_on=_as_date(get(d, "schedule_paid_on")),
                            created_at=now_utc(),
                        )
                    )
                    created_schedule += 1

                # Optional payment record
                pay_on = _as_date(get(d, "payment_paid_on"))
                pay_amt = _as_num(get(d, "payment_amount"))
                if pay_on and (pay_amt is not None):
                    db.add(
                        Payment(
                            policy_id=policy_id,
                            paid_on=pay_on,
                            amount=pay_amt,
                            status=str(get(d, "payment_status") or "SUCCESS").strip().upper(),
                            reference_id=(str(get(d, "payment_reference_id")).strip() if get(d, "payment_reference_id") else None),
                            method=(str(get(d, "payment_method")).strip() if get(d, "payment_method") else None),
                            notes=(str(get(d, "payment_notes")).strip() if get(d, "payment_notes") else None),
                            created_at=now_utc(),
                        )
                    )
                    created_payments += 1

        except Exception as e:
            errors.append({"row": r_idx, "error": str(e)})

    if dry_run:
        db.rollback()
        return {
            "ok": True,
            "dry_run": True,
            "created_customers": created_customers,
            "created_policies": created_policies,
            "created_schedule": created_schedule,
            "created_payments": created_payments,
            "skipped_existing_policy": skipped_existing_policy,
            "errors": errors[:50],
        }

    db.commit()

    return {
        "ok": True,
        "dry_run": False,
        "created_customers": created_customers,
        "created_policies": created_policies,
        "created_schedule": created_schedule,
        "created_payments": created_payments,
        "skipped_existing_policy": skipped_existing_policy,
        "errors": errors[:50],
    }
@app.get("/admin/audit")
def admin_audit(limit: int = 100, db: Session = Depends(get_db)):
    limit = min(max(limit, 1), 300)
    items = db.execute(
        select(AuditLog).order_by(desc(AuditLog.created_at)).limit(limit)
    ).scalars().all()

    data = []
    for a in items:
        data.append({
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "channel": a.channel,
            "action": a.action,
            "policy_number": a.policy_number,
            "success": a.success,
            "reason": a.reason,
            "request_id": a.request_id,
        })
    return {"items": data}


# =========================================================
# TEAM INBOX ADMIN API
# =========================================================

@app.get("/admin/team")
def admin_team(db: Session = Depends(get_db)):
    seed_team_if_empty(db)
    users = db.execute(
        select(TeamUser).where(TeamUser.is_active == True).order_by(asc(TeamUser.role), asc(TeamUser.full_name))  # noqa: E712
    ).scalars().all()
    return {
        "items": [{"id": u.id, "role": u.role, "full_name": u.full_name} for u in users]
    }


@app.get("/admin/inbox/conversations")
def admin_inbox_conversations(
    status: Optional[str] = None,
    assigned_to: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 80,
    db: Session = Depends(get_db),
):
    limit = min(max(limit, 1), 200)

    stmt = select(InboxConversation).order_by(desc(InboxConversation.last_message_at)).limit(limit)

    if status:
        stmt = stmt.where(InboxConversation.status == status.upper())
    if assigned_to:
        if assigned_to.lower() == "unassigned":
            stmt = stmt.where(InboxConversation.assigned_to_user_id.is_(None))
        else:
            stmt = stmt.where(InboxConversation.assigned_to_user_id == assigned_to)
    if q:
        qq = f"%{q.strip()}%"
        stmt = stmt.where(
            (InboxConversation.customer_phone.like(qq)) |
            (InboxConversation.customer_name.like(qq)) |
            (InboxConversation.policy_number.like(qq))
        )

    items = db.execute(stmt).scalars().all()
    users = db.execute(select(TeamUser)).scalars().all()
    user_map = {u.id: u.full_name for u in users}

    return {
        "items": [{
            "id": c.id,
            "channel": c.channel,
            "customer_phone": c.customer_phone,
            "customer_name": c.customer_name,
            "policy_number": c.policy_number,
            "status": c.status,
            "priority": c.priority,
            "assigned_to_user_id": c.assigned_to_user_id,
            "assigned_to_name": user_map.get(c.assigned_to_user_id),
            "last_message_at": c.last_message_at.isoformat() if c.last_message_at else None,
        } for c in items]
    }


@app.get("/admin/inbox/conversations/{conversation_id}")
def admin_inbox_conversation_detail(conversation_id: str, db: Session = Depends(get_db)):
    conv = db.execute(select(InboxConversation).where(InboxConversation.id == conversation_id)).scalars().first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    msgs = db.execute(
        select(InboxMessage).where(InboxMessage.conversation_id == conversation_id).order_by(asc(InboxMessage.created_at))
    ).scalars().all()

    users = db.execute(select(TeamUser)).scalars().all()
    user_map = {u.id: {"name": u.full_name, "role": u.role} for u in users}

    return {
        "conversation": {
            "id": conv.id,
            "channel": conv.channel,
            "customer_phone": conv.customer_phone,
            "customer_name": conv.customer_name,
            "policy_number": conv.policy_number,
            "status": conv.status,
            "priority": conv.priority,
            "assigned_to_user_id": conv.assigned_to_user_id,
            "assigned_to_name": user_map.get(conv.assigned_to_user_id, {}).get("name"),
            "last_message_at": conv.last_message_at.isoformat() if conv.last_message_at else None,
        },
        "messages": [{
            "id": m.id,
            "direction": m.direction,
            "body": m.body,
            "actor_user_id": m.actor_user_id,
            "actor_name": user_map.get(m.actor_user_id, {}).get("name") if m.actor_user_id else None,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        } for m in msgs]
    }


@app.post("/admin/inbox/conversations/{conversation_id}/assign")
def admin_inbox_assign(conversation_id: str, payload: InboxAssignRequest, db: Session = Depends(get_db)):
    conv = db.execute(select(InboxConversation).where(InboxConversation.id == conversation_id)).scalars().first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    if payload.assigned_to_user_id is not None:
        if payload.assigned_to_user_id != "":
            user = db.execute(select(TeamUser).where(TeamUser.id == payload.assigned_to_user_id)).scalars().first()
            if not user:
                raise HTTPException(status_code=400, detail="Invalid assigned_to_user_id")
            conv.assigned_to_user_id = user.id
        else:
            conv.assigned_to_user_id = None

    if payload.status:
        st = payload.status.upper()
        if st not in ("OPEN", "PENDING", "CLOSED"):
            raise HTTPException(status_code=400, detail="Invalid status")
        conv.status = st

    if payload.priority:
        pr = payload.priority.upper()
        if pr not in ("LOW", "NORMAL", "HIGH"):
            raise HTTPException(status_code=400, detail="Invalid priority")
        conv.priority = pr

    conv.updated_at = now_utc()
    db.commit()
    return {"ok": True}


@app.post("/admin/inbox/conversations/{conversation_id}/send")
def admin_inbox_send(conversation_id: str, payload: InboxSendRequest, db: Session = Depends(get_db)):
    """
    If direction == OUT: deliver message to WhatsApp and store it in DB thread.
    If direction == NOTE: store internal note only.
    """
    conv = db.execute(select(InboxConversation).where(InboxConversation.id == conversation_id)).scalars().first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    direction = payload.direction.upper().strip()
    if direction not in ("OUT", "NOTE"):
        raise HTTPException(status_code=400, detail="direction must be OUT or NOTE")

    actor_id = payload.actor_user_id
    if actor_id:
        user = db.execute(
            select(TeamUser).where(TeamUser.id == actor_id, TeamUser.is_active == True)  # noqa: E712
        ).scalars().first()
        if not user:
            raise HTTPException(status_code=400, detail="Invalid actor_user_id")

    text = payload.body.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Message body cannot be empty")

    wa_result = None
    if direction == "OUT":
        # deliver to WhatsApp
        try:
            wa_result = send_whatsapp_text(conv.customer_phone, text)
        except Exception as e:
            # don't store message if delivery failed
            raise HTTPException(status_code=502, detail=f"WhatsApp delivery failed: {str(e)}")

    # Store message in thread (history)
    db.add(
        InboxMessage(
            conversation_id=conversation_id,
            direction=direction,
            body=text,
            actor_user_id=actor_id,
            created_at=now_utc(),
        )
    )

    conv.last_message_at = now_utc()
    conv.updated_at = now_utc()
    db.commit()

    return {"ok": True, "whatsapp_result": wa_result}


# =========================================================
# Dashboard (single-file HTML + CSS + JS)
# =========================================================

DASHBOARD_HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Policy Dashboard + Team Inbox</title>
  <style>
    :root{
      --bg: #0b1020;
      --panel: rgba(255,255,255,.06);
      --text: rgba(255,255,255,.92);
      --muted: rgba(255,255,255,.66);
      --border: rgba(255,255,255,.12);
      --good: #2fe38a;
      --warn: #ffcc66;
      --bad: #ff5c7a;
      --accent: #7c5cff;
      --accent2: #22d3ee;
      --shadow: 0 18px 45px rgba(0,0,0,.40);
      --radius: 18px;
      --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
      --sans: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial;
    }
    *{ box-sizing:border-box; }
    body{
      margin:0;
      font-family:var(--sans);
      color:var(--text);
      background:
        radial-gradient(900px 600px at 20% 10%, rgba(124,92,255,.30), transparent 60%),
        radial-gradient(800px 500px at 85% 30%, rgba(34,211,238,.25), transparent 55%),
        radial-gradient(700px 500px at 50% 90%, rgba(47,227,138,.10), transparent 55%),
        var(--bg);
      min-height:100vh;
    }
    .wrap{ max-width:1180px; margin:0 auto; padding:24px 16px 48px; }
    .topbar{ display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:14px; }
    .brand{ display:flex; align-items:center; gap:12px; }
    .logo{
      width:42px; height:42px; border-radius:14px;
      background: linear-gradient(135deg, rgba(124,92,255,1), rgba(34,211,238,1));
      box-shadow: var(--shadow);
    }
    h1{ font-size:18px; margin:0; letter-spacing:.2px; }
    .sub{ color:var(--muted); font-size:12px; margin-top:2px; }
    .right{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; justify-content:flex-end; }
    .pill{ font-size:12px; padding:6px 10px; border-radius:999px; border: 1px solid var(--border); background: rgba(255,255,255,.06); color: var(--muted); }
    .btn{
      border:0; padding:12px 14px; border-radius: 14px; cursor:pointer; font-weight:700;
      background: linear-gradient(135deg, rgba(124,92,255,1), rgba(34,211,238,1));
      color:#071022; box-shadow: 0 14px 35px rgba(124,92,255,.18);
      transition: transform .08s ease;
    }
    .btn:active{ transform: translateY(1px); }
    .btnGhost{ background: rgba(255,255,255,.06); color: var(--text); border:1px solid var(--border); box-shadow:none; font-weight:700; }
    .tabs{ display:flex; gap:10px; flex-wrap:wrap; margin-bottom:14px; }
    .tab{
      padding:10px 12px; border-radius:999px; border:1px solid var(--border);
      background: rgba(255,255,255,.05); color: var(--muted);
      cursor:pointer; font-size:12.5px; user-select:none;
    }
    .tab.active{
      color:#071022;
      background: linear-gradient(135deg, rgba(124,92,255,1), rgba(34,211,238,1));
      border-color: transparent;
      font-weight:800;
    }
    .grid{ display:grid; grid-template-columns: 1.05fr .95fr; gap:16px; }
    @media (max-width: 980px){ .grid{ grid-template-columns: 1fr; } }
    .card{
      background: rgba(255,255,255,.06);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      overflow:hidden;
    }
    .cardHeader{
      padding:14px 16px;
      border-bottom: 1px solid var(--border);
      background: linear-gradient(180deg, rgba(255,255,255,.06), transparent);
      display:flex; align-items:center; justify-content:space-between; gap:12px;
    }
    .cardHeader h2{ font-size:14px; margin:0; letter-spacing:.2px; }
    .cardBody{ padding:16px; }
    .row{ display:flex; gap:10px; align-items:flex-end; flex-wrap:wrap; }
    .field{ flex: 1 1 240px; }
    label{ display:block; font-size:12px; color: var(--muted); margin:0 0 6px; }
    input, select, textarea{
      width:100%;
      padding:12px 12px;
      border-radius: 14px;
      border:1px solid var(--border);
      background: rgba(10,14,28,.55);
      color: var(--text);
      outline:none;
    }
    textarea{ min-height: 90px; resize: vertical; }
    .hint{ margin-top:10px; color: var(--muted); font-size:12px; line-height:1.45; }
    .result{
      margin-top:14px; padding:14px; border-radius: 16px;
      border:1px solid var(--border); background: rgba(255,255,255,.05);
    }
    .resultTitle{ display:flex; justify-content:space-between; align-items:center; gap:10px; margin-bottom:8px; }
    .badge{ font-size:12px; padding:5px 10px; border-radius:999px; border:1px solid var(--border); background: rgba(255,255,255,.06); }
    .badge.good{ color: var(--good); border-color: rgba(47,227,138,.35); }
    .badge.bad{ color: var(--bad); border-color: rgba(255,92,122,.35); }
    .kv{ display:grid; grid-template-columns: 1fr 1fr; gap:10px; margin-top:10px; }
    @media (max-width: 520px){ .kv{ grid-template-columns: 1fr; } }
    .k{
      padding:10px 12px; border-radius: 14px;
      background: rgba(255,255,255,.04);
      border:1px solid rgba(255,255,255,.08);
    }
    .k .t{ color: var(--muted); font-size:12px; margin-bottom:4px; }
    .k .v{ font-family: var(--mono); font-size:12.5px; }
    table{
      width:100%; border-collapse:separate; border-spacing:0;
      overflow:hidden; border-radius: 16px; border:1px solid var(--border);
      background: rgba(255,255,255,.04);
    }
    th, td{
      text-align:left; padding:10px 10px;
      border-bottom:1px solid rgba(255,255,255,.06);
      font-size:12.5px; vertical-align:top;
    }
    th{ color: rgba(255,255,255,.78); font-weight:800; background: rgba(255,255,255,.05); }
    tr:last-child td{ border-bottom:0; }
    .mono{ font-family: var(--mono); font-size:12px; color: rgba(255,255,255,.85); }
    a.link{ color: var(--accent2); text-decoration:none; }
    .footer{ margin-top:14px; color: var(--muted); font-size:12px; text-align:center; }

    /* Inbox layout */
    .inboxGrid{ display:grid; grid-template-columns: 360px 1fr; gap:14px; }
    @media (max-width: 980px){ .inboxGrid{ grid-template-columns: 1fr; } }
    .convList{
      max-height: 560px; overflow:auto;
      border-radius: 16px; border:1px solid var(--border);
      background: rgba(255,255,255,.04);
    }
    .convItem{ padding:12px 12px; border-bottom:1px solid rgba(255,255,255,.06); cursor:pointer; }
    .convItem:last-child{ border-bottom:0; }
    .convItem.active{ background: rgba(124,92,255,.16); border-left: 3px solid rgba(34,211,238,.95); }
    .convTop{ display:flex; justify-content:space-between; gap:10px; }
    .convName{ font-weight:800; font-size:12.8px; }
    .convMeta{ color: var(--muted); font-size:11.5px; margin-top:3px; }
    .chip{
      font-size:11px; padding:4px 8px; border-radius:999px;
      border:1px solid rgba(255,255,255,.12);
      background: rgba(255,255,255,.06);
      color: rgba(255,255,255,.75);
    }
    .chip.good{ color: var(--good); border-color: rgba(47,227,138,.35); }
    .chip.warn{ color: var(--warn); border-color: rgba(255,204,102,.35); }
    .chip.bad{ color: var(--bad); border-color: rgba(255,92,122,.35); }
    .thread{
      border-radius: 16px; border:1px solid var(--border);
      background: rgba(255,255,255,.04);
      max-height: 560px; overflow:auto;
      padding:12px;
    }
    .msg{
      padding:10px 12px; border-radius: 14px; margin-bottom:10px;
      border:1px solid rgba(255,255,255,.08);
      background: rgba(255,255,255,.04);
    }
    .msg.in{ border-color: rgba(34,211,238,.25); }
    .msg.out{ border-color: rgba(47,227,138,.25); }
    .msg.note{ border-color: rgba(255,204,102,.25); background: rgba(255,204,102,.06); }
    .msgHead{
      display:flex; justify-content:space-between; gap:10px;
      margin-bottom:6px; font-size:11.5px; color: var(--muted);
    }
    .compose{ margin-top:12px; display:grid; grid-template-columns: 1fr 180px; gap:10px; }
    @media (max-width: 700px){ .compose{ grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <div class="brand">
        <div class="logo"></div>
        <div>
          <h1>Policy Dashboard + Team Inbox</h1>
          <div class="sub">Policy lookup, audit logs, and a shared team inbox for WhatsApp support</div>
        </div>
      </div>
      <div class="right">
        <span class="pill" id="dbpill">DB: loading…</span>
        <span class="pill">Webhook: <span class="mono">/webhook</span></span>
        <button class="btn btnGhost" onclick="refreshAll()">Refresh</button>
      </div>
    </div>

    <div class="tabs">
      <div class="tab active" id="tab-lookup" onclick="showTab('lookup')">Policy Lookup</div>
      <div class="tab" id="tab-policies" onclick="showTab('policies')">Policies</div>
      <div class="tab" id="tab-inbox" onclick="showTab('inbox')">Team Inbox</div>
      <div class="tab" id="tab-audit" onclick="showTab('audit')">Audit Logs</div>
    </div>

    <div id="panel-lookup">
      <div class="grid">
        <div class="card">
          <div class="cardHeader">
            <h2>Quick Policy Lookup</h2>
            <span class="pill">POST /policy/lookup</span>
          </div>
          <div class="cardBody">
            <div class="row">
              <div class="field">
                <label>Policy Number</label>
                <input id="policy_number" placeholder="e.g. 12345678" />
              </div>
              <div class="field">
                <label>Customer Phone (optional verification)</label>
                <input id="phone" placeholder="+919876543210" />
              </div>
              <div class="field" style="flex: 0 0 180px;">
                <label>Channel</label>
                <select id="channel">
                  <option>WHATSAPP</option>
                  <option>WEB</option>
                  <option>VOICE</option>
                  <option>AGENT_APP</option>
                </select>
              </div>
              <div style="flex:0 0 auto;">
                <button class="btn" onclick="doLookup()">Lookup</button>
              </div>
            </div>

            <div class="hint">
              Lookup is DB-backed only (no hallucinations). WhatsApp replies are sent from Inbox tab and delivered via Cloud API.
            </div>

            <div id="lookup_result" class="result" style="display:none;"></div>
          </div>
        </div>

        <div class="card">
          <div class="cardHeader">
            <h2>WhatsApp Setup (Meta)</h2>
            <span class="pill">Delivery enabled</span>
          </div>
          <div class="cardBody">
            <div class="hint">
              Set Meta webhook URL to <span class="mono">https://YOUR_DOMAIN/webhook</span><br/>
              Env vars required:
              <div class="mono" style="margin-top:8px;">
                WHATSAPP_VERIFY_TOKEN<br/>
                WHATSAPP_ACCESS_TOKEN<br/>
                WHATSAPP_PHONE_NUMBER_ID<br/>
              </div>
              OUT messages from Inbox are delivered immediately.
            </div>
          </div>
        </div>
      </div>
    </div>

    <div id="panel-policies" style="display:none;">
      <div class="card">
        <div class="cardHeader">
          <h2>Policies</h2>
          <span class="pill">GET /admin/policies</span>
        </div>
        <div class="cardBody">
          <div class="row" style="margin-bottom:10px;">
            <div class="field">
              <label>Search by policy number</label>
              <input id="policy_search" placeholder="type to filter…" oninput="debouncedRefreshPolicies()" />
            </div>
          </div>
          <div style="overflow:auto;">
            <table>
              <thead>
                <tr>
                  <th>Policy</th>
                  <th>Status</th>
                  <th>Next Due</th>
                  <th>Maturity</th>
                </tr>
              </thead>
              <tbody id="policies_tbody">
                <tr><td colspan="4" class="small">Loading…</td></tr>
              </tbody>
            </table>
          </div>
          <div class="footer">Tip: click a policy number to auto-fill lookup.</div>

          <div style="margin-top:14px; padding-top:12px; border-top:1px solid rgba(255,255,255,.10);">
            <div style="display:flex; align-items:center; justify-content:space-between; gap:10px; flex-wrap:wrap;">
              <div style="font-weight:800; font-size:13px;">Upload Policies (Excel)</div>
              <span class="pill">POST /admin/policies/upload</span>
            </div>
            <div class="hint" style="margin-top:8px;">
              Upload a <span class="mono">.xlsx</span> file to add Customers + Policies (optional: Schedule + Payments). Existing policies are skipped.
              <div class="mono" style="margin-top:8px;">
                Required headers: <b>policy_number</b>, <b>start_date</b>, <b>customer_full_name</b>
              </div>
            </div>
            <div class="row" style="margin-top:10px;">
              <div class="field">
                <label>Select Excel (.xlsx)</label>
                <input type="file" id="policies_excel" accept=".xlsx" />
              </div>
              <div style="flex:0 0 auto;">
                <button class="btn btnGhost" onclick="uploadPoliciesExcel(true)">Dry Run</button>
              </div>
              <div style="flex:0 0 auto;">
                <button class="btn" onclick="uploadPoliciesExcel(false)">Upload</button>
              </div>
            </div>
            <div id="policies_upload_result" class="result" style="display:none;"></div>
          </div>

        </div>
      </div>
    </div>

    <div id="panel-inbox" style="display:none;">
      <div class="card">
        <div class="cardHeader">
          <h2>Team Inbox (Delivered Replies)</h2>
          <span class="pill">Replies go to WhatsApp</span>
        </div>
        <div class="cardBody">
          <div class="row" style="margin-bottom:10px;">
            <div class="field">
              <label>Search (phone / name / policy)</label>
              <input id="inbox_search" placeholder="type to filter…" oninput="debouncedRefreshInbox()" />
            </div>
            <div class="field" style="flex:0 0 180px;">
              <label>Status</label>
              <select id="inbox_status" onchange="refreshInbox()">
                <option value="">ALL</option>
                <option>OPEN</option>
                <option>PENDING</option>
                <option>CLOSED</option>
              </select>
            </div>
            <div class="field" style="flex:0 0 220px;">
              <label>Assigned</label>
              <select id="inbox_assigned" onchange="refreshInbox()">
                <option value="">ALL</option>
                <option value="unassigned">UNASSIGNED</option>
              </select>
            </div>
            <div class="field" style="flex:0 0 260px;">
              <label>Acting as (for replies/notes)</label>
              <select id="acting_user"></select>
            </div>
          </div>

          <div class="inboxGrid">
            <div>
              <div class="convList" id="conv_list">
                <div class="convItem"><span class="small">Loading…</span></div>
              </div>
              <div class="footer">Incoming WhatsApp messages appear via <span class="mono">/webhook</span>.</div>
            </div>

            <div>
              <div class="row" style="margin-bottom:10px;">
                <div class="field">
                  <label>Assign to</label>
                  <select id="assign_to"></select>
                </div>
                <div class="field" style="flex:0 0 180px;">
                  <label>Status</label>
                  <select id="conv_status">
                    <option>OPEN</option>
                    <option>PENDING</option>
                    <option>CLOSED</option>
                  </select>
                </div>
                <div class="field" style="flex:0 0 180px;">
                  <label>Priority</label>
                  <select id="conv_priority">
                    <option>LOW</option>
                    <option selected>NORMAL</option>
                    <option>HIGH</option>
                  </select>
                </div>
                <div style="flex:0 0 auto;">
                  <button class="btn btnGhost" onclick="saveConvMeta()">Save</button>
                </div>
              </div>

              <div class="thread" id="thread">
                <div class="small">Select a conversation to view messages.</div>
              </div>

              <div class="compose">
                <div>
                  <label>Message (Reply or Note)</label>
                  <textarea id="compose_body" placeholder="Reply to customer (OUT) or internal note (NOTE)…"></textarea>
                </div>
                <div>
                  <label>Type</label>
                  <select id="compose_type">
                    <option value="OUT">Reply (OUT) — delivered</option>
                    <option value="NOTE">Internal Note</option>
                  </select>
                  <div style="height:10px;"></div>
                  <button class="btn" style="width:100%;" onclick="sendMessage()">Send</button>
                  <div class="hint">OUT is sent via WhatsApp Cloud API. NOTE is internal only.</div>
                </div>
              </div>

            </div>
          </div>

        </div>
      </div>
    </div>

    <div id="panel-audit" style="display:none;">
      <div class="card">
        <div class="cardHeader">
          <h2>Audit Logs</h2>
          <span class="pill">GET /admin/audit</span>
        </div>
        <div class="cardBody">
          <div style="overflow:auto;">
            <table>
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Channel</th>
                  <th>Action</th>
                  <th>Policy</th>
                  <th>Result</th>
                  <th>Reason</th>
                </tr>
              </thead>
              <tbody id="audit_tbody">
                <tr><td colspan="6" class="small">Loading…</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>

  </div>

<script>
  let debounceTimer = null;
  let inboxDebounce = null;
  let selectedConvId = null;
  let teamUsers = [];

  function $(id){ return document.getElementById(id); }

  function safe(v){
    if (v === null || v === undefined || v === "") return "Not available";
    return v;
  }

  function fmtINR(v){
    if (v === null || v === undefined) return "Not available";
    try{
      return "₹" + Number(v).toLocaleString("en-IN", {minimumFractionDigits:2, maximumFractionDigits:2});
    }catch(e){
      return v;
    }
  }

  function setActiveTab(name){
    ["lookup","policies","inbox","audit"].forEach(t => {
      $("tab-"+t).classList.toggle("active", t===name);
      $("panel-"+t).style.display = (t===name) ? "block" : "none";
    });
  }

  function showTab(name){
    setActiveTab(name);
    if(name === "policies") refreshPolicies();
    if(name === "audit") refreshAudit();
    if(name === "inbox") refreshInbox();
  }

  async function doLookup(){
    const policy_number = $("policy_number").value.trim();
    const phone = $("phone").value.trim();
    const channel = $("channel").value;

    if(!policy_number){ alert("Enter policy number"); return; }

    const payload = {
      policy_number,
      customer_phone_e164: phone ? phone : null,
      channel,
      request_id: "dash-" + Date.now()
    };

    const box = $("lookup_result");
    box.style.display = "block";
    box.innerHTML = "<div class='small'>Looking up…</div>";

    try{
      const resp = await fetch("/policy/lookup", {
        method: "POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify(payload)
      });

      const data = await resp.json();
      if(!resp.ok){
        box.innerHTML = `<div class="resultTitle">
          <div><b>Lookup Failed</b></div>
          <span class="badge bad">Error</span>
        </div>
        <div class="small">${safe(data.detail)}</div>`;
        await refreshAudit();
        return;
      }

      const badge = data.found ? `<span class="badge good">FOUND</span>` : `<span class="badge bad">NOT FOUND</span>`;
      box.innerHTML = `
        <div class="resultTitle">
          <div><b>Result</b> <span class="mono">${safe(data.policy_number)}</span></div>
          ${badge}
        </div>
        <div class="small">${safe(data.message)}</div>

        <div class="kv">
          <div class="k"><div class="t">Status</div><div class="v">${safe(data.status)}</div></div>
          <div class="k"><div class="t">Plan</div><div class="v">${safe(data.plan_name)}</div></div>

          <div class="k"><div class="t">Premium Amount</div><div class="v">${fmtINR(data.premium_amount)}</div></div>
          <div class="k"><div class="t">Next Premium Due</div><div class="v">${safe(data.next_premium_due_date)}</div></div>

          <div class="k"><div class="t">Maturity Date</div><div class="v">${safe(data.maturity_date)}</div></div>
          <div class="k"><div class="t">Expected Maturity</div><div class="v">${fmtINR(data.maturity_amount_expected)}</div></div>

          <div class="k"><div class="t">Last Payment Date</div><div class="v">${safe(data.last_payment_date)}</div></div>
          <div class="k"><div class="t">Last Payment</div><div class="v">${fmtINR(data.last_payment_amount)} (${safe(data.last_payment_status)})</div></div>
        </div>
      `;

      await refreshAudit();
    }catch(e){
      box.innerHTML = `<div class="small">Network error: ${e}</div>`;
    }
  }

  async function refreshPolicies(){
    const q = ($("policy_search") ? $("policy_search").value.trim() : "");
    const url = q ? `/admin/policies?q=${encodeURIComponent(q)}&limit=50` : `/admin/policies?limit=50`;
    const tbody = $("policies_tbody");
    tbody.innerHTML = `<tr><td colspan="4" class="small">Loading…</td></tr>`;

    try{
      const resp = await fetch(url);
      const data = await resp.json();
      const items = data.items || [];
      if(items.length === 0){
        tbody.innerHTML = `<tr><td colspan="4" class="small">No policies found.</td></tr>`;
        return;
      }

      tbody.innerHTML = items.map(p => {
        const pn = safe(p.policy_number);
        return `
          <tr>
            <td><a class="link mono" href="#" onclick="pickPolicy('${pn}'); return false;">${pn}</a></td>
            <td>${safe(p.status)}</td>
            <td class="mono">${safe(p.next_premium_due_date)}</td>
            <td class="mono">${safe(p.maturity_date)}</td>
          </tr>
        `;
      }).join("");
    }catch(e){
      tbody.innerHTML = `<tr><td colspan="4" class="small">Error loading policies.</td></tr>`;
    }
  }

  function debouncedRefreshPolicies(){
    if(debounceTimer) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(refreshPolicies, 250);
  }

  function pickPolicy(pn){
    setActiveTab("lookup");
    $("policy_number").value = pn;
    doLookup();
  }

  async function refreshAudit(){
    const tbody = $("audit_tbody");
    tbody.innerHTML = `<tr><td colspan="6" class="small">Loading…</td></tr>`;
    try{
      const resp = await fetch("/admin/audit?limit=100");
      const data = await resp.json();
      const items = data.items || [];
      if(items.length === 0){
        tbody.innerHTML = `<tr><td colspan="6" class="small">No audit logs yet.</td></tr>`;
        return;
      }
      tbody.innerHTML = items.map(a => {
        const ok = a.success ? `<span class="badge good">OK</span>` : `<span class="badge bad">FAIL</span>`;
        return `
          <tr>
            <td class="mono">${safe(a.created_at)}</td>
            <td>${safe(a.channel)}</td>
            <td>${safe(a.action)}</td>
            <td class="mono">${safe(a.policy_number)}</td>
            <td>${ok}</td>
            <td class="mono">${safe(a.reason)}</td>
          </tr>
        `;
      }).join("");
    }catch(e){
      tbody.innerHTML = `<tr><td colspan="6" class="small">Error loading audit logs.</td></tr>`;
    }
  }

  async function loadTeam(){
    const resp = await fetch("/admin/team");
    const data = await resp.json();
    teamUsers = data.items || [];

    $("acting_user").innerHTML = teamUsers.map(u => `<option value="${u.id}">${u.full_name} (${u.role})</option>`).join("");
    $("assign_to").innerHTML = `<option value="">UNASSIGNED</option>` + teamUsers.map(u => `<option value="${u.id}">${u.full_name}</option>`).join("");

    const filt = $("inbox_assigned");
    const base = `<option value="">ALL</option><option value="unassigned">UNASSIGNED</option>`;
    const more = teamUsers.map(u => `<option value="${u.id}">${u.full_name}</option>`).join("");
    filt.innerHTML = base + more;
  }

  function debouncedRefreshInbox(){
    if(inboxDebounce) clearTimeout(inboxDebounce);
    inboxDebounce = setTimeout(refreshInbox, 250);
  }

  function statusChip(status){
    const s = (status||"").toUpperCase();
    if(s === "OPEN") return `<span class="chip good">OPEN</span>`;
    if(s === "PENDING") return `<span class="chip warn">PENDING</span>`;
    if(s === "CLOSED") return `<span class="chip bad">CLOSED</span>`;
    return `<span class="chip">${safe(status)}</span>`;
  }

  async function refreshInbox(){
    const q = $("inbox_search").value.trim();
    const status = $("inbox_status").value.trim();
    const assigned = $("inbox_assigned").value.trim();

    let url = `/admin/inbox/conversations?limit=120`;
    if(q) url += `&q=${encodeURIComponent(q)}`;
    if(status) url += `&status=${encodeURIComponent(status)}`;
    if(assigned) url += `&assigned_to=${encodeURIComponent(assigned)}`;

    const list = $("conv_list");
    list.innerHTML = `<div class="convItem"><span class="small">Loading…</span></div>`;

    try{
      const resp = await fetch(url);
      const data = await resp.json();
      const items = data.items || [];
      if(items.length === 0){
        list.innerHTML = `<div class="convItem"><span class="small">No conversations.</span></div>`;
        $("thread").innerHTML = `<div class="small">No conversation selected.</div>`;
        selectedConvId = null;
        return;
      }

      list.innerHTML = items.map(c => {
        const active = (c.id === selectedConvId) ? "active" : "";
        const title = c.customer_name ? c.customer_name : c.customer_phone;
        const line2 = `${c.customer_phone} • ${safe(c.policy_number)}`;
        const asg = c.assigned_to_name ? `Assigned: ${c.assigned_to_name}` : "Unassigned";
        return `
          <div class="convItem ${active}" onclick="openConv('${c.id}')">
            <div class="convTop">
              <div class="convName">${title}</div>
              ${statusChip(c.status)}
            </div>
            <div class="convMeta">${line2}</div>
            <div class="convMeta">${asg} • <span class="mono">${safe(c.last_message_at)}</span></div>
          </div>
        `;
      }).join("");

      if(!selectedConvId){
        openConv(items[0].id);
      }
    }catch(e){
      list.innerHTML = `<div class="convItem"><span class="small">Error loading inbox.</span></div>`;
    }
  }

  async function openConv(id){
    selectedConvId = id;
    await refreshInbox(); // easy re-render to highlight active
    await loadConvDetail();
  }

  async function loadConvDetail(){
    if(!selectedConvId){
      $("thread").innerHTML = `<div class="small">Select a conversation to view messages.</div>`;
      return;
    }
    const resp = await fetch(`/admin/inbox/conversations/${selectedConvId}`);
    const data = await resp.json();

    const conv = data.conversation;
    const msgs = data.messages || [];

    $("conv_status").value = conv.status || "OPEN";
    $("conv_priority").value = conv.priority || "NORMAL";
    $("assign_to").value = conv.assigned_to_user_id || "";

    const header = `
      <div class="hint" style="margin:0 0 10px;">
        <b>${safe(conv.customer_name) || conv.customer_phone}</b><br/>
        <span class="mono">${conv.customer_phone}</span> • Policy: <span class="mono">${safe(conv.policy_number)}</span> • Channel: <span class="mono">${safe(conv.channel)}</span>
      </div>
    `;

    const body = msgs.map(m => {
      const cls = (m.direction || "").toLowerCase();
      const who = m.actor_name ? `${m.direction} • ${m.actor_name}` : `${m.direction}`;
      return `
        <div class="msg ${cls}">
          <div class="msgHead">
            <div>${who}</div>
            <div class="mono">${safe(m.created_at)}</div>
          </div>
          <div>${escapeHtml(m.body)}</div>
        </div>
      `;
    }).join("");

    $("thread").innerHTML = header + (body || `<div class="small">No messages in this conversation yet.</div>`);
    $("thread").scrollTop = $("thread").scrollHeight;
  }

  function escapeHtml(text){
    const div = document.createElement("div");
    div.innerText = text || "";
    return div.innerHTML;
  }

  async function saveConvMeta(){
    if(!selectedConvId){ alert("Select a conversation"); return; }
    const assigned = $("assign_to").value;
    const status = $("conv_status").value;
    const priority = $("conv_priority").value;

    const payload = { assigned_to_user_id: assigned ? assigned : "", status, priority };

    const resp = await fetch(`/admin/inbox/conversations/${selectedConvId}/assign`, {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify(payload)
    });

    if(!resp.ok){
      const d = await resp.json();
      alert("Failed: " + (d.detail || "error"));
      return;
    }
    await refreshInbox();
    await loadConvDetail();
  }

  async function sendMessage(){
    if(!selectedConvId){ alert("Select a conversation"); return; }
    const body = $("compose_body").value.trim();
    if(!body){ alert("Type a message"); return; }

    const direction = $("compose_type").value;
    const actor_user_id = $("acting_user").value;
    const payload = { actor_user_id, direction, body };

    const resp = await fetch(`/admin/inbox/conversations/${selectedConvId}/send`, {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify(payload)
    });

    if(!resp.ok){
      const d = await resp.json();
      alert("Failed: " + (d.detail || "error"));
      return;
    }

    $("compose_body").value = "";
    await loadConvDetail();
    await refreshInbox();
  }

  async function refreshAll(){
    await refreshPolicies();
    await refreshAudit();
    await refreshInbox();
  }

  async function detectDb(){
    const pill = $("dbpill");
    const v = "{{DBURL}}";
    pill.textContent = "DB: " + (v.startsWith("sqlite") ? "SQLite" : "Postgres");
  }

  (async () => {
    await detectDb();
    await loadTeam();
    await refreshPolicies();
    await refreshAudit();
    await refreshInbox();
  })();


  async function uploadPoliciesExcel(dryRun){
    const f = $("policies_excel");
    const out = $("policies_upload_result");
    out.style.display = "none";
    if (!f || !f.files || !f.files[0]){
      out.style.display = "block";
      out.innerHTML = '<div class="resultTitle"><div><b>Upload</b></div><span class="badge bad">No file</span></div><div class="mono">Please choose an .xlsx file first.</div>';
      return;
    }
    const fd = new FormData();
    fd.append("file", f.files[0]);
    fd.append("dry_run", dryRun ? "true" : "false");

    try{
      const r = await fetch('/admin/policies/upload', { method:'POST', body: fd });
      const j = await r.json();
      const ok = r.ok && j && j.ok;
      out.style.display = "block";
      out.innerHTML = `
        <div class="resultTitle">
          <div><b>Upload Policies</b></div>
          <span class="badge ${ok ? 'good' : 'bad'}">${ok ? (dryRun ? 'DRY RUN' : 'OK') : 'ERROR'}</span>
        </div>
        <div class="mono">${escapeHtml(JSON.stringify(j, null, 2))}</div>
      `;
      if(ok && !dryRun){
        refreshPolicies();
      }
    }catch(e){
      out.style.display = "block";
      out.innerHTML = '<div class="resultTitle"><div><b>Upload Policies</b></div><span class="badge bad">ERROR</span></div><div class="mono">'+escapeHtml(String(e))+'</div>';
    }
  }

</script>
</body>
</html>
"""


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    html = DASHBOARD_HTML.replace("{{DBURL}}", DATABASE_URL)
    return HTMLResponse(content=html, status_code=200)


@app.get("/privacy", response_class=HTMLResponse)
def privacy_policy():
    return """
    <html>
    <head><title>Privacy Policy</title></head>
    <body style="font-family: Arial; max-width: 800px; margin: auto;">
        <h1>Privacy Policy</h1>

        <p>This application uses WhatsApp Cloud API to send and receive messages
        on behalf of the business.</p>

        <h3>Data We Collect</h3>
        <ul>
            <li>Phone number</li>
            <li>Message content sent by users</li>
        </ul>

        <h3>How We Use Data</h3>
        <ul>
            <li>To respond to user queries</li>
            <li>To provide customer support</li>
        </ul>

        <h3>Data Storage</h3>
        <p>Messages are processed in real-time and are not sold or shared with third parties.</p>

        <h3>Contact</h3>
        <p>Email: support@nathinvestments.com</p>

        <p>Last updated: 2025</p>
    </body>
    </html>
    """



@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(
        """
        <html><body style="font-family:system-ui;padding:24px">
          <h2>Policy Lookup + Team Inbox (WhatsApp Delivery) is running</h2>
          <ul>
            <li><a href="/dashboard">Open Dashboard</a></li>
            <li><code>GET/POST /webhook</code> (Meta WhatsApp webhook)</li>
            <li><code>POST /policy/lookup</code></li>
            <li><code>POST /admin/inbox/conversations/&lt;id&gt;/send</code> (delivers if OUT)</li>
          </ul>
        </body></html>
        """
    )
