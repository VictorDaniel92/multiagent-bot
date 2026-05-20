"""
agent_state.py — Stato persistente per-agente e condiviso.

Due tabelle:
  agent_state        — stato individuale per-agente/per-utente
  agent_shared_state — stato condiviso visibile a tutti gli agenti
"""

import json
import logging
from datetime import datetime
from typing import Any
import time

from sqlalchemy import Column, String, Text, Float, Integer, DateTime, select
from sqlalchemy.orm import DeclarativeBase

import memory as mem
from agents import call_llm

logger = logging.getLogger(__name__)

# Cache in-memory per get_all_shared_states (invalidata quando uno stato viene scritto)
_shared_states_cache: dict = {"data": None, "ts": 0.0}


# ── MODELLI DB ────────────────────────────────────────────────────────────────

class StateBase(DeclarativeBase):
    pass


class AgentState(StateBase):
    """
    Stato individuale: ogni agente ha una riga per ogni utente.
    Tiene traccia di cosa sa di quell'utente, com'è il rapporto, cosa sta monitorando.
    """
    __tablename__ = "agent_state"

    agent_name        = Column(String,  primary_key=True)
    user_id           = Column(String,  primary_key=True)
    perspective       = Column(Text,    nullable=False, default="")
    mood              = Column(Float,   nullable=False, default=0.5)   # 0.0-1.0
    active_goals      = Column(Text,    nullable=False, default="[]")  # JSON list
    last_interaction  = Column(DateTime, default=datetime.utcnow)
    interaction_count = Column(Integer, nullable=False, default=0)


class AgentSharedState(StateBase):
    """
    Stato condiviso: una riga per agente, visibile a tutti gli altri.
    Permette agli agenti di "sapere" cosa stanno facendo i colleghi.
    """
    __tablename__ = "agent_shared_state"

    agent_name     = Column(String,  primary_key=True)
    current_focus  = Column(Text,    nullable=False, default="")
    recent_insight = Column(Text,    nullable=False, default="")
    updated_at     = Column(DateTime, default=datetime.utcnow)


# ── INIT ──────────────────────────────────────────────────────────────────────

async def init_agent_state_db():
    if not mem.engine:
        logger.warning("Engine DB non disponibile — agent_state DB non inizializzato")
        return
    async with mem.engine.begin() as conn:
        await conn.run_sync(StateBase.metadata.create_all)
    logger.info("DB agent_state inizializzato ✅")


# ── CRUD: STATO INDIVIDUALE ───────────────────────────────────────────────────

async def get_agent_state(agent_name: str, user_id: int) -> dict:
    """Carica lo stato di un agente per un utente specifico."""
    if not mem.SessionLocal:
        return _default_state(agent_name, user_id)

    async with mem.SessionLocal() as db:
        result = await db.execute(
            select(AgentState).where(
                AgentState.agent_name == agent_name,
                AgentState.user_id    == str(user_id),
            )
        )
        row = result.scalar_one_or_none()

    if not row:
        return _default_state(agent_name, user_id)

    return {
        "agent_name":        row.agent_name,
        "user_id":           row.user_id,
        "perspective":       row.perspective,
        "mood":              row.mood,
        "active_goals":      json.loads(row.active_goals or "[]"),
        "last_interaction":  row.last_interaction,
        "interaction_count": row.interaction_count,
    }


async def save_agent_state(agent_name: str, user_id: int, **kwargs):
    """
    Aggiorna lo stato di un agente per un utente.
    Accetta campi: perspective, mood, active_goals (lista), interaction_count.
    """
    if not mem.SessionLocal:
        return

    async with mem.SessionLocal() as db:
        result = await db.execute(
            select(AgentState).where(
                AgentState.agent_name == agent_name,
                AgentState.user_id    == str(user_id),
            )
        )
        row = result.scalar_one_or_none()

        if not row:
            row = AgentState(agent_name=agent_name, user_id=str(user_id))
            db.add(row)

        if "perspective" in kwargs:
            row.perspective = kwargs["perspective"]
        if "mood" in kwargs:
            row.mood = max(0.0, min(1.0, float(kwargs["mood"])))
        if "active_goals" in kwargs:
            row.active_goals = json.dumps(kwargs["active_goals"], ensure_ascii=False)
        if "interaction_count" in kwargs:
            row.interaction_count = kwargs["interaction_count"]

        row.last_interaction = datetime.utcnow()
        await db.commit()


async def increment_interaction(agent_name: str, user_id: int):
    """Incrementa il contatore di interazioni — chiamato dopo ogni risposta."""
    state = await get_agent_state(agent_name, user_id)
    await save_agent_state(
        agent_name, user_id,
        interaction_count=state["interaction_count"] + 1,
    )


# ── MOOD ──────────────────────────────────────────────────────────────────────

