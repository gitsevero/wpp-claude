# claude-wa-bridge (Python)

Bridge entre WhatsApp (via Evolution API) e Claude Code CLI, com aprovação de permissões via WhatsApp (mensagem interativa).

## Setup

```bash
cd claude_bridge_py
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux/Mac
pip install -r requirements.txt
cp .env.example .env          # edita valores
python main.py
```

## Fluxo

1. User manda texto no WA
2. Bridge spawna `claude -p --mcp-config ... --permission-prompt-tool mcp__wa__approval_prompt`
3. Claude chama `approval_prompt` quando precisa executar Write/Edit/Bash/etc
4. Bridge envia lista interativa pro WA: ✅ Sim / ❌ Não
5. User toca → webhook → resolve aprovação → claude executa (ou nega)

## Comandos no WA

- `/reset` — limpa histórico
- `sim`/`nao` — texto fallback pra aprovação (se lista não funcionar)
