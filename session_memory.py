"""
session_memory.py — Memoria episodica per-utente per-topic

Risolve tre problemi insieme:
1. Guard contestuale — il guard vede gli ultimi N messaggi della sessione
2. Memoria episodica — ogni utente ha la sua sequenza indipendente per topic
3. Separazione utenti — Mario e Victor hanno sessioni separate nello stesso topic

Architettura:
- In-memory (dict) per la sessione attiva — veloce, zero DB per ogni messaggio
- DB (tabella episodes) per la persistenza a lungo termine — pattern e insight
- Sessione = messaggi entro SESSION_TIMEOUT minuti nello stesso (user_id, topic)
"""
import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import Column, String, Text, DateTime, Integer, Float, select
from sqlalchemy.orm import DeclarativeBase

import memory as mem
from agents import call_llm

logger = logging.getLogger(__name__)

SESSION_TIMEOUT  = 30   # minuti — dopo questo tempo inizia una nuova sessione
MAX_SESSION_MSGS = 10   # messaggi massimi tenuti in memoria per sessione


# ── STRUTTURE IN-MEMORY ───────────────────────────────────────────────────────

@dataclass
class SessionMessage:
    role:      str        # "user" | "agent"
    content:   str
    agent:     str        # chi ha risposto (luca, alex, giorgio...)
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Session:
    user_id:   int
    topic:     str
    messages:  list[SessionMessage] = field(default_factory=list)
    session_id: str = ""
    started_at: datetime = field(default_factory=datetime.utcnow)
    last_active: datetime = field(default_factory=datetime.utcnow)

    def is_expired(self) -> bool:
        return datetime.utcnow() - self.last_active > timedelta(minutes=SESSION_TIMEOUT)

    def add_message(self, role: str, content: str, agent: str = ""):
        self.messages.append(SessionMessage(role=role, content=content, agent=agent))
        self.last_active = datetime.utcnow()
        # Mantieni solo gli ultimi N messaggi
        if len(self.messages) > MAX_SESSION_MSGS:
            self.messages = self.messages[-MAX_SESSION_MSGS:]

    def get_context_text(self, max_messages: int = 4) -> str:
        """Formatta gli ultimi N messaggi come contesto leggibile dall'LLM."""
        recent = self.messages[-max_messages:]
        lines  = []
        for msg in recent:
            prefix = "Utente" if msg.role == "user" else f"Agente ({msg.agent})"
            lines.append(f"{prefix}: {msg.content[:200]}")
        return "\n".join(lines)


# Dizionario globale: (user_id, topic) → Session
# defaultdict crea automaticamente sessioni nuove
_sessions: dict[tuple[int, str], Session] = {}
_sessions_lock = asyncio.Lock()


# ── GESTIONE SESSIONI ─────────────────────────────────────────────────────────

async def get_or_create_session(user_id: int, topic: str) -> Session:
    """
    Restituisce la sessione attiva per (user_id, topic).
    Se è scaduta o non esiste, ne crea una nuova e salva la vecchia nel DB.
    """
    async with _sessions_lock:
        key     = (user_id, topic)
        session = _sessions.get(key)

        if session and session.is_expired():
            # Salva la sessione scaduta nel DB in background
            asyncio.ensure_future(_persist_session(session))
            session = None

        if not session:
            session_id = f"{user_id}_{topic}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
            session = Session(
                user_id    = user_id,
                topic      = topic,
                session_id = session_id,
            )
            _sessions[key] = session

        return session


async def add_user_message(user_id: int, topic: str, content: str):
    """Aggiunge un messaggio utente alla sessione."""
    session = await get_or_create_session(user_id, topic)
    session.add_message("user", content, agent="")


async def add_agent_message(user_id: int, topic: str, content: str, agent: str):
    """Aggiunge una risposta dell'agente alla sessione."""
    session = await get_or_create_session(user_id, topic)
    session.add_message("agent", content, agent=agent)


async def get_session_context(user_id: int, topic: str, max_messages: int = 4) -> str:
    """
    Restituisce il contesto della sessione come testo.
    Usato dal guard e dagli agenti per capire il contesto conversazionale.
    """
    session = await get_or_create_session(user_id, topic)
    if not session.messages:
        return ""
    return session.get_context_text(max_messages)