# Etichette human-readable per i valori numerici del mood
MOOD_LABELS = {
    (0.0, 0.2): "deluso",
    (0.2, 0.4): "laconico",
    (0.4, 0.6): "neutro",
    (0.6, 0.8): "coinvolto",
    (0.8, 1.0): "entusiasta",
}

def mood_label(mood: float) -> str:
    for (lo, hi), label in MOOD_LABELS.items():
        if lo <= mood < hi:
            return label
    return "entusiasta" if mood >= 1.0 else "neutro"


async def update_mood(agent_name: str, user_id: int, delta: float):
    """
    Aggiusta il mood di ±delta.
    delta > 0 → più entusiasta (es. utente fa follow-up, mostra interesse)
    delta < 0 → più laconico  (es. utente ignora, cambia topic bruscamente)
    """
    state = await get_agent_state(agent_name, user_id)
    new_mood = max(0.0, min(1.0, state["mood"] + delta))
    await save_agent_state(agent_name, user_id, mood=new_mood)
    logger.debug(f"Mood {agent_name}/{user_id}: {state['mood']:.2f} → {new_mood:.2f}")
    return new_mood


# ── GOAL MANAGEMENT ───────────────────────────────────────────────────────────

async def add_goal(agent_name: str, user_id: int, goal: str):
    """Aggiunge un obiettivo attivo alla lista (max 10)."""
    state = await get_agent_state(agent_name, user_id)
    goals = state["active_goals"]
    if goal not in goals:
        goals = (goals + [goal])[-10:]  # mantieni solo gli ultimi 10
        await save_agent_state(agent_name, user_id, active_goals=goals)


async def remove_goal(agent_name: str, user_id: int, goal: str):
    state = await get_agent_state(agent_name, user_id)
    goals = [g for g in state["active_goals"] if g != goal]
    await save_agent_state(agent_name, user_id, active_goals=goals)


async def get_goals(agent_name: str, user_id: int) -> list[str]:
    state = await get_agent_state(agent_name, user_id)
    return state["active_goals"]


# ── PERSPECTIVE ───────────────────────────────────────────────────────────────

async def update_perspective(
    agent_name: str, user_id: int,
    current_perspective: str, new_exchange: str,
) -> str:
    """
    Aggiorna la 'visione' che l'agente ha dell'utente, incorporando
    la nuova interazione. Usa il LLM per sintetizzare.
    Ritorna la nuova perspective (stringa).
    Throttle: aggiorna solo ogni 5 interazioni per risparmiare token.
    """
    if not current_perspective and not new_exchange:
        return current_perspective or ""

    # Throttle — aggiorna solo ogni 5 interazioni
    state = await get_agent_state(agent_name, user_id)
    count = state.get("interaction_count", 0)
    if count > 0 and count % 5 != 0:
        return current_perspective or ""

    prompt = f"""Sei {agent_name}. Stai aggiornando la tua comprensione di questo utente.

Prospettiva attuale (quello che già sai):
{current_perspective or "Prima interazione — nessuna prospettiva precedente."}

Nuova interazione:
{new_exchange}

Aggiorna la prospettiva incorporando le nuove informazioni.
Sii conciso (max 150 parole). Includi:
- Interessi ricorrenti o nuovi
- Pattern di comportamento osservati
- Preferenze emerse
- Eventuali obiettivi espressi

Scrivi in italiano, tono neutro e descrittivo."""

    return await call_llm(
        system=f"Sei {agent_name}, un agente AI. Aggiorna la tua comprensione dell'utente.",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=200,
    )


# ── CRUD: STATO CONDIVISO ─────────────────────────────────────────────────────

async def get_shared_state(agent_name: str) -> dict:
    """Carica lo stato condiviso di un agente."""
    if not mem.SessionLocal:
        return {"agent_name": agent_name, "current_focus": "", "recent_insight": ""}

    async with mem.SessionLocal() as db:
        result = await db.execute(
            select(AgentSharedState).where(
                AgentSharedState.agent_name == agent_name
            )
        )
        row = result.scalar_one_or_none()

    if not row:
        return {"agent_name": agent_name, "current_focus": "", "recent_insight": ""}

    return {
        "agent_name":     row.agent_name,
        "current_focus":  row.current_focus,
        "recent_insight": row.recent_insight,
        "updated_at":     row.updated_at,
    }


async def set_shared_state(agent_name: str, current_focus: str = None, recent_insight: str = None):
    """Aggiorna lo stato condiviso di un agente."""
    if not mem.SessionLocal:
        return

    async with mem.SessionLocal() as db:
        result = await db.execute(
            select(AgentSharedState).where(
                AgentSharedState.agent_name == agent_name
            )
        )
        row = result.scalar_one_or_none()

        if not row:
            row = AgentSharedState(agent_name=agent_name)
            db.add(row)

        if current_focus  is not None: row.current_focus  = current_focus
        if recent_insight is not None: row.recent_insight = recent_insight
        row.updated_at = datetime.utcnow()
        await db.commit()
    # Invalida la cache
    _shared_states_cache["ts"] = 0.0


