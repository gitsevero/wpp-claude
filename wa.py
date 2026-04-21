"""Cliente Evolution API."""
from __future__ import annotations
import httpx
from typing import Any


class WA:
    def __init__(self, url: str, apikey: str, instance: str) -> None:
        self.url = url.rstrip("/")
        self.instance = instance
        self.headers = {"Content-Type": "application/json", "apikey": apikey}
        self.client = httpx.AsyncClient(timeout=30.0)

    async def send_text(self, number: str, text: str) -> dict:
        r = await self.client.post(
            f"{self.url}/message/sendText/{self.instance}",
            headers=self.headers,
            json={"number": number, "text": text or "(vazio)"},
        )
        r.raise_for_status()
        try: return r.json()
        except Exception: return {}

    async def send_list(self, number: str, *, title: str, description: str,
                        button_text: str, rows: list[dict]) -> dict:
        r = await self.client.post(
            f"{self.url}/message/sendList/{self.instance}",
            headers=self.headers,
            json={
                "number": number,
                "title": title,
                "description": description,
                "buttonText": button_text or "Escolher",
                "footerText": "claude-wa-bridge",
                "sections": [{"title": title or "Opcoes", "rows": rows}],
            },
        )
        if not r.is_success:
            raise RuntimeError(f"sendList {r.status_code}: {r.text[:500]}")
        try: return r.json()
        except Exception: return {}

    async def send_poll(self, number: str, *, name: str, values: list[str], selectable_count: int = 1) -> dict:
        """Enquete (sim/nao etc). Melhor UX pra aprovacao binaria no WhatsApp."""
        r = await self.client.post(
            f"{self.url}/message/sendPoll/{self.instance}",
            headers=self.headers,
            json={
                "number": number,
                "name": name,
                "selectableCount": selectable_count,
                "values": values,
            },
        )
        if not r.is_success:
            raise RuntimeError(f"sendPoll {r.status_code}: {r.text[:500]}")
        try: return r.json()
        except Exception: return {}

    async def send_presence(self, number: str, presence: str = "composing", delay_ms: int = 25000) -> None:
        # Evolution segura a conexao por `delay_ms` antes de responder. Nunca chame com await dentro
        # de um caminho critico — use schedule_presence() pra fire-and-forget.
        try:
            await self.client.post(
                f"{self.url}/chat/sendPresence/{self.instance}",
                headers=self.headers,
                json={"number": number, "presence": presence, "delay": delay_ms},
            )
        except Exception:
            pass

    def schedule_presence(self, number: str, presence: str = "composing", delay_ms: int = 25000):
        """Fire-and-forget — agenda no event loop e retorna imediatamente."""
        import asyncio
        asyncio.create_task(self.send_presence(number, presence, delay_ms))

    @staticmethod
    def parse_webhook(body: dict) -> dict | None:
        d = (body or {}).get("data") or {}
        if not d: return None
        key = d.get("key") or {}
        jid = key.get("remoteJid")
        if not jid: return None
        msg = d.get("message") or {}
        text = (
            msg.get("conversation")
            or (msg.get("extendedTextMessage") or {}).get("text")
            or (msg.get("imageMessage") or {}).get("caption")
            or (msg.get("listResponseMessage") or {}).get("title")
        )
        list_row_id = (
            ((msg.get("listResponseMessage") or {}).get("singleSelectReply") or {}).get("selectedRowId")
        )

        # voto de poll — Evolution decodifica e pode trazer em paths diferentes
        poll_vote = None
        pum = msg.get("pollUpdateMessage") or {}
        if pum:
            vote = pum.get("vote") or {}
            sel = vote.get("selectedOptions") or vote.get("values") or []
            if sel:
                poll_vote = sel[0] if isinstance(sel, list) else sel
        # alguns formatos vem em d.pollUpdates
        if not poll_vote:
            updates = d.get("pollUpdates") or []
            if updates:
                v = (updates[0] or {}).get("vote") or {}
                sel = v.get("selectedOptions") or v.get("values") or []
                if sel: poll_vote = sel[0] if isinstance(sel, list) else sel

        media_type = next(iter(msg.keys()), None)
        return {
            "jid": jid,
            "number": jid.split("@")[0],
            "text": text,
            "list_row_id": list_row_id,
            "poll_vote": poll_vote,
            "from_me": bool(key.get("fromMe")),
            "is_group": jid.endswith("@g.us"),
            "media_type": media_type,
        }

    async def close(self) -> None:
        await self.client.aclose()
