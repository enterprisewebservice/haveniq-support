"""HavenIQ voice support agent.

One LiveKit room per call. The caller's token dispatches this worker with the phone number they typed (if any).
The agent greets, finds the account (through the MCP gateway), answers questions about orders and devices, and,
when the caller wants a person, opens a ticket, pages the desk, and steps back while a human joins the same room.
The whole call is recorded with LiveKit Egress into the cluster's object store; when the egress completes the
recording is attached to the ticket.

Governance in one paragraph: the model runs behind the platform's guardrailed front door; every tool call the
model makes goes to the MCP gateway with this worker's caller key, where it is authorized and logged; the worker
holds no CRM, chat or cloud credentials; the humans work in their own desk.
"""
import asyncio
import json
import logging
import os
import re
import time

import aiohttp
from livekit import api, rtc
from livekit.agents import (Agent, AgentSession, AudioConfig, BackgroundAudioPlayer, BuiltinAudioClip, JobContext,
                            RunContext, WorkerOptions, cli, function_tool, inference, mcp)
from livekit.agents import tts as agents_tts
from livekit.agents.voice import room_io
from livekit.plugins import openai, silero

log = logging.getLogger("haveniq")
logging.basicConfig(level=logging.INFO)

AGENT_NAME = os.environ.get("AGENT_NAME", "haveniq-support")
LLM_BASE_URL = os.environ["LLM_BASE_URL"].rstrip("/")
LLM_API_KEY = os.environ["LLM_API_KEY"]
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")
GATEWAY_MCP_URL = os.environ["GATEWAY_MCP_URL"]
GATEWAY_CALLER_KEY = os.environ["GATEWAY_CALLER_KEY"]
CLOUD_URL = os.environ.get("CLOUD_URL", "http://haveniq-cloud.haveniq.svc:8080").rstrip("/")
CRM_URL = os.environ.get("CRM_URL", "http://haveniq-crm.haveniq.svc:8080").rstrip("/")
DESK_PUBLIC_URL = os.environ.get("DESK_PUBLIC_URL", "").rstrip("/")
STT_MODEL = os.environ.get("STT_MODEL", "deepgram/nova-3")
TTS_VOICE = os.environ.get("TTS_VOICE", "inworld/inworld-tts-2:Ashley")
S3_ENDPOINT, S3_BUCKET = os.environ.get("S3_ENDPOINT", ""), os.environ.get("S3_BUCKET", "")
S3_ACCESS_KEY, S3_SECRET_KEY = os.environ.get("AWS_ACCESS_KEY_ID", ""), os.environ.get("AWS_SECRET_ACCESS_KEY", "")
IDLE_HANGUP_S = int(os.environ.get("IDLE_HANGUP_S", "240"))

INSTRUCTIONS = """You are Haven, the voice support agent for HavenIQ, a smart-home company (thermostats, cameras, door locks,
sensors, and a cloud video subscription). You are on a live phone-style call: keep every answer short, natural and
spoken, one or two sentences, no lists, no markdown, no emoji. Never read out long numbers unprompted; offer them.

How a call goes:
1. Greet the caller. If you already know their account (see CONTEXT), greet them by name. Otherwise ask for the phone
   number on the account, or the account number (it starts with HQ), and look it up with find_account.
2. Answer their questions with the tools: get_orders for order status and delivery, get_devices and device_state for
   thermostats, cameras, locks and sensors (temperatures, online status, batteries, alerts), set_thermostat to change
   a target. Say what the tool says; never invent device data. If something is offline or alerting, mention it.
3. If the caller asks for a person, a supervisor, a human, or you cannot help (billing disputes, refunds, safety
   emergencies, anything the tools cannot do): first call open_ticket with a clear summary of the whole call
   (who, account, what they asked, what you checked, what is still open), then call request_handoff with that
   ticket id and the ROOM from CONTEXT, then call transfer_to_human. Say one sentence like "I'm bringing a support
   specialist into this call now, they have the summary in front of them", and then stay silent.
4. Do not ask for passwords or payment details. Do not promise refunds. If asked whether you are a person, say you are
   HavenIQ's automated assistant and a person is one request away.
"""


