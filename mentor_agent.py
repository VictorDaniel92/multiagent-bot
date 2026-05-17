"""
mentor_agent.py — Il Mentor: analisi e proposta di miglioramento degli agenti.

Architettura (step attuali):
  1. ANALISI  — legge le ultime N conversazioni per agente dal vector store
  2. PROPOSTA — genera report con diff al soul.md
  [ approvazione e applicazione: step successivi ]
"""

import logging
from pathlib import Path
from datetime import datetime

from agents import call_llm, load_soul
from memory_vector import get_recent_conversations

logger = logging.getLogger(__name__)

SOULS_DIR   = Path(__file__).parent / "souls"
SOUL_MENTOR = load_soul("mentor")

# Agenti analizzabili dal Mentor e il loro nome nel vector store
ANALYZABLE_AGENTS = {
    "max":    "max",
    "sofia":  "sofia",
    "alex":   "alex",
    "luca":   "luca",
}


# ── STEP 1: RACCOLTA DATI ─────────────────────────────────────────────────────

async def collect_agent_conversations(agent_name: str, limit: int = 20) -> list[dict]:
    """
    Recupera le ultime `limit` conversazioni di un agente dal vector store.
    Ritorna lista di dict con: question, answer, topic, created_at
    """
    agent_key = ANALYZABLE_AGENTS.get(agent_name)
    if not agent_key:
        logger.warning(f"Agente non riconosciuto: {agent_name}")
        return []

    conversations = await get_recent_conversations(agent=agent_key, limit=limit)
    logger.info(f"Recuperate {len(conversations)} conversazioni per {agent_name}")
    return conversations


def _format_conversations_for_analysis(conversations: list[dict]) -> str:
    """
    Formatta le conversazioni in testo leggibile per il Mentor.
    """
    if not conversations:
        return "Nessuna conversazione disponibile."

    lines = []
    for i, conv in enumerate(conversations, 1):
        created = conv.get("created_at", "")
        if isinstance(created, datetime):
            created = created.strftime("%d/%m %H:%M")

        q = conv.get("question", "")[:200]
        a = conv.get("answer",   "")[:400]
        topic = conv.get("topic", "?")

        lines.append(
            f"[{i}] [{topic}] {created}\n"
            f"  D: {q}\n"
            f"  R: {a}\n"
        )

    return "\n".join(lines)


# ── STEP 2: ANALISI DEI PATTERN ───────────────────────────────────────────────

async def mentor_analyze_agent(agent_name: str, limit: int = 20) -> dict:
    """
    Analizza le conversazioni di un agente e produce un report strutturato.

    Returns:
        {
            "agent":     str,
            "n_convs":   int,
            "analysis":  str,   # testo completo del report
            "timestamp": str,
        }
    """
    conversations = await collect_agent_conversations(agent_name, limit=limit)

    if not conversations:
        return {
            "agent":     agent_name,
            "n_convs":   0,
            "analysis":  f"Nessuna conversazione trovata per l'agente *{agent_name}*. "
                         f"L'analisi richiede almeno qualche interazione registrata.",
            "timestamp": datetime.utcnow().isoformat(),
        }

    # Carica il soul attuale dell'agente come riferimento
    current_soul = load_soul(agent_name)
    soul_section = f"## Soul attuale di {agent_name}\n{current_soul}" if current_soul else ""

    conv_text = _format_conversations_for_analysis(conversations)

    system = f"""{SOUL_MENTOR}

Stai analizzando le conversazioni recenti dell'agente **{agent_name}**.

{soul_section}

Il tuo compito è identificare pattern nelle risposte e proporre miglioramenti
SPECIFICI e APPLICABILI al soul.md dell'agente.

Struttura OBBLIGATORIA del report (usa questi titoli esatti):

## ✅ Cosa funziona bene
[2-4 punti concreti, con esempio dalla conversazione se possibile]

## ⚠️ Problemi riscontrati
[Per ogni problema: descrizione + esempio concreto dalla lista + impatto]

## 🔧 Proposta di modifica al soul.md
[Diff chiaro in questo formato:]
SOSTITUISCI:
```
[testo attuale del soul da cambiare]
```
CON:
```
[nuovo testo proposto]
```
MOTIVAZIONE: [perché questa modifica]

## 📊 Sintesi
[2-3 righe: giudizio complessivo e priorità della modifica]

Sii specifico: cita le conversazioni con il loro numero [N].
Evita generalizzazioni: ogni affermazione deve avere un esempio."""

    analysis = await call_llm(
        system=system,
        messages=[{
            "role":    "user",
            "content": f"Analizza queste {len(conversations)} conversazioni di {agent_name}:\n\n{conv_text}"
        }],
        max_tokens=1500,
    )

    return {
        "agent":     agent_name,
        "n_convs":   len(conversations),
        "analysis":  analysis,
        "timestamp": datetime.utcnow().isoformat(),
    }


async def mentor_analyze_all(limit: int = 15) -> list[dict]:
    """
    Analizza tutti gli agenti in sequenza.
    Usato dal job settimanale.
    Returns: lista di report, uno per agente.
    """
    reports = []
    for agent_name in ANALYZABLE_AGENTS:
        logger.info(f"Mentor: analisi agente {agent_name}...")
        try:
            report = await mentor_analyze_agent(agent_name, limit=limit)
            reports.append(report)
        except Exception as e:
            logger.error(f"Errore analisi {agent_name}: {e}", exc_info=True)
            reports.append({
                "agent":     agent_name,
                "n_convs":   0,
                "analysis":  f"⚠️ Errore durante l'analisi di {agent_name}: {e}",
                "timestamp": datetime.utcnow().isoformat(),
            })
    return reports


# ── FORMATTAZIONE MESSAGGIO TELEGRAM ─────────────────────────────────────────

def format_analysis_for_telegram(report: dict) -> str:
    """
    Formatta il report di analisi per Telegram.
    Tronca se necessario (limite 4096 caratteri).
    """
    agent     = report["agent"].capitalize()
    n_convs   = report["n_convs"]
    timestamp = report["timestamp"][:16].replace("T", " ")
    analysis  = report["analysis"]

    header = (
        f"🧠 *Analisi Mentor — {agent}*\n"
        f"📊 Conversazioni analizzate: {n_convs}\n"
        f"🕐 {timestamp} UTC\n"
        f"{'─' * 30}\n\n"
    )

    full = header + analysis

    # Tronca rispettando il limite Telegram
    if len(full) > 3800:
        full = full[:3800] + "\n\n_(report troncato — continua con /mentor)_"

    return full