async def get_session_topic(user_id: int, topic: str) -> Optional[str]:
    """
    Inferisce il topic principale della sessione corrente.
    Es: se gli ultimi 3 messaggi parlano di Elden Ring → "Elden Ring"
    Usato dal guard per capire che "quanto dura?" si riferisce all'argomento precedente.
    """
    session = await get_or_create_session(user_id, topic)
    if len(session.messages) < 2:
        return None

    context = session.get_context_text(4)
    result  = await call_llm(
        system="""Estrai il topic principale della conversazione in 1-5 parole.
Es: "Elden Ring", "meteo Milano", "framework Python".
Se non c'è un topic chiaro, rispondi: NESSUNO""",
        messages=[{"role": "user", "content": context}],
        max_tokens=20,
    )
    return None if "NESSUNO" in result else result.strip()


# ── GUARD CONTESTUALE ─────────────────────────────────────────────────────────

async def contextual_guard(
    question: str,
    topic: str,
    user_id: int,
    topic_description: str,
) -> dict:
    """
    Guard che valuta se la domanda è appropriata per il topic,
    tenendo conto del contesto della sessione.

    Restituisce: {"ok": bool, "reason": str}

    Logica:
    1. Se c'è contesto di sessione, lo passa al guard
    2. Il guard ragiona: "nel contesto della conversazione, questa domanda è nel topic?"
    3. "quanto dura?" + contesto "Elden Ring" → ok nel topic news/gaming
    """
    context = await get_session_context(user_id, topic, max_messages=3)

    system = f"""Sei un classificatore di messaggi per un canale Telegram.
Topic corrente: {topic} — {topic_description}

{f"Contesto della conversazione recente:{chr(10)}{context}{chr(10)}" if context else ""}

Valuta se il messaggio dell'utente è appropriato per questo topic,
tenendo conto del contesto conversazionale se presente.

IMPORTANTE: se il messaggio sembra una domanda di follow-up a qualcosa detto nel contesto
(es. "quanto dura?", "e quello?", "perché?"), consideralo appropriato se il contesto è nel topic.

Rispondi SOLO con JSON: {{"ok": true}} oppure {{"ok": false, "reason": "motivo breve"}}"""

    import json as _json
    result = await call_llm(
        system=system,
        messages=[{"role": "user", "content": f"Messaggio: {question}"}],
        max_tokens=60,
    )
    try:
        clean = result.strip().strip("```json").strip("```").strip()
        data  = _json.loads(clean)
        return {"ok": data.get("ok", True), "reason": data.get("reason", "")}
    except Exception:
        return {"ok": True, "reason": ""}


# ── PERSISTENZA EPISODICA (DB) ────────────────────────────────────────────────

class EpisodeBase(DeclarativeBase):
    pass


class Episode(EpisodeBase):
    """
    Episodio persistente — una sessione completata salvata nel DB.
    Usato per analisi di pattern a lungo termine.
    """
    __tablename__ = "episodes"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    session_id     = Column(String,  nullable=False, index=True)
    user_id        = Column(String,  nullable=False, index=True)
    topic          = Column(String,  nullable=False)
    sequence_order = Column(Integer, nullable=False)   # posizione nella sessione
    role           = Column(String,  nullable=False)   # user | agent
    content        = Column(Text,    nullable=False)
    agent          = Column(String,  default="")
    intent         = Column(String,  default="")       # estratto dall'LLM
    created_at     = Column(DateTime, default=datetime.utcnow)
    session_start  = Column(DateTime, nullable=True)


async def init_episode_db():
    if not mem.engine:
        return
    async with mem.engine.begin() as conn:
        await conn.run_sync(EpisodeBase.metadata.create_all)
    logger.info("DB episodi inizializzato ✅")


async def _persist_session(session: Session):
    """
    Salva una sessione completata nel DB come sequenza di episodi.
    Chiamato in background quando la sessione scade.
    """
    if not mem.SessionLocal or not session.messages:
        return
    try:
        # Estrai l'intent principale della sessione con l'LLM
        context = session.get_context_text(6)
        intent_result = await call_llm(
            system="""Descrivi in 5-10 parole cosa stava cercando di fare l'utente in questa conversazione.
Es: "capire quanto dura Elden Ring", "pianificare weekend a Milano", "confrontare framework Python"
Rispondi solo con la descrizione, niente altro.""",
            messages=[{"role": "user", "content": context}],
            max_tokens=30,
        )
        intent = intent_result.strip()

        async with mem.SessionLocal() as db:
            for i, msg in enumerate(session.messages):
                db.add(Episode(
                    session_id     = session.session_id,
                    user_id        = str(session.user_id),
                    topic          = session.topic,
                    sequence_order = i,
                    role           = msg.role,
                    content        = msg.content[:500],
                    agent          = msg.agent,
                    intent         = intent if msg.role == "user" and i == 0 else "",
                    session_start  = session.started_at,
                ))
            await db.commit()
        logger.debug(f"Sessione {session.session_id} persistita ({len(session.messages)} messaggi)")

    except Exception as e:
        logger.error(f"Errore persistenza sessione: {e}")