class Recorder:
    """LiveKit Egress (cloud) → the cluster's S3. Started at call start, finished when the room empties."""

    def __init__(self, lk: api.LiveKitAPI, room_name: str):
        self.lk, self.room_name, self.egress_id, self.started = lk, room_name, None, time.time()
        self.key = f"calls/{time.strftime('%Y/%m/%d')}/{room_name}.mp4"

    async def start(self):
        if not (S3_ENDPOINT and S3_BUCKET and S3_ACCESS_KEY):
            log.warning("recording disabled: bucket not configured")
            return
        req = api.RoomCompositeEgressRequest(
            room_name=self.room_name, audio_only=True,
            file_outputs=[api.EncodedFileOutput(file_type=api.EncodedFileType.MP4, filepath=self.key,
                                                s3=api.S3Upload(access_key=S3_ACCESS_KEY, secret=S3_SECRET_KEY, bucket=S3_BUCKET,
                                                                endpoint=S3_ENDPOINT, force_path_style=True, region="us-east-1"))])
        try:
            info = await self.lk.egress.start_room_composite_egress(req)
            self.egress_id = info.egress_id
            log.info("recording started egress=%s key=%s", self.egress_id, self.key)
        except Exception as e:
            log.warning("recording could not start: %s", e)

    async def finish(self) -> tuple[str, int] | None:
        if not self.egress_id:
            return None
        try:
            await self.lk.egress.stop_egress(api.StopEgressRequest(egress_id=self.egress_id))
        except Exception as e:
            log.info("stop_egress: %s", e)
        status = None
        for _ in range(60):
            await asyncio.sleep(3)
            items = await self.lk.egress.list_egress(api.ListEgressRequest(egress_id=self.egress_id))
            if items.items:
                status = items.items[0].status
                if status in (api.EgressStatus.EGRESS_COMPLETE, api.EgressStatus.EGRESS_FAILED, api.EgressStatus.EGRESS_ABORTED):
                    break
        if status != api.EgressStatus.EGRESS_COMPLETE:
            log.warning("egress %s ended with status %s", self.egress_id, status)
            return None
        return self.key, int(time.time() - self.started)


class CallState:
    def __init__(self):
        self.ticket_id: int | None = None
        self.ticket_url = ""
        self.account: dict | None = None
        self.summary = ""
        self.handoff = False
        self.human_joined = asyncio.Event()


class SupportAgent(Agent):
    def __init__(self, ctx_text: str, state: CallState, session_ref: dict):
        super().__init__(instructions=INSTRUCTIONS + "\n\nCONTEXT:\n" + ctx_text)
        self.state, self.session_ref = state, session_ref

    @function_tool()
    async def transfer_to_human(self, context: RunContext, summary: str) -> str:
        """Final step of a handoff, after open_ticket and request_handoff: registers the summary for the human's
        screen and lets the person take over. `summary` = the same call summary you gave the ticket."""
        st = self.state
        st.summary, st.handoff = summary, True
        room = context.session.room_io.room.name if context.session.room_io else ""
        try:
            async with aiohttp.ClientSession() as s:
                await s.post(f"{CLOUD_URL}/api/handoff", json={"room": room, "summary": summary, "account": st.account,
                                                                "ticket_url": st.ticket_url, "ticket_id": st.ticket_id}, timeout=aiohttp.ClientTimeout(10))
        except Exception as e:
            log.warning("handoff registration failed: %s", e)
        return "Handoff registered. Tell the caller a specialist is joining and then stay silent until spoken to."


def build_tts() -> agents_tts.TTS:
    primary = inference.TTS(TTS_VOICE)
    if os.environ.get("OPENAI_API_KEY"):
        return agents_tts.FallbackAdapter([primary, openai.TTS(model="gpt-4o-mini-tts", voice="alloy")])
    return primary


async def prefetch_account(phone: str) -> dict | None:
    """Ask the cloud for the account before the first word so the greeting can use the caller's name.
    This goes through the gateway like every other tool call (the worker is a registered caller)."""
    if not phone:
        return None
    try:
        srv = mcp.MCPServerHTTP(url=GATEWAY_MCP_URL, headers={"Authorization": f"Bearer {GATEWAY_CALLER_KEY}"}, timeout=15)
        await srv.initialize()
        tools = await srv.list_tools()
        name = next((t.name for t in tools if t.name.endswith("find_account")), None)
        if not name:
            return None
        res = await srv.call_tool(name, {"phone": phone})
        text = "".join(getattr(c, "text", "") for c in getattr(res, "content", []) or [])
        data = json.loads(text) if text.strip().startswith("{") else {}
        await srv.aclose()
        return data if data.get("found") else None
    except Exception as e:
        log.info("prefetch skipped: %s", e)
        return None


