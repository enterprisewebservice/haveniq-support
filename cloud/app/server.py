"""haveniq-cloud — HavenIQ's (mock) customer and device cloud, and the web front of the voice agent.

Two faces on one port:
  * MCP (streamable HTTP on /mcp): the tools the support agent calls THROUGH the platform's MCP gateway —
    account lookup by phone or account number, orders, devices and their live state. The gateway
    authorizes and logs every call; this service never sees a caller credential.
  * Web: the caller page (/call), the human agent's join page (/desk/join), and the token routes that put
    either of them into a LiveKit room. The caller's token asks LiveKit to dispatch the voice worker.

The data is a JSON seed (the exercise allows an in-memory or file store). Nothing here is production-grade
on purpose; the shape of the calls is what matters.
"""
import json
import os
import re
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from mcp.server.fastmcp import FastMCP

PORT = int(os.environ.get("PORT", "8080"))
ROOT = Path(__file__).resolve().parent.parent
DATA = json.loads((ROOT / "data" / "seed.json").read_text())
AGENT_NAME = os.environ.get("AGENT_NAME", "haveniq-support")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")

# ----------------------------------------------------------------------------- data helpers
def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")

def _account(account_number: str):
    key = (account_number or "").strip().upper().replace(" ", "")
    for a in DATA["accounts"]:
        if a["account_number"].upper() == key:
            return a
    return None

def _by_phone(phone: str):
    d = _digits(phone)[-10:]
    if len(d) < 7:
        return None
    for a in DATA["accounts"]:
        if _digits(a["phone"]).endswith(d):
            return a
    return None

def _summary(a):
    last = max(a["orders"], key=lambda o: o["placed"]) if a["orders"] else None
    return {
        "account_number": a["account_number"], "name": a["name"], "phone": a["phone"], "email": a["email"],
        "subscription": a["subscription"], "device_count": len(a["devices"]),
        "devices_offline": [d["name"] for d in a["devices"] if not d["online"]],
        "alerts": [d["name"] for d in a["devices"] if d["state"].get("alert")],
        "latest_order": {"order_id": last["order_id"], "status": last["status"], "eta": last.get("eta")} if last else None,
    }

# ----------------------------------------------------------------------------- MCP tools
mcp = FastMCP("haveniq-cloud", host="0.0.0.0", port=PORT, stateless_http=True)

@mcp.tool()
def find_account(phone: str = "", account_number: str = "") -> dict:
    """Look up a HavenIQ customer by phone number OR account number (format HQ-12345). Returns the account
    summary: name, subscription status, device count, anything offline or alerting, and the latest order.
    Use this first, then greet the customer by name."""
    a = _account(account_number) if account_number else None
    if not a and phone:
        a = _by_phone(phone)
    if not a:
        return {"found": False, "message": "No account matches. Ask the customer for their account number (it starts with HQ-)."}
    return {"found": True, **_summary(a)}

@mcp.tool()
def get_orders(account_number: str) -> dict:
    """Recent orders for an account: status, carrier, tracking number and delivery estimate."""
    a = _account(account_number)
    if not a:
        return {"found": False}
    return {"found": True, "orders": sorted(a["orders"], key=lambda o: o["placed"], reverse=True)}

@mcp.tool()
def get_devices(account_number: str) -> dict:
    """All registered devices on an account with room, online status and current state."""
    a = _account(account_number)
    if not a:
        return {"found": False}
    return {"found": True, "devices": a["devices"]}

@mcp.tool()
def device_state(account_number: str, query: str) -> dict:
    """Live state of one device, found by room or name or type, e.g. 'living room', 'thermostat',
    'front door camera'. Returns online status and the state (temperature, mode, lock, battery, alerts)."""
    a = _account(account_number)
    if not a:
        return {"found": False}
    q = (query or "").lower()
    words = [w for w in re.split(r"[^a-z0-9]+", q) if w]
    best, score = None, 0
    for d in a["devices"]:
        hay = f"{d['name']} {d['room']} {d['type']}".lower()
        s = sum(1 for w in words if w in hay)
        if s > score:
            best, score = d, s
    if not best:
        return {"found": False, "message": "No device matches that description.", "devices": [d["name"] for d in a["devices"]]}
    return {"found": True, "device": best}

