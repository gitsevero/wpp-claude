"""claude-wa-bridge (Python). Orquestra webhook WA <-> Claude CLI com aprovacao via MCP."""
from __future__ import annotations
import asyncio
import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, JSONResponse
import uvicorn

from wa import WA
from mcp import MCP
from claude_runner import run_claude

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("bridge")

EVO_URL = os.getenv("EVO_URL", "http://localhost:8080")
EVO_APIKEY = os.getenv("EVO_APIKEY", "")
EVO_INSTANCE = os.getenv("EVO_INSTANCE", "")
PORT = int(os.getenv("PORT", "3333"))
ALLOWED = [s.strip() for s in os.getenv("ALLOWED_NUMBERS", "").split(",") if s.strip()]
TIMEOUT_MS = int(os.getenv("CLAUDE_TIMEOUT_MS", "300000"))
MODEL = (os.getenv("CLAUDE_MODEL", "sonnet")).lower()

# Historico simples por jid
history: dict[str, list[dict]] = {}
# Lock global pra rodar 1 claude de cada vez (currentJid do MCP precisa ser confiavel)
claude_lock = asyncio.Lock()

wa = WA(EVO_URL, EVO_APIKEY, EVO_INSTANCE)
mcp = MCP(wa)

app = FastAPI()

# Grava mcp-config apontando pro /mcp embutido
MCP_CONFIG_PATH = str(Path.cwd() / ".mcp-config.json")
Path(MCP_CONFIG_PATH).write_text(json.dumps({
    "mcpServers": {"wa": {"type": "http", "url": f"http://localhost:{PORT}/mcp"}}
}, indent=2), encoding="utf-8")


@app.get("/", response_class=PlainTextResponse)
async def root():
    return "claude-wa-bridge (python) ok"


@app.post("/mcp")
async def mcp_endpoint(req: Request):
    body = await req.json()
    resp = await mcp.handle_rpc(body)
    if not resp: return PlainTextResponse("", status_code=200)
    return JSONResponse(resp)


@app.post("/wa")
async def wa_webhook(req: Request):
    try:
        body = await req.json()
    except Exception:
        return PlainTextResponse("ok")
    # responde rapido
    asyncio.create_task(_handle_wa(body))
    return PlainTextResponse("ok")


async def _handle_wa(body: dict):
    try:
        m = WA.parse_webhook(body)
        if not m or m["from_me"] or m["is_group"]: return
        if ALLOWED and m["number"] not in ALLOWED:
            log.info("bloqueado: %s", m["number"])
            return

        jid, number, text, row_id = m["jid"], m["number"], m["text"], m["list_row_id"]
        poll_vote = m.get("poll_vote")

        # 1a) Voto de poll (sim/nao da enquete)
        if poll_vote is not None:
            v = str(poll_vote).lower()
            is_yes = ("sim" in v) or ("✅" in v) or v == "0"
            is_no = ("nao" in v) or ("não" in v) or ("❌" in v) or v == "1"
            if is_yes:
                if mcp.resolve_approval(jid, {"behavior": "allow"}):
                    log.info("APROVOU via poll [%s]", number)
                return
            if is_no:
                if mcp.resolve_approval(jid, {"behavior": "deny", "message": "usuario negou via poll"}):
                    log.info("NEGOU via poll [%s]", number)
                    await wa.send_text(number, "ok, cancelado.")
                return

        # 1b) Resposta de lista (compat — caso ainda venha)
        if row_id:
            action = row_id.split(":", 1)[0]
            if action == "approve":
                if mcp.resolve_approval(jid, {"behavior": "allow"}):
                    log.info("APROVOU via lista [%s]", number)
                return
            if action == "deny":
                if mcp.resolve_approval(jid, {"behavior": "deny", "message": "usuario negou via lista"}):
                    log.info("NEGOU via lista [%s]", number)
                    await wa.send_text(number, "ok, cancelado.")
                return

        if not text:
            if m["media_type"] == "audioMessage":
                await wa.send_text(number, "audio nao suportado, manda em texto.")
            return

        text = text.strip()

        if text == "/reset":
            history.pop(jid, None)
            await wa.send_text(number, "contexto limpo.")
            return

        # Fallback texto pra aprovacao
        if mcp.has_pending(jid):
            norm = text.lower()
            if norm in {"sim", "s", "yes", "y", "aprovar", "ok"}:
                mcp.resolve_approval(jid, {"behavior": "allow"})
                log.info("APROVOU via texto [%s]", number)
                return
            if norm in {"nao", "não", "n", "no", "cancelar"}:
                mcp.resolve_approval(jid, {"behavior": "deny", "message": "usuario negou"})
                log.info("NEGOU via texto [%s]", number)
                await wa.send_text(number, "ok, cancelado.")
                return
            mcp.resolve_approval(jid, {"behavior": "deny", "message": "usuario mudou de assunto"})

        # Nova pergunta
        hist = history.get(jid) or []
        hist.append({"role": "user", "text": text})
        prompt = "\n".join(f"{h['role']}: {h['text']}" for h in hist) + "\nassistant:"
        log.info("RECV [%s] %s", number, text[:120])

        # presence "digitando..." sem bloquear o fluxo
        wa.schedule_presence(number, "composing")
        stop_presence = asyncio.Event()
        presence_task = asyncio.create_task(_keep_presence(number, stop_presence))

        async with claude_lock:
            mcp.set_current_jid(jid)
            try:
                result = await run_claude(
                    prompt=prompt, model=MODEL, timeout_ms=TIMEOUT_MS,
                    mcp_config_path=MCP_CONFIG_PATH,
                )
            finally:
                mcp.set_current_jid(None)
                stop_presence.set()
                presence_task.cancel()
                wa.schedule_presence(number, "paused", delay_ms=0)

        if not result.ok:
            await wa.send_text(number, f"⚠️ erro: {result.reason}")
            return

        out = result.text or "(sem resposta)"
        hist.append({"role": "assistant", "text": out})
        history[jid] = hist[-20:]

        for chunk in _chunks(out, 3500):
            await wa.send_text(number, chunk)
        log.info("DONE [%s] chars=%d tools=%d", number, len(out), len(result.tool_uses))

    except Exception as e:
        log.exception("erro webhook: %s", e)


async def _keep_presence(number: str, stop: asyncio.Event):
    # refresh a cada 20s (delay da Evolution dura 25s, entao sobrepoe)
    try:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=20)
                return
            except asyncio.TimeoutError:
                wa.schedule_presence(number, "composing")
    except asyncio.CancelledError:
        pass


def _chunks(s: str, n: int):
    for i in range(0, len(s), n):
        yield s[i:i + n]


@app.on_event("startup")
async def _startup():
    log.info("claude-wa-bridge (python) ouvindo em http://localhost:%d/wa", PORT)
    log.info("evolution: %s | instancia: %s", EVO_URL, EVO_INSTANCE)
    log.info("allowlist: %s", ", ".join(ALLOWED) or "(VAZIA — QUALQUER UM!)")
    log.info("claude: model=%s timeout=%dms", MODEL, TIMEOUT_MS)
    log.info("mcp-config: %s", MCP_CONFIG_PATH)


@app.on_event("shutdown")
async def _shutdown():
    await wa.close()


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=False, log_level="info")
