"""haveniq-crm — the bridge between the voice agent and the humans' desk (Zammad), plus the handoff signal.

MCP tools (through the platform's MCP gateway, so every call is authorized and logged):
  crm_find_customer, crm_open_ticket, crm_add_note, crm_request_handoff, crm_get_ticket
REST (service to service, used by the voice worker after the call): /api/tickets/{id}/recording

This service holds the Zammad API token and the Mattermost token; the agent never does. Recordings are
objects in the cluster's NooBaa bucket; the bridge presigns a link and attaches it to the ticket, and, for
short calls, attaches the audio file itself so the ticket is self-contained.
"""
import base64
import datetime as dt
import json
import os
import re
import time

import requests
from fastapi import FastAPI, HTTPException, Request
from mcp.server.fastmcp import FastMCP

PORT = int(os.environ.get("PORT", "8080"))
ZAMMAD = os.environ.get("ZAMMAD_URL", "http://zammad-nginx.haveniq.svc:8080").rstrip("/")
ZAMMAD_PUBLIC = os.environ.get("ZAMMAD_PUBLIC_URL", ZAMMAD).rstrip("/")
ZTOKEN = os.environ.get("ZAMMAD_TOKEN", "")
ZGROUP = os.environ.get("ZAMMAD_GROUP", "Users")
MM_URL = os.environ.get("MM_URL", "").rstrip("/")
MM_TOKEN = os.environ.get("MM_TOKEN", "")
MM_TEAM = os.environ.get("MM_TEAM", "agents")
MM_CHANNEL = os.environ.get("MM_CHANNEL", "haveniq-support")
DESK_URL = os.environ.get("DESK_URL", "").rstrip("/")
S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "")
S3_BUCKET = os.environ.get("S3_BUCKET", "")
ATTACH_MAX_MB = float(os.environ.get("ATTACH_MAX_MB", "12"))

def z(method, path, **kw):
    if not ZTOKEN:
        raise RuntimeError("ZAMMAD_TOKEN is not configured")
    r = requests.request(method, f"{ZAMMAD}/api/v1{path}", headers={"Authorization": f"Token token={ZTOKEN}"}, timeout=30, **kw)
    if r.status_code >= 400:
        raise RuntimeError(f"zammad {method} {path}: {r.status_code} {r.text[:300]}")
    return r.json() if r.text else {}

def ticket_url(tid) -> str:
    return f"{ZAMMAD_PUBLIC}/#ticket/zoom/{tid}"

def mm_post(text: str) -> bool:
    """Post into the support channel. Creates the channel in the team on first use. Never raises."""
    if not (MM_URL and MM_TOKEN):
        return False
    try:
        h = {"Authorization": f"Bearer {MM_TOKEN}"}
        team = requests.get(f"{MM_URL}/api/v4/teams/name/{MM_TEAM}", headers=h, timeout=10).json()
        ch = requests.get(f"{MM_URL}/api/v4/teams/{team['id']}/channels/name/{MM_CHANNEL}", headers=h, timeout=10)
        if ch.status_code == 404:
            ch = requests.post(f"{MM_URL}/api/v4/channels", headers=h, timeout=10,
                               json={"team_id": team["id"], "name": MM_CHANNEL, "display_name": "HavenIQ support", "type": "O"})
        cid = ch.json()["id"]
        requests.post(f"{MM_URL}/api/v4/posts", headers=h, timeout=10, json={"channel_id": cid, "message": text})
        return True
    except Exception as e:  # the handoff must never fail because chat is down
        print("mattermost post failed:", e)
        return False

def presign(key: str, hours: int = 72) -> str:
    if not (S3_ENDPOINT and S3_BUCKET):
        return ""
    import boto3
    from botocore.config import Config
    s3 = boto3.client("s3", endpoint_url=S3_ENDPOINT, aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
                      aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"), region_name="us-east-1",
                      config=Config(signature_version="s3v4", s3={"addressing_style": "path"}), verify=False)
    return s3.generate_presigned_url("get_object", Params={"Bucket": S3_BUCKET, "Key": key}, ExpiresIn=hours * 3600)

