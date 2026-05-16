"""
sophia_agent.py — La receptionist del gruppo Telegram.

Sophia si attiva SOLO in General (thread_id None) quando:
  - viene esplicitamente nominata
  - un nuovo membro entra
  - alle 18:00 per il riepilogo giornaliero
  - qualcuno usa /agenti o /sophia
"""

import os
import json
import logging
import re
import datetime
from pathlib import Path
from agents import call_llm

logger = logging.getLogger(__name__)

SOULS_DIR = Path(__file__).parent / "souls"

# ── Configurazione agenti conosciuti da Sophia ─────────────────────────────
# Aggiungi qui i nuovi agenti man mano che li crei
KNOWN_AGENTS = {
    "giorgio": {
        "topic_id":    99,
        "topic_name":  "meteo",
        "emoji":       "🌤️",
        "description": "previsioni meteo, temperatura, vento, pioggia",
        "keywords":    ["meteo", "tempo", "pioggia", "sole", "vento", "temperatura",
                        "previsioni", "caldo", "freddo", "neve", "umidità", "forecast"],
    },
    "luca": {
        "topic_id":    57,
        "topic_name":  "news",
        "emoji":       "🎮",
        "description": "videogiochi, gaming, recensioni, industria videoludica, eSports",
        "keywords":    ["gioco", "gaming", "videogioco", "ps5", "xbox", "nintendo",
                        "steam", "uscita", "recensione", "gameplay", "dlc", "patch"],
    },
}

def load_soul() -> str:
    path = SOULS_DIR / "sophia.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""

SOUL_SOPHIA = load_soul()


# ── Rilevamento menzione di Sophia ─────────────────────────────────────────

def sophia_is_mentioned(text: str) -> bool:
    """True se il messaggio contiene 'sophia' o 'sofia' (case-insensitive)."""
    return bool(re.search(r'\b(sophia|sofia)\b', text, re.IGNORECASE))


# ── Routing intelligente ───────────────────────────────────────────────────

async def sophia_route_request(question: str) -> dict:
    """
    Analizza la domanda e decide quale agente è competente.
    Ritorna: {
        "agent": "giorgio" | "luca" | None,
        "topic_id": int | None,
        "topic_name": str,
        "rephrased_question": str,   # domanda riformulata per l'agente
        "sophia_reply": str,         # risposta di Sophia in General
    }
    """
    agents_desc = "\n".join(
        f'- "{name}": {info["description"]} (topic: {info["topic_name"]})'
        for name, info in KNOWN_AGENTS.items()
    )

    system = f"""{SOUL_SOPHIA}

Sei la receptionist. Analizza la richiesta e decidi:
1. Quale agente è competente (o nessuno se è fuori dalla competenza di tutti)
2. Come riformulare la domanda per quell'agente
3. Cosa rispondere all'utente in General (breve, caldo, al massimo 2 righe)

Agenti disponibili:
{agents_desc}

Rispondi SOLO con JSON valido, nessun testo fuori:
{{
  "agent": "nome_agente_o_null",
  "rephrased_question": "domanda riformulata per l'agente",
  "sophia_reply": "risposta breve in General per l'utente",
  "out_of_scope": false
}}

Se la richiesta non rientra nelle competenze di nessun agente, metti agent: null e out_of_scope: true,
e in sophia_reply spiega gentilmente cosa sanno fare gli agenti."""

    raw = await call_llm(
        system=system,
        messages=[{"role": "user", "content": f"Richiesta utente: {question}"}],
        max_tokens=300
    )

    try:
        data = json.loads(re.search(r'\{.*\}', raw, re.DOTALL).group())
        agent_name = data.get("agent")
        agent_info = KNOWN_AGENTS.get(agent_name) if agent_name else None

        return {
            "agent":               agent_name,
            "topic_id":            agent_info["topic_id"] if agent_info else None,
            "topic_name":          agent_info["topic_name"] if agent_info else "",
            "emoji":               agent_info["emoji"] if agent_info else "💬",
            "rephrased_question":  data.get("rephrased_question", question),
            "sophia_reply":        data.get("sophia_reply", "Lascia che controlli..."),
            "out_of_scope":        data.get("out_of_scope", False),
        }
    except Exception as e:
        logger.error(f"Errore parsing routing Sophia: {e} — raw: {raw}")
        return {
            "agent": None, "topic_id": None, "topic_name": "",
            "emoji": "💬",
            "rephrased_question": question,
            "sophia_reply": "Mmh, non sono sicura di chi possa aiutarti. Prova a scrivere direttamente nel topic giusto!",
            "out_of_scope": True,
        }


