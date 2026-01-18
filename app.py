import os
import re
from io import BytesIO
import uuid
from datetime import datetime, date
from typing import Optional

import requests
try:
    import openpyxl  # for Excel upload
except Exception:
    openpyxl = None

from fastapi import FastAPI, Depends, HTTPException, Request, UploadFile, File
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

# OpenAI (optional - only used as very last fallback if needed)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
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


# ────────────────────────────────────────────────────────────────
# MAIN LOGIC - SIMPLIFIED & DETERMINISTIC POLICY LOOKUP
# ────────────────────────────────────────────────────────────────
def generate_reply(
    *,
    customer_phone: str,
    customer_name: str | None,
    user_text: str,
    policy_number: str | None,  # usually the extracted/cleaned number
    db: Session
) -> str:
    """
    Simple, predictable reply logic:
    1. If message looks like policy number → check DB immediately
    2. Then handle menu options
    3. Fallback to friendly menu prompt
    """
    txt = (user_text or "").strip()
    name = (customer_name or "").strip()
    prefix = f"Hi {name}! " if name else "Hi! "

    # ── 1. Primary case: User sent what looks like a policy number ──
    if POLICY_RE.match(txt) and 6 <= len(txt) <= 20:
        policy = db.execute(
            select(Policy).where(Policy.policy_number == txt)
        ).scalars().first()

        if policy:
            # Build summary (customize fields according to your actual Policy model)
            start_date = getattr(policy, 'start_date', None) or getattr(policy, 'created_at', None)
            total_paid = 0.0
            try:
                payments = db.execute(
                    select(Payment).where(Payment.policy_id == policy.id)
                ).scalars().all()
                for p in payments:
                    status = (getattr(p, 'status', '') or '').upper()
                    if status in ('PAID', 'SUCCESS', 'COMPLETED', ''):
                        total_paid += float(getattr(p, 'amount', 0))
            except Exception:
                total_paid = 0.0

            summary = (
                f"{prefix}Here are the details for policy **{txt}**:\n\n"
                f"• Policyholder: {getattr(policy, 'name', 'N/A')}\n"
                f"• Premium Amount: ₹{getattr(policy, 'premium_amount', 'N/A')}\n"
                f"• Next Due Date: {getattr(policy, 'due_date', 'N/A')}\n"
                f"• Start Date: {start_date.strftime('%d-%m-%Y') if start_date else 'N/A'}\n"
                f"• Total Premium Paid: ₹{total_paid:,.2f}\n\n"
                "Reply with:\n"
                "1 → About Nath Investments & our services\n"
                "2 → Know your policy details\n"
                "3 → Talk to our human agent"
            )
            return summary

        else:
            return (
                f"{prefix}Sorry, we couldn't find any policy with number **{txt}**.\n\n"
                "Please double-check the number and try again.\n\n"
                "Or choose:\n"
                "1 → About us\n"
                "2 → Check another policy\n"
                "3 → Speak to agent"
            )

    # ── 2. Menu options ──
    txt_lower = txt.lower()

    if re.search(r"\b1\b", txt_lower):
        return (
            f"{prefix}Nath Investments is a financial firm specializing in:\n\n"
            "✅ LIC Services: New policy guidance, premium reminders, revival, maturity/claim help\n"
            "✅ Mutual Funds: SIP/lumpsum guidance, KYC, portfolio review (no guaranteed returns)\n\n"
            "Tell me what you're interested in!\n\n"
            "Or choose:\n1 → About us\n2 → Policy details\n3 → Human agent"
        )

    if re.search(r"\b3\b", txt_lower):
        return (
            f"Sure {name or 'friend'}! "
            "I'm connecting you to a human agent right away.\n"
            "Please hold on for a moment or share more details in the meantime."
        )

    # ── 3. Default friendly prompt ──
    return (
        f"{prefix}How can I help you today?\n\n"
        "Please reply with:\n"
        "1 → About Nath Investments & our services\n"
        "2 → Know your policy details (or simply send your policy number)\n"
        "3 → Talk to our human agent\n\n"
        "You can also just send your policy number directly!"
    )