# ----------------------------------------------------------------------------- customers
def find_customer_record(phone: str = "", account_number: str = "", email: str = ""):
    """Find a customer by email (exact), phone (digits), or account number (kept in the user's note).
    Zammad's database search tokenizes the query, so each candidate query is checked against the records it returns."""
    digits = re.sub(r"\D", "", phone or "")
    candidates = []
    if email:
        candidates.append((email, lambda u: (u.get("email") or "").lower() == email.lower()))
        candidates.append((email.split("@")[0], lambda u: (u.get("email") or "").lower() == email.lower()))
    if digits:
        candidates.append((digits[-7:], lambda u: re.sub(r"\D", "", u.get("phone") or "").endswith(digits[-7:])))
    if account_number:
        candidates.append((account_number, lambda u: account_number.upper() in (u.get("note") or "").upper()))
    for q, ok in candidates:
        try:
            for u in z("GET", "/users/search", params={"query": q, "limit": 10}):
                if ok(u):
                    return u
        except Exception:
            continue
    return None

def ensure_customer(name: str, phone: str, email: str, account_number: str):
    u = find_customer_record(email=email) if email else None
    if not u and phone:
        u = find_customer_record(phone=phone)
    if u:
        return u
    first, _, last = (name or "HavenIQ Customer").partition(" ")
    try:
        return z("POST", "/users", json={"firstname": first, "lastname": last or "", "email": email or f"{account_number.lower()}@customers.haveniq.example",
                                         "phone": phone, "note": f"HavenIQ account {account_number}", "roles": ["Customer"]})
    except RuntimeError as e:
        if "already used" in str(e) or "422" in str(e):   # created moments ago by a parallel call: find it again
            u = find_customer_record(phone, account_number, email)
            if u:
                return u
        raise

# ----------------------------------------------------------------------------- MCP tools
mcp = FastMCP("haveniq-crm", host="0.0.0.0", port=PORT, stateless_http=True)

@mcp.tool()
def find_customer(phone: str = "", account_number: str = "", email: str = "") -> dict:
    """Find the customer's record in the support desk (Zammad) by phone, HavenIQ account number or email.
    Returns the desk's customer id and open tickets, or found=false."""
    try:
        u = find_customer_record(phone, account_number, email)
    except Exception as e:
        return {"found": False, "error": str(e)}
    if not u:
        return {"found": False}
    open_t = z("GET", "/tickets/search", params={"query": f"customer.id:{u['id']} AND state.name:(new OR open OR pending reminder)", "limit": 5})
    return {"found": True, "customer_id": u["id"], "name": f"{u.get('firstname','')} {u.get('lastname','')}".strip(),
            "open_tickets": [{"id": t["id"], "title": t["title"], "state": t.get("state")} for t in open_t] if isinstance(open_t, list) else []}

@mcp.tool()
def open_ticket(caller_name: str, account_number: str, title: str, summary: str, phone: str = "", email: str = "", room: str = "") -> dict:
    """Open a support ticket for this call in the humans' desk. `summary` is what a colleague needs to pick
    the conversation up: who called, what they asked, what was checked, what is still open. Returns the
    ticket id and a link. Call this once per call, before any handoff."""
    try:
        cust = ensure_customer(caller_name, phone, email, account_number)
        body = f"<p><b>Account:</b> {account_number} &middot; <b>Caller:</b> {caller_name} {('&middot; ' + phone) if phone else ''}</p>" \
               f"<p><b>Call summary (voice agent):</b></p><p>{summary.replace(chr(10), '<br>')}</p>" + \
               (f"<p><b>Live room:</b> {room}</p>" if room else "")
        t = z("POST", "/tickets", json={"title": title[:150], "group": ZGROUP, "customer_id": cust["id"],
                                        "article": {"subject": "Voice agent call summary", "body": body, "content_type": "text/html", "type": "note", "internal": False}})
        return {"ok": True, "ticket_id": t["id"], "ticket_number": t.get("number"), "ticket_url": ticket_url(t["id"])}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@mcp.tool()
