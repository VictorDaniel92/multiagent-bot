import os
import json
import logging
from datetime import datetime
from sqlalchemy import Column, String, Text, DateTime, select, delete
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "").replace("postgresql://", "postgresql+asyncpg://")

engine       = None
SessionLocal = None


async def init_db():
    """Inizializza il DB — chiamato all'avvio del bot."""
    global engine, SessionLocal
    if not DATABASE_URL:
        logger.warning("DATABASE_URL non configurato — memoria disabilitata")
        return

    engine       = create_async_engine(DATABASE_URL, echo=False)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("DB memoria inizializzato")


class Base(DeclarativeBase):
    pass


class Memory(Base):
    """
    Tabella chiave-valore per la memoria degli utenti.
    Ogni riga è un fatto che il sistema ricorda su un utente specifico.
    """
    __tablename__ = "memory"

    user_id    = Column(String, primary_key=True)
    key        = Column(String, primary_key=True)   # es. "name", "topics", "preferences"
    value      = Column(Text, nullable=False)        # JSON serializzato
    updated_at = Column(DateTime, default=datetime.utcnow)


# ── CRUD ──────────────────────────────────────────────────────────────────────

async def get_memory(user_id: int) -> dict:
    """Carica tutta la memoria di un utente come dizionario."""
    if not SessionLocal:
        return {}
    try:
        async with SessionLocal() as db:
            result = await db.execute(
                select(Memory).where(Memory.user_id == str(user_id))
            )
            rows = result.scalars().all()
            return {row.key: json.loads(row.value) for row in rows}
    except Exception as e:
        logger.error(f"Errore lettura memoria: {e}")
        return {}


async def set_memory(user_id: int, key: str, value) -> None:
    """Salva o aggiorna un singolo fatto nella memoria dell'utente."""
    if not SessionLocal:
        return
    try:
        async with SessionLocal() as db:
            # Cerca se esiste già
            result = await db.execute(
                select(Memory).where(Memory.user_id == str(user_id), Memory.key == key)
            )
            existing = result.scalar_one_or_none()

            if existing:
                existing.value      = json.dumps(value, ensure_ascii=False)
                existing.updated_at = datetime.utcnow()
            else:
                db.add(Memory(
                    user_id    = str(user_id),
                    key        = key,
                    value      = json.dumps(value, ensure_ascii=False),
                    updated_at = datetime.utcnow()
                ))
            await db.commit()
    except Exception as e:
        logger.error(f"Errore scrittura memoria: {e}")


async def update_topics(user_id: int, new_topics: list[str]) -> None:
    """
    Aggiorna i topic di interesse dell'utente mantenendo gli ultimi 10.
    Chiamato dopo ogni conversazione per tracciare gli interessi.
    """
    memory   = await get_memory(user_id)
    existing = memory.get("recent_topics", [])

    # Aggiungi i nuovi topic senza duplicati, mantieni gli ultimi 10
    combined = list(dict.fromkeys(new_topics + existing))[:10]
    await set_memory(user_id, "recent_topics", combined)


async def format_memory_for_prompt(user_id: int) -> str:
    """
    Formatta la memoria dell'utente come testo da iniettare nel system prompt.
    Restituisce stringa vuota se non c'è memoria.
    """
    memory = await get_memory(user_id)
    if not memory:
        return ""

    lines = ["## Memoria utente"]

    if "name" in memory:
        lines.append(f"- Nome: {memory['name']}")
    if "preferences" in memory:
        lines.append(f"- Preferenze: {memory['preferences']}")
    if "recent_topics" in memory and memory["recent_topics"]:
        topics = ", ".join(memory["recent_topics"][:5])
        lines.append(f"- Topic recenti: {topics}")
    if "notes" in memory:
        lines.append(f"- Note: {memory['notes']}")

    return "\n".join(lines)