def sophia_format_agent_message(agent_name: str, rephrased_question: str, requester_name: str) -> str:
    """Formatta il messaggio che Sophia manda nel topic dell'agente."""
    agent = KNOWN_AGENTS.get(agent_name, {})
    emoji = agent.get("emoji", "💬")
    name_cap = agent_name.capitalize()
    return (
        f"{emoji} {name_cap}, ti giro una domanda da *{requester_name}*:\n\n"
        f"_{rephrased_question}_"
    )


# ── Benvenuto nuovo membro ─────────────────────────────────────────────────

async def sophia_welcome(new_member_name: str) -> str:
    """Genera un benvenuto personalizzato per un nuovo membro."""
    agents_list = "\n".join(
        f'- *{name.capitalize()}* {info["emoji"]} — {info["description"].split(",")[0]}'
        for name, info in KNOWN_AGENTS.items()
    )

    system = f"""{SOUL_SOPHIA}

Dai il benvenuto a un nuovo membro del gruppo. Sii calorosa, breve (max 5 righe),
spiega chi sono gli agenti nel gruppo e come usarli.
Non usare liste puntate pesanti — scrivi in modo naturale.
Usa Markdown Telegram (*grassetto*, _corsivo_)."""

    return await call_llm(
        system=system,
        messages=[{
            "role": "user",
            "content": f"Nuovo membro: {new_member_name}\n\nAgenti presenti:\n{agents_list}"
        }],
        max_tokens=300
    )


# ── Riepilogo giornaliero ─────────────────────────────────────────────────

async def sophia_daily_recap(activity_summary: dict) -> str:
    """
    Genera il riepilogo delle 18:00.
    activity_summary: {"news": 5, "meteo": 3, ...} — messaggi per topic
    """
    system = f"""{SOUL_SOPHIA}

Scrivi un breve recap serale (max 6 righe) del giorno nel gruppo.
Menziona i topic più attivi. Tono caldo e leggero, come la fine di una giornata
di lavoro condiviso. Usa emoji con parsimonia.
Usa Markdown Telegram."""

    summary_text = "\n".join(
        f"- {topic}: {count} messaggi"
        for topic, count in sorted(activity_summary.items(), key=lambda x: -x[1])
    ) or "Giornata tranquilla, pochi messaggi."

    return await call_llm(
        system=system,
        messages=[{"role": "user", "content": f"Attività di oggi:\n{summary_text}"}],
        max_tokens=250
    )


# ── Stato agenti (/agenti in General) ─────────────────────────────────────

def sophia_agent_status() -> str:
    """Risposta al comando /agenti in General — lista degli agenti."""
    lines = ["👋 Ecco chi c'è nel gruppo:\n"]
    for name, info in KNOWN_AGENTS.items():
        lines.append(
            f"{info['emoji']} *{name.capitalize()}* — {info['description'].split(',')[0]}\n"
            f"   📍 Topic: _{info['topic_name'].capitalize()}_"
        )
    lines.append(
        "\nMenzionami scrivendo *Sophia* e ti smisto dalla persona giusta! 😊"
    )
    return "\n".join(lines)


# ── Memoria attività (contatore semplice in-memory) ───────────────────────

_activity_counter: dict[str, int] = {}

def sophia_log_activity(topic_name: str):
    """Registra un'attività in un topic."""
    _activity_counter[topic_name] = _activity_counter.get(topic_name, 0) + 1

