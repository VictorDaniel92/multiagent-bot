"""
mentor_agent.py — Il Mentor: analisi e proposta di miglioramento degli agenti.

Architettura (step attuali):
  1. ANALISI  — legge le ultime N conversazioni per agente dal vector store
               + storico modifiche precedenti per evitare rollback
  2. PROPOSTA — genera report con diff al soul.md
  3. APPROVAZIONE — bottoni inline ✅ ✏️ ❌
  4. APPLICAZIONE — scrive soul.md e ricarica in memoria
"""

import json
import logging
from pathlib import Path
from datetime import datetime

from sqlalchemy import Column, String, Text, DateTime, select
from sqlalchemy.orm import DeclarativeBase

from agents import call_llm, load_soul
from memory_vector import get_recent_conversations
import memory as mem

logger = logging.getLogger(__name__)

SOULS_DIR   = Path(__file__).parent / "souls"
SOUL_MENTOR = load_soul("mentor")


# ── STORICO MODIFICHE ─────────────────────────────────────────────────────────

class MentorBase(DeclarativeBase):
    pass


class MentorHistory(MentorBase):
    """
    Storico di tutte le proposte del Mentor, applicate o rifiutate.
    Iniettato nel prompt ad ogni nuova analisi per evitare rollback involontari.
    """
    __tablename__ = "mentor_history"

    id          = Column(String,   primary_key=True)   # proposal_id
    agent_name  = Column(String,   nullable=False)
    proposed_at = Column(DateTime, default=datetime.utcnow)
    status      = Column(String,   nullable=False, default="pending")
                                                       # pending / applied / rejected
    analysis_summary = Column(Text, nullable=False, default="")  # breve sintesi
    diff_description = Column(Text, nullable=False, default="")  # cosa cambiava
    applied_at       = Column(DateTime, nullable=True)


async def init_mentor_db():
    if not mem.engine:
        logger.warning("Engine DB non disponibile — mentor DB non inizializzato")
        return
    async with mem.engine.begin() as conn:
        await conn.run_sync(MentorBase.metadata.create_all)
    logger.info("DB mentor_history inizializzato ✅")


async def save_mentor_proposal(
    proposal_id: str, agent_name: str,
    analysis_summary: str, diff_description: str,
):
    if not mem.SessionLocal:
        return
    async with mem.SessionLocal() as db:
        existing = await db.execute(
            select(MentorHistory).where(MentorHistory.id == proposal_id)
        )
        if not existing.scalar_one_or_none():
            db.add(MentorHistory(
                id=proposal_id,
                agent_name=agent_name,
                analysis_summary=analysis_summary,
                diff_description=diff_description,
                status="pending",
            ))
            await db.commit()


async def mark_proposal_applied(proposal_id: str):
    if not mem.SessionLocal:
        return
    async with mem.SessionLocal() as db:
        result = await db.execute(
            select(MentorHistory).where(MentorHistory.id == proposal_id)
        )
        row = result.scalar_one_or_none()
        if row:
            row.status     = "applied"
            row.applied_at = datetime.utcnow()
            await db.commit()


async def mark_proposal_rejected(proposal_id: str):
    if not mem.SessionLocal:
        return
    async with mem.SessionLocal() as db:
        result = await db.execute(
            select(MentorHistory).where(MentorHistory.id == proposal_id)
        )
        row = result.scalar_one_or_none()
        if row:
            row.status = "rejected"
            await db.commit()


async def get_agent_history(agent_name: str, limit: int = 10) -> list[dict]:
    """Carica le ultime N proposte per un agente — da iniettare nel prompt."""
    if not mem.SessionLocal:
        return []
    async with mem.SessionLocal() as db:
        result = await db.execute(
            select(MentorHistory)
            .where(MentorHistory.agent_name == agent_name)
            .order_by(MentorHistory.proposed_at.desc())
            .limit(limit)
        )
        rows = result.scalars().all()
    return [
        {
            "id":               r.id,
            "proposed_at":      str(r.proposed_at)[:16],
            "status":           r.status,
            "analysis_summary": r.analysis_summary,
            "diff_description": r.diff_description,
            "applied_at":       str(r.applied_at)[:16] if r.applied_at else None,
        }
        for r in rows
    ]


def _format_history_for_prompt(history: list[dict]) -> str:
    """Formatta lo storico per il prompt del Mentor."""
    if not history:
        return ""

    lines = ["## Storico modifiche precedenti (non riproporre quanto già applicato)\n"]
    for h in history:
        status_icon = {"applied": "✅", "rejected": "❌", "pending": "⏳"}.get(h["status"], "?")
        lines.append(
            f"{status_icon} [{h['proposed_at']}] {h['status'].upper()}\n"
            f"   Modifica: {h['diff_description']}\n"
            f"   Analisi:  {h['analysis_summary'][:120]}\n"
        )
    return "\n".join(lines)