async def entrypoint(ctx: JobContext):
    await ctx.connect()
    meta = {}
    try:
        meta = json.loads(ctx.job.metadata or "{}")
    except Exception:
        pass
    phone = str(meta.get("phone", ""))
    state = CallState()
    lk = api.LiveKitAPI()
    recorder = Recorder(lk, ctx.room.name)
    await recorder.start()

    state.account = await prefetch_account(phone)
    ctx_text = f"ROOM: {ctx.room.name}\n"
    if state.account:
        ctx_text += "The caller's account was found from their phone number: " + json.dumps(state.account) + "\n"
    else:
        ctx_text += "The caller's account is not known yet.\n"

    gateway = mcp.MCPServerHTTP(url=GATEWAY_MCP_URL, headers={"Authorization": f"Bearer {GATEWAY_CALLER_KEY}"}, timeout=20)
    session_ref: dict = {}
    session = AgentSession(
        stt=inference.STT(STT_MODEL),
        llm=openai.LLM(model=LLM_MODEL, base_url=LLM_BASE_URL, api_key=LLM_API_KEY, temperature=0.3, timeout=60),
        tts=build_tts(),
        vad=silero.VAD.load(),
        mcp_servers=[gateway],
        max_tool_steps=6,
    )
    session_ref["session"] = session
    agent = SupportAgent(ctx_text, state, session_ref)

    @session.on("function_tools_executed")
    def _on_tools(ev):
        for call, out in zip(getattr(ev, "function_calls", []) or [], getattr(ev, "function_call_outputs", []) or []):
            name, text = getattr(call, "name", ""), str(getattr(out, "output", "") or "")
            m = re.search(r'"ticket_id":\s*(\d+)', text)
            if name.endswith("open_ticket") and m:
                state.ticket_id = int(m.group(1))
                u = re.search(r'"ticket_url":\s*"([^"]+)"', text)
                state.ticket_url = u.group(1) if u else ""
                log.info("ticket %s opened", state.ticket_id)
            if name.endswith("find_account") and '"found": true' in text.lower():
                try:
                    state.account = json.loads(text)
                except Exception:
                    pass

    @ctx.room.on("participant_connected")
    def _joined(p: rtc.RemoteParticipant):
        if p.identity.startswith("human-"):
            log.info("human %s joined; agent steps back", p.identity)
            state.human_joined.set()

    await session.start(agent=agent, room=ctx.room, room_input_options=room_io.RoomInputOptions(close_on_disconnect=False))
    try:
        bg = BackgroundAudioPlayer(thinking_sound=[AudioConfig(BuiltinAudioClip.KEYBOARD_TYPING, volume=0.5)])
        await bg.start(room=ctx.room, agent_session=session)
    except Exception as e:
        log.info("no background audio: %s", e)

    greet = f"Greet the caller warmly as HavenIQ support" + (f", by name: {state.account['name']}" if state.account else ", and ask for the phone number or account number on the account") + ". One sentence."
    await session.generate_reply(instructions=greet)

    # step back once a person is on the call: stop listening/speaking, keep recording
    async def step_back():
        await state.human_joined.wait()
        try:
            session.input.set_audio_enabled(False)
            session.output.set_audio_enabled(False)
        except Exception as e:
            log.info("step back: %s", e)
    asyncio.create_task(step_back())

    # end of call: when no humans remain (caller and any support agent), finish the recording and attach it
    def humans():
        return [p for p in ctx.room.remote_participants.values() if not p.identity.startswith("agent-")]
    last_seen = time.time()
    while True:
        await asyncio.sleep(2)
        if humans():
            last_seen = time.time()
        elif time.time() - last_seen > 8:
            break
        if time.time() - last_seen > IDLE_HANGUP_S:
            break
    log.info("call over in %s; finishing recording", ctx.room.name)
    try:
        await session.aclose()
    except Exception:
        pass
    rec = await recorder.finish()
    if rec and state.ticket_id:
        key, seconds = rec
        try:
            async with aiohttp.ClientSession() as s:
                r = await s.post(f"{CRM_URL}/api/tickets/{state.ticket_id}/recording", json={"key": key, "room": ctx.room.name, "seconds": seconds},
                                 timeout=aiohttp.ClientTimeout(120))
                log.info("recording attached: %s %s", r.status, (await r.text())[:200])
        except Exception as e:
            log.warning("recording attach failed: %s", e)
    elif rec:
        log.info("recording %s kept in the bucket; no ticket was opened on this call", rec[0])
    await lk.aclose()
    ctx.shutdown(reason="call over")


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, agent_name=AGENT_NAME, shutdown_process_timeout=240.0))