# ────────────────────────────────────────────────────────────────
# DASHBOARD HTML (unchanged)
# ────────────────────────────────────────────────────────────────

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Nath Investments - Admin Dashboard</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://unpkg.com/htmx.org@1.9.10"></script>
  <script>
    tailwind.config = {
      theme: {
        extend: {
          colors: {
            primary: '#4f46e5',
          }
        }
      }
    }
  </script>
  <style>
    body { font-family: system-ui, sans-serif; }
    .msg { margin: 8px 0; padding: 10px; border-radius: 8px; max-width: 80%; }
    .msg.in { background: #e5e7eb; align-self: flex-start; }
    .msg.out { background: #dcf8c6; align-self: flex-end; text-align: right; }
    .mono { font-family: monospace; font-size: 0.8rem; color: #666; }
    .pill { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 0.75rem; }
  </style>
</head>
<body class="bg-gray-100 min-h-screen">
  <div class="container mx-auto p-4 max-w-6xl">
    <header class="flex justify-between items-center mb-6">
      <h1 class="text-3xl font-bold text-primary">Nath Investments Admin</h1>
      <div id="dbpill" class="pill bg-gray-200"></div>
    </header>

    <!-- Tabs -->
    <div class="tabs flex border-b mb-6">
      <button class="tab px-6 py-3 font-medium border-b-2 border-primary text-primary" onclick="showTab('policies')">Policies</button>
      <button class="tab px-6 py-3 font-medium" onclick="showTab('inbox')">Inbox</button>
      <button class="tab px-6 py-3 font-medium" onclick="showTab('reminders')">Reminders</button>
      <button class="tab px-6 py-3 font-medium" onclick="showTab('audit')">Audit</button>
    </div>

    <!-- Policies Tab -->
    <div id="policies" class="tab-content">
      <h2 class="text-2xl font-bold mb-4">Policy Records</h2>
      <div id="policies-list" class="bg-white rounded shadow overflow-hidden"></div>
    </div>

    <!-- Inbox Tab -->
    <div id="inbox" class="tab-content hidden">
      <div class="grid grid-cols-1 md:grid-cols-3 gap-6">
        <!-- Conversations List -->
        <div class="md:col-span-1 bg-white rounded shadow p-4">
          <h3 class="text-xl font-bold mb-4">Conversations</h3>
          <div id="conversations" class="space-y-2 max-h-[70vh] overflow-y-auto"></div>
        </div>

        <!-- Chat View -->
        <div class="md:col-span-2 bg-white rounded shadow p-4 flex flex-col">
          <div id="thread-header" class="font-bold text-lg mb-2"></div>
          <div id="thread" class="flex-1 overflow-y-auto border rounded p-4 bg-gray-50 mb-4"></div>
          
          <!-- Compose -->
          <div class="flex gap-2">
            <select id="compose_type" class="border rounded px-2 py-1">
              <option value="OUT">Send</option>
              <option value="IN">Simulate Inbound</option>
            </select>
            <input id="compose_body" type="text" class="flex-1 border rounded px-3 py-2" placeholder="Type message..."/>
            <button onclick="sendMessage()" class="bg-primary text-white px-4 py-2 rounded">Send</button>
          </div>
        </div>
      </div>
    </div>

    <!-- Reminders Tab -->
    <div id="reminders" class="tab-content hidden">
      <h2 class="text-2xl font-bold mb-4">Premium Reminders</h2>
      
      <div class="bg-white p-6 rounded shadow">
        <h3 class="text-xl mb-4">Run DB Scan</h3>
        <div class="flex gap-4 mb-4">
          <input id="rem_days" type="number" value="3" class="border rounded px-3 py-2 w-24" placeholder="Days ahead"/>
          <button onclick="runDbReminders()" class="bg-primary text-white px-6 py-2 rounded">Run Now</button>
        </div>
        <pre id="rem_out" class="bg-gray-100 p-4 rounded overflow-auto max-h-60"></pre>
      </div>
    </div>

    <!-- Audit Tab -->
    <div id="audit" class="tab-content hidden">
      <h2 class="text-2xl font-bold mb-4">Audit Log</h2>
      <div id="audit-log" class="bg-white rounded shadow p-4"></div>
    </div>
  </div>

  <script>
    let selectedConvId = null;

    function $(id) { return document.getElementById(id); }

    function showTab(tabId) {
      document.querySelectorAll('.tab-content').forEach(el => el.classList.add('hidden'));
      document.querySelectorAll('.tab').forEach(el => el.classList.remove('border-b-2', 'border-primary', 'text-primary'));
      $(tabId).classList.remove('hidden');
      document.querySelector(`button[onclick="showTab('${tabId}')"]`).classList.add('border-b-2', 'border-primary', 'text-primary');
    }

    async function refreshPolicies() {
      const resp = await fetch('/admin/policies');
      const html = await resp.text();
      $('policies-list').innerHTML = html;
    }

    async function refreshInbox() {
      const resp = await fetch('/admin/inbox');
      const html = await resp.text();
      $('conversations').innerHTML = html;
    }

    async function loadConvDetail(id = selectedConvId) {
      if (!id) return;
      selectedConvId = id;
      const resp = await fetch(`/admin/inbox/conversations/${id}`);
      const data = await resp.json();
      
      $('thread-header').innerHTML = `Conversation with ${data.customer_phone} ${data.customer_name ? `(${data.customer_name})` : ''}`;
      
      const msgs = data.messages || [];
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

      $('thread').innerHTML = body || `<div class="small">No messages in this conversation yet.</div>`;
      $('thread').scrollTop = $('thread').scrollHeight;
    }

    function escapeHtml(text){
      const div = document.createElement("div");
      div.innerText = text || "";
      return div.innerHTML;
    }

    function safe(text) {
      return escapeHtml(text || '');
    }

    async function sendMessage(){
      if(!selectedConvId){ alert("Select a conversation"); return; }
      const body = $("compose_body").value.trim();
      if(!body){ alert("Type a message"); return; }

      const direction = $("compose_type").value;
      const payload = { direction, body };

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

    async function runDbReminders(){
      const out = $("rem_out");
      out.innerHTML = "Running DB reminder scan…";
      const days = ($("rem_days").value || "3").trim();
      try{
        const resp = await fetch(`/admin/reminders/run?days_ahead=${encodeURIComponent(days)}&dry_run=false`, {method:"POST"});
        const data = await resp.json();
        if(!resp.ok){
          out.innerHTML = "Failed: " + escapeHtml(data.detail || "error");
          return;
        }
        out.innerHTML = `Done. Scanned: <b>${data.scanned}</b>, Sent: <b>${data.sent}</b>, Skipped: <b>${data.skipped}</b>, Errors: <b>${data.errors}</b>.`;
        await refreshInbox();
      }catch(e){
        out.innerHTML = "Network error: " + escapeHtml(String(e));
      }
    }

    (async () => {
      await refreshPolicies();
      await refreshInbox();
      showTab('policies');
    })();
  </script>
</body>
</html>
"""

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    html = DASHBOARD_HTML
    return HTMLResponse(content=html, status_code=200)


@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(
        """
        <html><body style="font-family:system-ui;padding:24px">
         <h2>Policy Lookup + Team Inbox (WhatsApp Delivery) is running</h2>
         <ul>
          <li><a href="/dashboard">Open Dashboard</a></li>
          <li><code>GET/POST /webhook</code> (Meta WhatsApp webhook)</li>
         </ul>
        </body></html>
        """
    )


# Note: Add your remaining routes here (webhook, admin endpoints, models, etc.)
# For example:
# @app.post("/webhook")
# async def webhook(...):
#     ...
#     reply = generate_reply(
#         customer_phone=from_number,
#         customer_name=name,
#         user_text=message_text,
#         policy_number=policy_number,
#         db=db
#     )
#     send_whatsapp_text(from_number, reply)
#     ...

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)