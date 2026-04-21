"""Wrapper do Claude CLI em stream-json."""
from __future__ import annotations
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field

log = logging.getLogger("claude")


@dataclass
class ClaudeResult:
    ok: bool
    text: str = ""
    tool_uses: list[dict] = field(default_factory=list)
    reason: str = ""


async def run_claude(*, prompt: str, model: str, timeout_ms: int, mcp_config_path: str) -> ClaudeResult:
    args = [
        "claude",
        "-p",
        "--model", model,
        "--output-format", "stream-json",
        "--verbose",
        "--mcp-config", mcp_config_path,
        "--permission-prompt-tool", "mcp__wa__approval_prompt",
    ]
    # Windows: claude eh instalado como claude.cmd — precisa shell pra resolver
    is_windows = sys.platform.startswith("win")
    log.info("exec: %s", " ".join(args))

    if is_windows:
        # shell=True no Windows usa cmd.exe que acha claude.cmd no PATH
        proc = await asyncio.create_subprocess_shell(
            subprocess_cmd(args),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    else:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    text_parts: list[str] = []
    tool_uses: list[dict] = []

    async def read_stdout():
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line: break
            raw = line.decode("utf-8", errors="replace").rstrip()
            if not raw: continue
            sys.stdout.write("[claude-out] " + raw + "\n")
            sys.stdout.flush()
            try:
                ev = json.loads(raw)
            except Exception:
                continue
            t = ev.get("type")
            if t == "system" and ev.get("subtype") == "init":
                log.info("init session=%s mcp=%s", ev.get("session_id"), ev.get("mcp_servers"))
            elif t == "assistant":
                msg = ev.get("message") or {}
                for b in (msg.get("content") or []):
                    if b.get("type") == "text":
                        text_parts.append(b.get("text") or "")
                    elif b.get("type") == "tool_use":
                        tool_uses.append({"name": b.get("name"), "input": b.get("input")})
            elif t == "result":
                if ev.get("is_error"):
                    log.warning("result.error: %s", json.dumps(ev)[:500])
                if not text_parts and isinstance(ev.get("result"), str) and ev["result"].strip():
                    text_parts.append(ev["result"])

    async def read_stderr():
        assert proc.stderr is not None
        while True:
            line = await proc.stderr.readline()
            if not line: break
            sys.stdout.write("[claude-err] " + line.decode("utf-8", errors="replace"))
            sys.stdout.flush()

    # envia prompt via stdin
    try:
        assert proc.stdin is not None
        proc.stdin.write(prompt.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()
    except Exception as e:
        log.warning("stdin write falhou: %s", e)

    try:
        await asyncio.wait_for(
            asyncio.gather(read_stdout(), read_stderr(), proc.wait()),
            timeout=timeout_ms / 1000,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return ClaudeResult(ok=False, reason=f"timeout ({timeout_ms}ms)", tool_uses=tool_uses)

    code = proc.returncode
    final = "".join(text_parts).strip()
    log.info("done code=%s tools=%d textB=%d", code, len(tool_uses), len(final))
    if code != 0 and not final:
        return ClaudeResult(ok=False, reason=f"exit={code}", tool_uses=tool_uses)
    return ClaudeResult(ok=True, text=final, tool_uses=tool_uses)


def subprocess_cmd(args: list[str]) -> str:
    """Quota args adequadamente pra cmd.exe."""
    def q(a: str) -> str:
        if " " in a or "\\" in a: return f'"{a}"'
        return a
    return " ".join(q(a) for a in args)