@mcp.tool()
def set_thermostat(account_number: str, target_f: int, room: str = "living room") -> dict:
    """Change a thermostat's target temperature (Fahrenheit). Confirms the new setting. Only for
    thermostats that are online."""
    a = _account(account_number)
    if not a:
        return {"ok": False, "message": "No such account."}
    r = (room or "").lower()
    for d in a["devices"]:
        if d["type"] == "thermostat" and (r in d["room"].lower() or r in d["name"].lower()):
            if not d["online"]:
                return {"ok": False, "message": f"{d['name']} is offline (last seen {d['state'].get('last_seen')}); the change cannot be applied."}
            if not 50 <= int(target_f) <= 90:
                return {"ok": False, "message": "Target must be between 50 and 90 F."}
            d["state"]["target_f"] = int(target_f)
            return {"ok": True, "device": d["name"], "target_f": d["state"]["target_f"]}
    return {"ok": False, "message": "No thermostat in that room."}

# ----------------------------------------------------------------------------- web
app = FastAPI(title="haveniq-cloud", lifespan=lambda _app: mcp.session_manager.run())
HANDOFFS: dict[str, dict] = {}   # room -> what the human needs to see (in-memory, this is a demo)

@app.get("/healthz")
def healthz():
    return {"status": "ok", "service": "haveniq-cloud", "accounts": len(DATA["accounts"])}

def _lk():
    from livekit import api
    url, key, secret = os.environ.get("LIVEKIT_URL"), os.environ.get("LIVEKIT_API_KEY"), os.environ.get("LIVEKIT_API_SECRET")
    if not (url and key and secret):
        raise HTTPException(503, "LiveKit is not configured")
    return api, url, key, secret

@app.post("/api/call/token")
async def call_token(req: Request):
    """A new call: one room per call, the caller's token creates it and asks LiveKit to dispatch the voice
    worker into it. The phone number the caller typed (optional) rides along as dispatch metadata so the
    agent can greet by name without asking."""
    api, url, key, secret = _lk()
    body = {}
    try:
        body = await req.json()
    except Exception:
        pass
    phone = str(body.get("phone", ""))[:32]
    room = "call-" + secrets.token_hex(4)
    identity = "caller-" + secrets.token_hex(3)
    at = api.AccessToken(key, secret).with_identity(identity).with_name("Caller").with_ttl(__import__("datetime").timedelta(hours=1))
    at = at.with_grants(api.VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True, can_publish_data=True))
    at = at.with_room_config(api.RoomConfiguration(
        empty_timeout=120,
        agents=[api.RoomAgentDispatch(agent_name=AGENT_NAME, metadata=json.dumps({"phone": phone, "started": int(time.time())}))],
    ))
    return {"ok": True, "url": url, "token": at.to_jwt(), "room": room, "identity": identity}

@app.post("/api/handoff")
async def register_handoff(req: Request):
    """The voice worker registers what the human needs to see when they join: summary, account, ticket."""
    body = await req.json()
    room = str(body.get("room", ""))
    if not room:
        raise HTTPException(400, "room required")
    HANDOFFS[room] = {**body, "registered": int(time.time())}
    return {"ok": True, "join_url": f"{PUBLIC_URL}/desk/join?room={room}"}

@app.get("/api/handoff")
def get_handoff(room: str):
    return HANDOFFS.get(room) or {"room": room, "summary": "(no summary registered for this room)"}

@app.post("/api/desk/token")
async def desk_token(req: Request):
    """The human agent joins the caller's room. Identity prefix 'human-' is how the worker knows a person arrived."""
    api, url, key, secret = _lk()
    body = await req.json()
    room = str(body.get("room", ""))
    name = re.sub(r"[^A-Za-z0-9 _-]", "", str(body.get("name", "Support agent")))[:40] or "Support agent"
    if not re.fullmatch(r"call-[0-9a-f]{8}", room):
        raise HTTPException(400, "bad room")
    at = api.AccessToken(key, secret).with_identity("human-" + secrets.token_hex(3)).with_name(name).with_ttl(__import__("datetime").timedelta(hours=1))
    at = at.with_grants(api.VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True, can_publish_data=True))
    return {"ok": True, "url": url, "token": at.to_jwt(), "room": room}

@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "call.html")

@app.get("/call")
def call_page():
    return FileResponse(ROOT / "static" / "call.html")

@app.get("/desk/join")
def desk_page():
    return FileResponse(ROOT / "static" / "desk.html")

# the MCP app is mounted last so the REST routes above win
app.mount("/", mcp.streamable_http_app())

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
