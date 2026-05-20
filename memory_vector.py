"""
memory_vector.py — Memoria semantica persistente con pgvector + Cohere embeddings.

Ogni conversazione viene salvata come vettore. Prima di rispondere,
gli agenti cercano i ricordi più simili alla domanda corrente e li
iniettano nel contesto.

Tabella `conversations`:
  id          SERIAL PRIMARY KEY
  user_id     TEXT
  topic       TEXT          — es. "meteo", "news", "ricerca"
  agent       TEXT          — es. "giorgio", "luca", "alex"
  question    TEXT
  answer      TEXT
  embedding   vector(1024)  — Cohere embed-multilingual-v3.0 = 1024 dim
  created_at  TIMESTAMP
"""

import os
import logging
import asyncio
from datetime import datetime
from typing import Optional

import httpx
import asyncpg

logger = logging.getLogger(__name__)

COHERE_API_KEY = os.environ.get("COHERE_API_KEY", "")
COHERE_MODEL   = "embed-multilingual-v3.0"
EMBED_DIM      = 1024

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Pool di connessioni asyncpg (separato da SQLAlchemy usato in memory.py)
_pool: Optional[asyncpg.Pool] = None

# Client HTTP persistente per Cohere
_cohere_client: Optional[httpx.AsyncClient] = None

async def _get_cohere_client() -> httpx.AsyncClient:
    global _cohere_client
    if _cohere_client is None or _cohere_client.is_closed:
        _cohere_client = httpx.AsyncClient(timeout=15)
    return _cohere_client


# ── Connessione ────────────────────────────────────────────────────────────────

async def init_vector_db():
    """Inizializza il pool e crea la tabella se non esiste."""
    global _pool
    if not DATABASE_URL:
        logger.warning("DATABASE_URL non configurato — vector memory disabilitata")
        return

    try:
        # asyncpg vuole postgresql:// non postgresql+asyncpg://
        url = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
        _pool = await asyncpg.create_pool(url, min_size=1, max_size=5)

        async with _pool.acquire() as conn:
            # Assicura che pgvector sia abilitato
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")

            # Tabella conversazioni
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id         SERIAL PRIMARY KEY,
                    user_id    TEXT        NOT NULL,
                    topic      TEXT        NOT NULL DEFAULT 'general',
                    agent      TEXT        NOT NULL DEFAULT 'system',
                    question   TEXT        NOT NULL,
                    answer     TEXT        NOT NULL,
                    embedding  vector(1024),
                    created_at TIMESTAMP   NOT NULL DEFAULT NOW()
                )
            """)

            # Indice HNSW per ricerche veloci per similarità
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS conversations_embedding_idx
                ON conversations
                USING hnsw (embedding vector_cosine_ops)
            """)

            # Indici B-tree per query frequenti (topic, agent, created_at)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS conversations_topic_idx
                ON conversations (topic)
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS conversations_user_topic_idx
                ON conversations (user_id, topic)
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS conversations_created_at_idx
                ON conversations (created_at DESC)
            """)

        logger.info("Vector DB inizializzato ✅")

    except Exception as e:
        logger.error(f"Errore init vector DB: {e}", exc_info=True)
        _pool = None


# ── Embeddings via Cohere ──────────────────────────────────────────────────────

async def get_embedding(text: str) -> list[float] | None:
    """Chiama Cohere per ottenere l'embedding di un testo."""
    if not COHERE_API_KEY:
        return None
    try:
        client = await _get_cohere_client()
        response = await client.post(
            "https://api.cohere.com/v2/embed",
            headers={
                "Authorization": f"Bearer {COHERE_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model":      COHERE_MODEL,
                "texts":      [text[:2048]],
                "input_type": "search_document",
                "embedding_types": ["float"],
            }
        )
        data = response.json()
        if response.status_code != 200:
            logger.error(f"Cohere error {response.status_code}: {data}")
            return None
        return data["embeddings"]["float"][0]
    except Exception as e:
        logger.error(f"Errore embedding Cohere: {e}")
        return None


async def get_query_embedding(text: str) -> list[float] | None:
    """Embedding per query (input_type diverso per Cohere)."""
    if not COHERE_API_KEY:
        return None
    try:
        client = await _get_cohere_client()
        response = await client.post(
            "https://api.cohere.com/v2/embed",
            headers={
                "Authorization": f"Bearer {COHERE_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model":      COHERE_MODEL,
                "texts":      [text[:2048]],
                "input_type": "search_query",
                "embedding_types": ["float"],
            }
        )
        data = response.json()
        if response.status_code != 200:
            return None
        return data["embeddings"]["float"][0]
    except Exception as e:
        logger.error(f"Errore query embedding: {e}")
        return None


# ── Salvataggio ────────────────────────────────────────────────────────────────

