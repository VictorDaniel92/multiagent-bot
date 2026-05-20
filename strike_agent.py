"""
strike_agent.py — Monitoraggio scioperi trasporti pubblici Milano

Cerca online con Serper le notizie di sciopero ATM/trasporti Milano
per i prossimi 7 giorni e formatta una risposta chiara.
"""
import os
import json
import logging
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

from agents import call_llm

logger        = logging.getLogger(__name__)
SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "")
SOULS_DIR      = Path(__file__).parent / "souls"


# ── SOUL SOPHIA (per il tono delle risposte sugli scioperi) ──────────────────

def _load_soul(name: str) -> str:
    path = SOULS_DIR / f"{name}.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""

SOUL_SOPHIA = _load_soul("sophia")


# ── RICERCA SERPER ────────────────────────────────────────────────────────────

def _serper_search(query: str, max_results: int = 6) -> list[dict]:
    """Cerca con Serper e restituisce i risultati organici."""
    if not SERPER_API_KEY:
        logger.error("SERPER_API_KEY mancante")
        return []
    try:
        payload = json.dumps({"q": query, "num": max_results, "gl": "it", "hl": "it"}).encode()
        req = urllib.request.Request(
            "https://google.serper.dev/search",
            data=payload,
            headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return data.get("organic", [])
    except Exception as e:
        logger.error(f"Errore Serper scioperi: {e}")
        return []


def _format_results(results: list[dict]) -> str:
    if not results:
        return "Nessun risultato trovato."
    lines = []
    for r in results[:5]:
        lines.append(f"- {r.get('title', '')}: {r.get('snippet', '')}")
    return "\n".join(lines)


# ── RICERCA SCIOPERI ──────────────────────────────────────────────────────────

async def search_strikes(days_ahead: int = 7) -> dict:
    """
    Cerca scioperi trasporti pubblici Milano nei prossimi N giorni.
    Restituisce: {found: bool, summary: str, raw: str}
    """
    today     = datetime.now()
    date_str  = today.strftime("%d %B %Y")
    end_date  = (today + timedelta(days=days_ahead)).strftime("%d %B %Y")

    # Query multiple per coprire più fonti
    queries = [
        f"sciopero ATM Milano {today.strftime('%B %Y')}",
        f"sciopero trasporti pubblici Milano prossimi giorni {today.year}",
        f"sciopero metro bus tram Milano {today.strftime('%B %Y')}",
    ]

    all_results = []
    for query in queries:
        results = _serper_search(query, max_results=4)
        all_results.extend(results)

    # Deduplicazione per URL
    seen_urls: set = set()
    unique = []
    for r in all_results:
        url = r.get("link", "")
        if url not in seen_urls:
            seen_urls.add(url)
            unique.append(r)

    raw_text = _format_results(unique[:8])

    # Chiedi all'LLM di analizzare i risultati
    analysis = await call_llm(
        system=f"""{SOUL_SOPHIA}

Sei Sophia. Hai appena cercato online notizie di sciopero dei trasporti pubblici
(metro, bus, tram ATM) a Milano. Oggi è {date_str}.

Analizza i risultati e rispondi in modo chiaro e utile.

Se non ci sono scioperi confermati nei prossimi {days_ahead} giorni: dillo chiaramente.
Se ci sono scioperi: indica data, orari se disponibili, linee coinvolte, e un consiglio pratico.
Se le informazioni sono ambigue o datate: segnalalo.

Formato: messaggio Telegram conciso, max 150 parole.
Inizia con un'emoji appropriata (✅ se tutto ok, ⚠️ se ci sono scioperi, ❓ se incerto).
Scrivi in italiano.""",
        messages=[{
            "role": "user",
            "content": f"Risultati ricerca scioperi Milano ({date_str} → {end_date}):\n{raw_text}"
        }],
        max_tokens=300,
    )

    # Determina se ci sono scioperi (per il job automatico)
    found = await call_llm(
        system="Rispondi SOLO con 'SI' o 'NO'. Nei risultati ci sono scioperi dei trasporti confermati per i prossimi giorni?",
        messages=[{"role": "user", "content": raw_text}],
        max_tokens=5,
    )
    has_strikes = "SI" in found.upper()

    return {
        "found":   has_strikes,
        "summary": analysis.strip(),
        "raw":     raw_text,
    }


# ── RISPOSTA ON-DEMAND ────────────────────────────────────────────────────────

async def sophia_strike_response() -> str:
    """
    Sophia risponde a una domanda sugli scioperi.
    Chiamata quando l'utente chiede in General.
    """
    result = await search_strikes(days_ahead=7)
    return result["summary"]


# ── JOB AUTOMATICO ────────────────────────────────────────────────────────────

async def job_check_strikes(context) -> None:
    """
    Job mattutino — controlla scioperi e avvisa solo se ce ne sono.
    Silenzioso se non ci sono scioperi.
    """
    import os
    GROUP_CHAT_ID    = int(os.environ.get("GROUP_CHAT_ID", "0"))
    GENERAL_TOPIC_ID = int(os.environ.get("GENERAL_TOPIC_ID", "0")) or None

    if not GROUP_CHAT_ID:
        logger.warning("GROUP_CHAT_ID non configurato — skip job scioperi")
        return

    try:
        result = await search_strikes(days_ahead=7)

        if not result["found"]:
            logger.info("Nessuno sciopero trovato — job silenzioso")
            return

        # Manda avviso solo se ci sono scioperi
        await context.bot.send_message(
            chat_id          = GROUP_CHAT_ID,
            message_thread_id= GENERAL_TOPIC_ID,
            text             = f"🚨 *Avviso scioperi trasporti Milano*\n\n{result['summary']}",
            parse_mode       = "Markdown",
        )
        logger.info("Avviso scioperi inviato ✅")

    except Exception as e:
        logger.error(f"Errore job scioperi: {e}", exc_info=True)