async def get_all_shared_states() -> list[dict]:
    """Carica lo stato condiviso di tutti gli agenti — usato da Sophia.
    Cache 60s in memoria per evitare una query DB per ogni messaggio."""
    if not mem.SessionLocal:
        return []

    now = time.monotonic()
    if now - _shared_states_cache["ts"] < 60 and _shared_states_cache["data"] is not None:
        return _shared_states_cache["data"]

    async with mem.SessionLocal() as db:
        result = await db.execute(select(AgentSharedState))
        rows = result.scalars().all()

    data = [
        {
            "agent_name":     r.agent_name,
            "current_focus":  r.current_focus,
            "recent_insight": r.recent_insight,
            "updated_at":     r.updated_at,
        }
        for r in rows
    ]
    _shared_states_cache["data"] = data
    _shared_states_cache["ts"]   = now
    return data


def format_shared_states_for_prompt(states: list[dict]) -> str:
    """
    Formatta gli stati condivisi come contesto per il prompt di Sophia
    o di qualsiasi agente che voglia sapere cosa stanno facendo gli altri.
    """
    if not states:
        return ""

    lines = ["## Stato attuale degli agenti colleghi\n"]
    for s in states:
        name    = s["agent_name"].capitalize()
        focus   = s.get("current_focus",  "") or "—"
        insight = s.get("recent_insight", "") or "—"
        lines.append(f"**{name}**\n  Focus: {focus}\n  Insight: {insight}\n")

    return "\n".join(lines)


# ── CONTEXT BUILDER ───────────────────────────────────────────────────────────

async def build_agent_context(agent_name: str, user_id: int) -> str:
    """
    Costruisce il blocco di contesto da iniettare nel system prompt di un agente.
    Include: stato individuale (perspective, mood, goals) + stati condivisi colleghi.
    """
    state        = await get_agent_state(agent_name, user_id)
    shared_all   = await get_all_shared_states()
    # Escludi lo stato dell'agente stesso
    shared_other = [s for s in shared_all if s["agent_name"] != agent_name]

    mood_str  = mood_label(state["mood"])
    goals_str = "\n".join(f"  - {g}" for g in state["active_goals"]) or "  — nessuno"

    lines = [
        f"## Il tuo stato interno [{agent_name}]",
        f"Mood attuale: {mood_str} ({state['mood']:.1f})",
        f"Interazioni con questo utente: {state['interaction_count']}",
    ]

    if state["perspective"]:
        lines.append(f"\nProspettiva sull'utente:\n{state['perspective']}")

    if state["active_goals"]:
        lines.append(f"\nObiettivi attivi:\n{goals_str}")

    if shared_other:
        lines.append("")
        lines.append(format_shared_states_for_prompt(shared_other))

    return "\n".join(lines)


# ── UTILS ─────────────────────────────────────────────────────────────────────

def _default_state(agent_name: str, user_id: int) -> dict:
    return {
        "agent_name":        agent_name,
        "user_id":           str(user_id),
        "perspective":       "",
        "mood":              0.5,
        "active_goals":      [],
        "last_interaction":  None,
        "interaction_count": 0,
    }


async def extract_goals_from_message(agent_name: str, question: str) -> list[str]:
    """
    Usa il LLM per capire se il messaggio esprime un obiettivo da monitorare.
    Es: "segui l'uscita di GTA6" → ["monitorare uscita GTA6"]
    Ritorna lista vuota se non ci sono obiettivi espliciti.
    Fast-path: se non ci sono keyword trigger, salta la chiamata LLM.
    """
    # Fast-path: chiama l'LLM solo se ci sono keyword esplicite di monitoraggio
    _GOAL_TRIGGERS = (
        "segui", "seguimi", "avvisami", "monitora", "ricordami",
        "fammi sapere", "tienimi aggiornato", "notificami", "avvisa",
        "quando esce", "quando uscirà", "aspetto", "voglio sapere quando",
    )
    q_lower = question.lower()
    if not any(t in q_lower for t in _GOAL_TRIGGERS):
        return []

    raw = await call_llm(
        system=(
            "Sei un estrattore di obiettivi. "
            "Dal messaggio dell'utente, estrai obiettivi espliciti da monitorare nel tempo. "
            "Un obiettivo è qualcosa come 'segui X', 'avvisami di Y', 'monitora Z'. "
            "Rispondi SOLO con una lista JSON di stringhe brevi (max 60 car ciascuna). "
            "Se non ci sono obiettivi espliciti, rispondi: []"
        ),
        messages=[{"role": "user", "content": question}],
        max_tokens=100,
    )
    try:
        import re
        raw = re.search(r"\[.*\]", raw, re.DOTALL)
        return json.loads(raw.group()) if raw else []
    except Exception:
        return []
