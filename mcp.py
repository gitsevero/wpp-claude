"""MCP HTTP server embutido. Tool: approval_prompt."""
from __future__ import annotations
import asyncio
import json
import logging
from typing import Any, Callable

log = logging.getLogger("mcp")


def _format_input(tool_name: str, inp: dict) -> str:
    if not inp: return "(sem input)"
    if tool_name == "Write":
        return f"criar {inp.get('file_path')} ({len(inp.get('content') or '')} chars)"
    if tool_name == "Edit":
        return f"editar {inp.get('file_path')}"
    if tool_name == "MultiEdit":
        return f"editar {inp.get('file_path')} ({len(inp.get('edits') or [])} mudancas)"
    if tool_name == "NotebookEdit":
        return f"notebook {inp.get('notebook_path')}"
    if tool_name == "Bash":
        return f"`{str(inp.get('command') or '')[:300]}`"
    return json.dumps(inp)[:300]


class MCP:
    def __init__(self, wa) -> None:
        self.wa = wa
        self.pending: dict[str, asyncio.Future] = {}
        self.current_jid: str | None = None

    def set_current_jid(self, jid: str | None) -> None:
        self.current_jid = jid

    def has_pending(self, jid: str) -> bool:
        return jid in self.pending

    def resolve_approval(self, jid: str, decision: dict) -> bool:
        fut = self.pending.pop(jid, None)
        if not fut or fut.done(): return False
        fut.set_result(decision)
        return True

    async def handle_rpc(self, req: dict) -> dict:
        rpc_id = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}
        log.info("<- %s id=%s", method, rpc_id)

        if method == "initialize":
            return {"jsonrpc": "2.0", "id": rpc_id, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "wa-approval", "version": "1.0.0"},
            }}

        if method == "notifications/initialized":
            return {}  # notificacao, sem resposta

        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": rpc_id, "result": {"tools": [{
                "name": "approval_prompt",
                "description": "Pede aprovacao do usuario via WhatsApp antes de executar tool.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "tool_name": {"type": "string"},
                        "input": {"type": "object"},
                        "tool_use_id": {"type": "string"},
                    },
                    "required": ["tool_name", "input"],
                },
            }]}}

        if method == "tools/call" and (params.get("name") == "approval_prompt"):
            jid = self.current_jid
            args = params.get("arguments") or {}
            tool_name = args.get("tool_name") or "?"
            inp = args.get("input") or {}
            tool_use_id = args.get("tool_use_id") or ""

            if not jid:
                return self._call_result(rpc_id, {"behavior": "deny", "message": "sem jid"})

            number = jid.split("@")[0]
            summary = _format_input(tool_name, inp)
            log.info("APROVACAO pedida [%s] %s", number, tool_name)

            # Manda enquete (sim/nao) — UX nativa do WA
            try:
                await self.wa.send_text(number, f"🔐 *Aprovar {tool_name}?*\n{summary}")
                await self.wa.send_poll(
                    number,
                    name=f"Aprovar {tool_name}?",
                    values=["✅ Sim, executar", "❌ Nao, cancelar"],
                    selectable_count=1,
                )
            except Exception as e:
                log.warning("send_poll falhou (%s), usando texto", e)
                try:
                    await self.wa.send_text(number, f"Responda *sim* ou *nao*")
                except Exception:
                    pass

            fut: asyncio.Future = asyncio.get_event_loop().create_future()
            self.pending[jid] = fut
            try:
                decision = await asyncio.wait_for(fut, timeout=300)
            except asyncio.TimeoutError:
                self.pending.pop(jid, None)
                decision = {"behavior": "deny", "message": "timeout (5 min)"}

            log.info("-> %s [%s] %s", decision.get("behavior"), number, tool_name)
            if decision.get("behavior") == "allow":
                result = {"behavior": "allow", "updatedInput": inp}
            else:
                result = {"behavior": "deny", "message": decision.get("message") or "usuario negou"}
            return self._call_result(rpc_id, result)

        return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32601, "message": f"metodo desconhecido: {method}"}}

    @staticmethod
    def _call_result(rpc_id: Any, payload: dict) -> dict:
        return {"jsonrpc": "2.0", "id": rpc_id, "result": {
            "content": [{"type": "text", "text": json.dumps(payload)}]
        }}