# ── PATTERN EPISODICI ─────────────────────────────────────────────────────────

async def get_user_episode_pattern(user_id: int, days: int = 14) -> str:
    """
    Analizza le sessioni recenti dell'utente e restituisce insight comportamentali.
    Usato da Sophia per aggiornare il profilo silenzioso e dagli agenti per anticipare.

    Es output: "L'utente di solito chiede il meteo prima di pianificare viaggi.
                Ha una preferenza per città del nord Italia.
                Chiede spesso di giochi action-RPG."
    """
    if not mem.SessionLocal:
        return ""

    try:
        cutoff = datetime.utcnow() - timedelta(days=days)
        async with mem.SessionLocal() as db:
            result = await db.execute(
                select(Episode)
                .where(
                    Episode.user_id == str(user_id),
                    Episode.created_at >= cutoff,
                    Episode.intent != "",   # solo i primi messaggi di ogni sessione
                )
                .order_by(Episode.created_at.desc())
                .limit(20)
            )
            rows = result.scalars().all()

        if not rows:
            return ""

        intents = [r.intent for r in rows if r.intent]
        topics  = [r.topic  for r in rows]

        summary_input = f"""Intent delle sessioni recenti:
{chr(10).join(f'- [{t}] {i}' for t, i in zip(topics, intents))}"""

        pattern = await call_llm(
            system="""Analizza questi intent di sessioni recenti e descrivi i pattern comportamentali dell'utente.
Sii conciso (3-5 frasi). Evidenzia: argomenti ricorrenti, sequenze tipiche, preferenze implicite.
Scrivi in italiano come se stessi briefando un agente su come servire meglio questo utente.""",
            messages=[{"role": "user", "content": summary_input}],
            max_tokens=200,
        )
        return pattern.strip()

    except Exception as e:
        logger.error(f"Errore pattern episodici: {e}")
        return ""


async def get_today_narrative(user_id: int) -> str:
    """
    Ricostruisce la sequenza narrativa delle conversazioni di oggi.
    Usato da Sophia quando chiedi "fammi un riassunto di oggi".
    """
    if not mem.SessionLocal:
        return "Nessun dato disponibile."

    try:
        today = datetime.utcnow().replace(hour=0, minute=0, second=0)
        async with mem.SessionLocal() as db:
            result = await db.execute(
                select(Episode)
                .where(
                    Episode.user_id  == str(user_id),
                    Episode.created_at >= today,
                    Episode.role     == "user",
                )
                .order_by(Episode.created_at)
            )
            rows = result.scalars().all()

        # Usa anche la sessione in-memory per i messaggi più recenti
        current_messages = []
        async with _sessions_lock:
            for (uid, topic), session in _sessions.items():
                if uid == user_id:
                    for msg in session.messages:
                        if msg.role == "user":
                            current_messages.append(f"[{topic}] {msg.content[:100]}")

        if not rows and not current_messages:
            return "Nessuna conversazione oggi."

        db_messages   = [f"[{r.topic}] {r.content[:100]}" for r in rows]
        all_messages  = db_messages + current_messages

        narrative = await call_llm(
            system="""Sei Sophia, la receptionist. Ricostruisci la giornata dell'utente
in modo narrativo e caldo, basandoti sui topic che ha esplorato.
Raggruppa per tema, non per ordine cronologico stretto.
Max 150 parole. Scrivi in italiano.""",
            messages=[{
                "role": "user",
                "content": "Conversazioni di oggi:\n" + "\n".join(all_messages)
            }],
            max_tokens=250,
        )
        return narrative.strip()

    except Exception as e:
        logger.error(f"Errore narrativa giornaliera: {e}")
        return "Errore nel recupero delle conversazioni."