async def save_conversation(
    user_id: int,
    topic: str,
    agent: str,
    question: str,
    answer: str,
) -> None:
    """
    Salva una conversazione con il suo embedding.
    Chiamato dopo ogni risposta degli agenti.
    Fire-and-forget: non blocca la risposta all'utente.
    """
    if not _pool:
        return

    # Testo da embeddare: domanda + risposta per catturare il contesto completo
    text_to_embed = f"Domanda: {question}\nRisposta: {answer[:500]}"

    async def _save():
        try:
            embedding = await get_embedding(text_to_embed)
            async with _pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO conversations
                        (user_id, topic, agent, question, answer, embedding, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6::vector, NOW())
                """,
                    str(user_id), topic, agent, question, answer[:2000],
                    str(embedding) if embedding else None
                )
        except Exception as e:
            logger.error(f"Errore salvataggio conversazione: {e}")

    # Lancia in background senza aspettare
    asyncio.create_task(_save())


# ── Ricerca per similarità ─────────────────────────────────────────────────────

async def search_memories(
    query: str,
    user_id: int | None = None,
    topic: str | None = None,
    limit: int = 3,
    min_similarity: float = 0.75,
) -> list[dict]:
    """
    Cerca i ricordi più simili alla query.

    Args:
        query:          testo da cercare
        user_id:        se specificato, cerca solo le conversazioni di quell'utente
        topic:          se specificato, filtra per topic
        limit:          quanti risultati restituire
        min_similarity: soglia minima di similarità coseno (0-1)

    Returns:
        lista di dict con keys: question, answer, agent, topic, created_at, similarity
    """
    if not _pool:
        return []

    embedding = await get_query_embedding(query)
    if not embedding:
        return []

    try:
        async with _pool.acquire() as conn:
            # Costruisce la query dinamicamente in base ai filtri
            conditions = ["embedding IS NOT NULL"]
            params     = [str(embedding)]  # $1 = vettore query
            idx        = 2

            if user_id is not None:
                conditions.append(f"user_id = ${idx}")
                params.append(str(user_id))
                idx += 1

            if topic:
                conditions.append(f"topic = ${idx}")
                params.append(topic)
                idx += 1

            where = " AND ".join(conditions)

            rows = await conn.fetch(f"""
                SELECT
                    question, answer, agent, topic, created_at,
                    1 - (embedding <=> $1::vector) AS similarity
                FROM conversations
                WHERE {where}
                  AND 1 - (embedding <=> $1::vector) >= {min_similarity}
                ORDER BY embedding <=> $1::vector
                LIMIT {limit}
            """, *params)

            return [dict(r) for r in rows]

    except Exception as e:
        logger.error(f"Errore ricerca memoria: {e}")
        return []


async def get_recent_conversations(
    topic: str | None = None,
    agent: str | None = None,
    limit: int = 5,
) -> list[dict]:
    """
    Recupera le conversazioni più recenti (senza ricerca vettoriale).
    Usato da Sophia per il recap e per rispondere a "di cosa si è parlato?"
    """
    if not _pool:
        return []

    try:
        async with _pool.acquire() as conn:
            conditions = []
            params     = []
            idx        = 1

            if topic:
                conditions.append(f"topic = ${idx}")
                params.append(topic)
                idx += 1
            if agent:
                conditions.append(f"agent = ${idx}")
                params.append(agent)
                idx += 1

            where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

            rows = await conn.fetch(f"""
                SELECT question, answer, agent, topic, created_at
                FROM conversations
                {where}
                ORDER BY created_at DESC
                LIMIT {limit}
            """, *params)

            return [dict(r) for r in rows]

    except Exception as e:
        logger.error(f"Errore lettura conversazioni recenti: {e}")
        return []


# ── Formattazione per il prompt ────────────────────────────────────────────────

def format_memories_for_prompt(memories: list[dict], label: str = "Ricordi rilevanti") -> str:
    """
    Converte i ricordi in testo da iniettare nel system prompt.
    """
    if not memories:
        return ""

    lines = [f"## {label}"]
    for m in memories:
        ago = _time_ago(m["created_at"])
        lines.append(
            f"- [{ago} · {m['topic']} · {m['agent']}] "
            f"D: {m['question'][:100]} → R: {m['answer'][:150]}"
        )
    return "\n".join(lines)


def _time_ago(dt: datetime) -> str:
    """Converte un datetime in stringa relativa tipo '2 ore fa'."""
    try:
        diff = datetime.utcnow() - dt.replace(tzinfo=None)
        seconds = int(diff.total_seconds())
        if seconds < 3600:
            return f"{seconds // 60}min fa"
        if seconds < 86400:
            return f"{seconds // 3600}h fa"
        return f"{seconds // 86400}g fa"
    except Exception:
        return "recente"