def sophia_get_activity() -> dict:
    return dict(_activity_counter)

def sophia_reset_activity():
    _activity_counter.clear()


# ── Promemoria ─────────────────────────────────────────────────────────────

# Struttura: { user_id: [ {"text": str, "when": datetime, "chat_id": int, "thread_id": int|None} ] }
_reminders: dict[int, list[dict]] = {}

async def sophia_parse_reminder(text: str) -> dict | None:
    """
    Analizza una richiesta di promemoria tipo "ricordami domani alle 9 di controllare il meteo".
    Ritorna: {"when_str": "domani alle 9:00", "what": "controllare il meteo"} oppure None.
    """
    now = datetime.datetime.now()
    system = f"""Sei un parser di promemoria. Data e ora attuale: {now.strftime("%Y-%m-%d %H:%M")}.
Estrai dal testo la data/ora e il contenuto del promemoria.
Rispondi SOLO con JSON: {{"when_iso": "YYYY-MM-DDTHH:MM", "what": "testo promemoria", "when_str": "descrizione umana"}}
Se non riesci a capire la data, metti null per when_iso."""

    raw = await call_llm(
        system=system,
        messages=[{"role": "user", "content": text}],
        max_tokens=150
    )
    try:
        data = json.loads(re.search(r'\{.*\}', raw, re.DOTALL).group())
        if not data.get("when_iso"):
            return None
        return data
    except Exception:
        return None

def sophia_add_reminder(user_id: int, chat_id: int, thread_id: int | None,
                        when: datetime.datetime, what: str):
    if user_id not in _reminders:
        _reminders[user_id] = []
    _reminders[user_id].append({
        "text": what,
        "when": when,
        "chat_id": chat_id,
        "thread_id": thread_id,
    })

def sophia_get_due_reminders() -> list[dict]:
    """Restituisce i promemoria scaduti (e li rimuove)."""
    now = datetime.datetime.now()
    due = []
    for user_id, items in list(_reminders.items()):
        still_pending = []
        for r in items:
            if r["when"] <= now:
                due.append({"user_id": user_id, **r})
            else:
                still_pending.append(r)
        _reminders[user_id] = still_pending
    return due


# ── Memoria del gruppo ─────────────────────────────────────────────────────────

async def sophia_answer_with_memory(question: str) -> str:
    """
    Sophia risponde a domande sulla memoria del gruppo tipo:
    "di cosa si è parlato oggi?", "Luca ha detto qualcosa su X?"
    Usa get_recent_conversations e search_memories dal vector store.
    Import lazy per evitare circular import.
    """
    try:
        from memory_vector import search_memories, get_recent_conversations, format_memories_for_prompt

        # Cerca sia per similarità che per recency
        similar  = await search_memories(question, limit=4, min_similarity=0.70)
        recent   = await get_recent_conversations(limit=5)

        # Deduplicazione per question
        seen  = set()
        combined = []
        for m in similar + recent:
            key = m["question"][:80]
            if key not in seen:
                seen.add(key)
                combined.append(m)

        if not combined:
            return "Non ho ancora abbastanza memoria delle conversazioni nel gruppo. Man mano che gli agenti rispondono, inizierò a ricordare tutto! 😊"

        memory_text = format_memories_for_prompt(combined, label="Conversazioni nel gruppo")

        system = f"""{SOUL_SOPHIA}

Hai accesso alla memoria delle conversazioni recenti nel gruppo.
Rispondi alla domanda in modo naturale e preciso, citando gli agenti e i topic quando rilevante.
Esempio: "Luca ne ha parlato ieri nel topic News — ha detto che..."
Sii concisa, max 4 righe. Usa Markdown Telegram."""

        return await call_llm(
            system=system,
            messages=[{
                "role": "user",
                "content": f"{memory_text}\n\nDomanda: {question}"
            }],
            max_tokens=300
        )

    except Exception as e:
        logger.error(f"Errore sophia_answer_with_memory: {e}")
        return "Non riesco ad accedere alla memoria in questo momento. Riprova tra poco!"