# Agenti analizzabili dal Mentor e il loro nome nel vector store
# Mappa nome_logico → nome_agente_nel_DB (come salvato da save_conversation in bot.py)
# Max e Sofia sono interni al pipeline — le loro conversazioni vengono salvate come "alex"
# Per analizzarli usiamo le stesse conversazioni di alex + il briefing che producono
ANALYZABLE_AGENTS = {
    "alex":   "alex",    # pipeline generale Max→Sofia→Alex
    "luca":   "luca",    # topic news / gaming
    "giorgio": "giorgio", # topic meteo
    "marco":  "marco",   # topic viaggi
}

# Descrizione estesa per il Mentor — spiega il ruolo di ogni agente
AGENT_DESCRIPTIONS = {
    "alex":    "Risponde alle domande generali (ricerca, coding, brainstorming, analisi). "
               "Usa il lavoro interno di Max (pianificatore) e Sofia (ricercatrice).",
    "luca":    "Critico videoludico. Risponde nel topic news/gaming con stile editoriale.",
    "giorgio": "Meteorologo. Risponde nel topic meteo con previsioni e briefing mattutini.",
    "marco":   "Esperto di viaggi. Risponde nel topic viaggi.",
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
    logger.info(f"Recuperate {len(conversations)} conversazioni per {agent_name} (DB key: {agent_key})")
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
    agent_desc   = AGENT_DESCRIPTIONS.get(agent_name, "")

    # Carica storico modifiche per evitare rollback
    history      = await get_agent_history(agent_name, limit=10)
    history_text = _format_history_for_prompt(history)

    conv_text = _format_conversations_for_analysis(conversations)

    system = f"""{SOUL_MENTOR}

Stai analizzando le conversazioni recenti dell'agente **{agent_name}**.
Ruolo: {agent_desc}

{soul_section}

{history_text}

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


# ── STEP 2: ESTRAZIONE E APPLICAZIONE PROPOSTA ───────────────────────────────

async def mentor_extract_proposed_soul(
    agent_name: str, analysis: str, current_soul: str
) -> str:
    """
    Partendo dall'analisi già fatta, genera il testo COMPLETO del nuovo soul.md.
    Questo è ciò che verrà scritto su disco se l'utente approva.
    """
    system = f"""{SOUL_MENTOR}

Hai appena prodotto un'analisi dell'agente {agent_name}.
Ora devi generare il testo COMPLETO e AGGIORNATO del soul.md dell'agente,
applicando le modifiche proposte nell'analisi.

Regole:
- Restituisci SOLO il testo del nuovo soul.md, senza preamboli né spiegazioni
- Mantieni la struttura Markdown (titoli ##, liste -)
- Applica SOLO le modifiche proposte nella sezione "Proposta di modifica"
- Tutto il resto del soul deve rimanere invariato
- Non aggiungere sezioni non presenti nell'originale"""

    return await call_llm(
        system=system,
        messages=[{
            "role": "user",
            "content": (
                f"Soul attuale di {agent_name}:\n```\n{current_soul}\n```\n\n"
                f"Analisi e proposta di modifica:\n{analysis}"
            )
        }],
        max_tokens=1200,
    )


def apply_soul_change(agent_name: str, new_soul_content: str) -> bool:
    """
    Scrive il nuovo soul.md su disco.
    Fa un backup del file precedente come soul_name.bak.md.
    Ritorna True se riuscito.
    """
    soul_path = SOULS_DIR / f"{agent_name}.md"
    bak_path  = SOULS_DIR / f"{agent_name}.bak.md"

    try:
        # Backup del soul precedente
        if soul_path.exists():
            bak_path.write_text(soul_path.read_text(encoding="utf-8"), encoding="utf-8")

        soul_path.write_text(new_soul_content.strip(), encoding="utf-8")
        logger.info(f"Soul '{agent_name}' scritto su disco ✅")
        return True

    except Exception as e:
        logger.error(f"Errore scrittura soul '{agent_name}': {e}")
        return False


def reload_agent_soul(agent_name: str) -> bool:
    """
    Ricarica il soul in memoria (aggiorna i global in agents.py).
    Da chiamare subito dopo apply_soul_change.
    """
    from agents import reload_soul
    return reload_soul(agent_name)


def restore_soul_backup(agent_name: str) -> bool:
    """Ripristina il backup del soul in caso di rollback."""
    soul_path = SOULS_DIR / f"{agent_name}.md"
    bak_path  = SOULS_DIR / f"{agent_name}.bak.md"

    if not bak_path.exists():
        logger.warning(f"Nessun backup disponibile per '{agent_name}'")
        return False

    try:
        soul_path.write_text(bak_path.read_text(encoding="utf-8"), encoding="utf-8")
        reload_agent_soul(agent_name)
        logger.info(f"Soul '{agent_name}' ripristinato dal backup ✅")
        return True
    except Exception as e:
        logger.error(f"Errore ripristino backup '{agent_name}': {e}")
        return False


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
