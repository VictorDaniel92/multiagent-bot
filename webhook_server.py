"""
webhook_server.py — Server HTTP per ricevere i webhook di GitHub.

Ascolta su POST /github-webhook, verifica la firma HMAC,
estrae i dati del commit e notifica Sophia in General.
"""

import os
import hmac
import hashlib
import logging
import asyncio
from aiohttp import web
from agents import call_llm
from pathlib import Path

logger = logging.getLogger(__name__)

GITHUB_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
GROUP_CHAT_ID         = int(os.environ.get("GROUP_CHAT_ID", "0"))
PORT                  = int(os.environ.get("PORT", "8080"))

SOULS_DIR  = Path(__file__).parent / "souls"
SOUL_SOPHIA = (SOULS_DIR / "sophia.md").read_text(encoding="utf-8") if (SOULS_DIR / "sophia.md").exists() else ""

# Bot application — viene iniettato da bot.py dopo l'avvio
_bot_app = None

def set_bot_app(app):
    global _bot_app
    _bot_app = app


# ── Verifica firma GitHub ──────────────────────────────────────────────────────

def verify_signature(payload: bytes, signature: str) -> bool:
    if not GITHUB_WEBHOOK_SECRET:
        logger.warning("GITHUB_WEBHOOK_SECRET non configurato — firma non verificata")
        return True  # in dev, accetta tutto
    expected = "sha256=" + hmac.new(
        GITHUB_WEBHOOK_SECRET.encode(),
        payload,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


# ── Sophia commenta il deploy ──────────────────────────────────────────────────

async def sophia_deploy_message(commits: list[dict], branch: str, pusher: str) -> str:
    """Sophia scrive un sunto human-friendly delle modifiche pushate."""

    # Costruisce il testo dei commit
    commit_lines = []
    for c in commits[:5]:  # max 5 commit
        msg   = c.get("message", "").split("\n")[0][:120]  # solo prima riga
        files = c.get("added", []) + c.get("modified", []) + c.get("removed", [])
        files_str = ", ".join(f[:40] for f in files[:4])
        commit_lines.append(f'- "{msg}" (file: {files_str or "nessuno"})')

    commits_text = "\n".join(commit_lines) or "- nessun commit"

    system = f"""{SOUL_SOPHIA}

Sei la receptionist del gruppo. Qualcuno ha appena fatto un push sul repository del bot.
Scrivi un messaggio breve e caldo per il gruppo General che spieghi cosa è cambiato.

Regole:
- Max 4 righe
- Tono leggero, come un aggiornamento tra colleghi
- Spiega in parole semplici cosa è stato modificato (non tecnico)
- Puoi citare i file o i commit in modo umano ("ho aggiornato la memoria di Giorgio", "Sophia ora ricorda i deploy")
- Usa emoji con parsimonia
- Inizia con qualcosa tipo "🔧 Piccolo aggiornamento..." o "✨ Ho appena ricevuto delle novità..."
- Usa Markdown Telegram"""

    return await call_llm(
        system=system,
        messages=[{
            "role": "user",
            "content": (
                f"Branch: {branch}\n"
                f"Push di: {pusher}\n"
                f"Commit:\n{commits_text}"
            )
        }],
        max_tokens=200,
    )


# ── Handler webhook ────────────────────────────────────────────────────────────

async def handle_github_webhook(request: web.Request) -> web.Response:
    # Verifica evento
    event = request.headers.get("X-GitHub-Event", "")
    if event == "ping":
        return web.json_response({"ok": True, "msg": "pong"})
    if event != "push":
        return web.json_response({"ok": True, "msg": f"evento '{event}' ignorato"})

    # Legge body e verifica firma
    payload = await request.read()
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not verify_signature(payload, signature):
        logger.warning("Firma webhook GitHub non valida")
        return web.json_response({"error": "invalid signature"}, status=401)

    import json
    try:
        data = json.loads(payload)
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    # Estrae dati del push
    commits = data.get("commits", [])
    branch  = data.get("ref", "").replace("refs/heads/", "")
    pusher  = data.get("pusher", {}).get("name", "qualcuno")

    logger.info(f"GitHub push: {len(commits)} commit su {branch} da {pusher}")

    # Solo branch main/master
    if branch not in ("main", "master"):
        return web.json_response({"ok": True, "msg": f"branch {branch} ignorato"})

    if not commits:
        return web.json_response({"ok": True, "msg": "nessun commit"})

    # Sophia scrive in General
    if _bot_app and GROUP_CHAT_ID:
        async def _notify():
            try:
                msg = await sophia_deploy_message(commits, branch, pusher)
                await _bot_app.bot.send_message(
                    chat_id=GROUP_CHAT_ID,
                    text=msg,
                    parse_mode="Markdown",
                )
                logger.info("Sophia ha notificato il deploy ✅")
            except Exception as e:
                logger.error(f"Errore notifica deploy: {e}", exc_info=True)

        asyncio.create_task(_notify())

    return web.json_response({"ok": True, "commits": len(commits)})


async def handle_health(request: web.Request) -> web.Response:
    """Health check per Railway."""
    return web.json_response({"status": "ok", "service": "multiagent-bot"})


# ── Avvio server ───────────────────────────────────────────────────────────────

async def start_webhook_server():
    """Avvia il server aiohttp in background."""
    app = web.Application()
    app.router.add_post("/github-webhook", handle_github_webhook)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/", handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Webhook server avviato su porta {PORT} ✅")