def add_note(ticket_id: int, note: str) -> dict:
    """Add an internal note to an existing ticket (what you found, what you changed, what the customer decided)."""
    try:
        z("POST", "/ticket_articles", json={"ticket_id": int(ticket_id), "body": note, "type": "note", "internal": True})
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@mcp.tool()
def request_handoff(room: str, summary: str, ticket_id: int = 0, reason: str = "customer asked for a person") -> dict:
    """Hand the live call to a human: pages the support channel with the summary and a one-click link to
    join the caller's room, and, when a ticket exists, raises its priority and records why. Call this after
    open_ticket (pass its ticket_id; pass 0 if the ticket could not be opened), then tell the caller a person
    is joining and stop talking."""
    join = f"{DESK_URL}/desk/join?room={room}" if DESK_URL else ""
    desk_note = ""
    if ticket_id:
        try:
            z("PUT", f"/tickets/{int(ticket_id)}", json={"state": "open", "priority": "3 high"})
            z("POST", "/ticket_articles", json={"ticket_id": int(ticket_id), "type": "note", "internal": True, "content_type": "text/html",
                                                "body": f"<p><b>Handoff requested:</b> {reason}</p><p>{summary.replace(chr(10), '<br>')}</p>" + (f'<p><a href="{join}">Join the live call</a></p>' if join else "")})
        except Exception as e:
            desk_note = f"ticket update failed: {e}"
    posted = mm_post(f"**Caller waiting for a person** ({reason})\n{summary}\n\n" + (f"Ticket: {ticket_url(ticket_id)}\n" if ticket_id else "No ticket could be opened; summary above.\n") + (f"Join the call: {join}" if join else ""))
    return {"ok": True, "ticket_url": ticket_url(ticket_id) if ticket_id else "", "join_url": join, "paged_channel": posted, **({"warning": desk_note} if desk_note else {})}

@mcp.tool()
def get_ticket(ticket_id: int) -> dict:
    """Read a ticket's title, state, priority and last notes."""
    try:
        t = z("GET", f"/tickets/{int(ticket_id)}")
        arts = z("GET", f"/ticket_articles/by_ticket/{int(ticket_id)}")
        return {"ok": True, "ticket": {k: t.get(k) for k in ("id", "number", "title", "state_id", "priority_id", "created_at", "updated_at")},
                "notes": [{"from": a.get("from"), "created_at": a.get("created_at"), "body": re.sub("<[^>]+>", " ", a.get("body", ""))[:600]} for a in arts[-5:]]}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ----------------------------------------------------------------------------- REST (worker → bridge, after the call)
app = FastAPI(title="haveniq-crm", lifespan=lambda _app: mcp.session_manager.run())

@app.get("/healthz")
def healthz():
    return {"status": "ok", "service": "haveniq-crm", "zammad": bool(ZTOKEN), "mattermost": bool(MM_TOKEN), "bucket": bool(S3_BUCKET)}

@app.post("/api/tickets/{ticket_id}/recording")
async def attach_recording(ticket_id: int, req: Request):
    """The worker reports the finished recording: object key in the bucket, duration, room. The bridge
    presigns a 72-hour link, attaches it to the ticket, attaches the file itself when it is small enough,
    and updates the support channel."""
    body = await req.json()
    key, room, seconds = str(body.get("key", "")), str(body.get("room", "")), int(body.get("seconds", 0) or 0)
    if not key:
        raise HTTPException(400, "key required")
    url = presign(key)
    attachments = []
    try:
        if S3_ENDPOINT and S3_BUCKET:
            import boto3
            from botocore.config import Config
            s3 = boto3.client("s3", endpoint_url=S3_ENDPOINT, aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
                              aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"), region_name="us-east-1",
                              config=Config(signature_version="s3v4", s3={"addressing_style": "path"}), verify=False)
            head = s3.head_object(Bucket=S3_BUCKET, Key=key)
            if head["ContentLength"] <= ATTACH_MAX_MB * 1024 * 1024:
                data = s3.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()
                attachments = [{"filename": key.rsplit("/", 1)[-1], "data": base64.b64encode(data).decode(), "mime-type": "audio/mp4"}]
    except Exception as e:
        print("recording fetch failed:", e)
    try:
        z("POST", "/ticket_articles", json={"ticket_id": int(ticket_id), "type": "note", "internal": True, "content_type": "text/html",
                                            "body": f"<p><b>Call recording</b> ({seconds // 60} min {seconds % 60} s, room {room}).</p>" +
                                                    (f'<p><a href="{url}">Play / download (link valid 72 h)</a></p>' if url else "") +
                                                    f"<p>Object: <code>{S3_BUCKET}/{key}</code></p>",
                                            "attachments": attachments})
    except Exception as e:
        raise HTTPException(502, f"zammad: {e}")
    mm_post(f"Recording attached to {ticket_url(ticket_id)} ({seconds // 60} min {seconds % 60} s)" + (f": {url}" if url else ""))
    return {"ok": True, "ticket_url": ticket_url(ticket_id), "recording_url": url, "attached_file": bool(attachments)}

app.mount("/", mcp.streamable_http_app())

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
