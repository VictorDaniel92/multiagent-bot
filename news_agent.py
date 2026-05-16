import logging
from pathlib import Path
from datetime import date, timedelta

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import Column, String, Text, Date, select, delete
from sqlalchemy.orm import DeclarativeBase

from agents import call_llm
import memory

logger = logging.getLogger(__name__)

# ── CONFIGURAZIONE ────────────────────────────────────────────────────────────

NEWS_PAGE_URL = "https://multiplayer.it/articoli/notizie/"
BASE_URL      = "https://multiplayer.it"
SOULS_DIR     = Path(__file__).parent / "souls"

SKIP_PATTERNS = [
    "aliexpress", "instant-gaming", "amazon", "offerta",
    "sconto", "coupon", "risparmia", "cashback", "ebay",
]

# ── SOUL ──────────────────────────────────────────────────────────────────────

def _load_soul(name: str) -> str:
    path = SOULS_DIR / f"{name}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    logger.warning(f"Soul non trovato: {path}")
    return ""

SOUL_LUCA = _load_soul("luca")


# ── MODELLO DB ────────────────────────────────────────────────────────────────

class NewsBase(DeclarativeBase):
    pass


class SeenNews(NewsBase):
    """
    Tabella per il tracciamento delle notizie già inviate.
    - url   : chiave primaria, URL univoco della notizia
    - title : titolo della notizia
    - day   : data in cui è stata pubblicata/inviata (per il digest)
    """
    __tablename__ = "seen_news"

    url   = Column(String, primary_key=True)
    title = Column(Text,   nullable=False)
    day   = Column(Date,   nullable=False, default=date.today)


async def init_news_db():
    """Crea la tabella seen_news se non esiste. Chiamato da post_init in bot.py."""
    if not memory.engine:
        logger.warning("Engine DB non disponibile — news DB non inizializzato")
        return
    async with memory.engine.begin() as conn:
        await conn.run_sync(NewsBase.metadata.create_all)
    logger.info("DB news inizializzato ✅")


# ── DEDUPLICAZIONE ────────────────────────────────────────────────────────────

async def is_seen(url: str) -> bool:
    if not memory.SessionLocal:
        return False
    async with memory.SessionLocal() as db:
        result = await db.execute(select(SeenNews).where(SeenNews.url == url))
        return result.scalar_one_or_none() is not None


async def mark_seen(url: str, title: str):
    if not memory.SessionLocal:
        return
    async with memory.SessionLocal() as db:
        existing = await db.execute(select(SeenNews).where(SeenNews.url == url))
        if not existing.scalar_one_or_none():
            db.add(SeenNews(url=url, title=title, day=date.today()))
            await db.commit()


async def get_yesterday_news() -> list[dict]:
    if not memory.SessionLocal:
        return []
    yesterday = date.today() - timedelta(days=1)
    async with memory.SessionLocal() as db:
        result = await db.execute(
            select(SeenNews).where(SeenNews.day == yesterday)
        )
        rows = result.scalars().all()
    return [{"title": r.title, "url": r.url} for r in rows]


async def cleanup_old_news(keep_days: int = 30):
    """Rimuove notizie più vecchie di N giorni — chiamato dal job settimanale."""
    if not memory.SessionLocal:
        return
    cutoff = date.today() - timedelta(days=keep_days)
    async with memory.SessionLocal() as db:
        await db.execute(delete(SeenNews).where(SeenNews.day < cutoff))
        await db.commit()


# ── SCRAPER ───────────────────────────────────────────────────────────────────

async def fetch_new_multiplayer_news(max_items: int = 30) -> list[dict]:
    """
    Scarica le ultime notizie da multiplayer.it e restituisce solo
    quelle non ancora viste (nuove dall'ultimo controllo).
    """
    try:
        async with httpx.AsyncClient(
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"},
            follow_redirects=True,
        ) as client:
            resp = await client.get(NEWS_PAGE_URL)
            resp.raise_for_status()
    except Exception as e:
        logger.error(f"Errore fetch notizie: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    seen_urls: set[str] = set()
    new_news:  list[dict] = []

    for a in soup.find_all("a", href=True):
        href: str = a["href"]
        if "/notizie/" not in href:
            continue

        url = (BASE_URL + href) if href.startswith("/") else href
        url = url.split("?")[0].rstrip("/")

        if url in seen_urls:
            continue
        seen_urls.add(url)

        if any(p in url.lower() for p in SKIP_PATTERNS):
            continue

        title = a.get_text(strip=True)
        if not title or len(title) < 15:
            continue

        if await is_seen(url):
            continue

        new_news.append({"title": title, "url": url})
        if len(new_news) >= max_items:
            break

    logger.info(f"Nuove notizie trovate: {len(new_news)}")
    return new_news


# ── AGENTE LUCA ───────────────────────────────────────────────────────────────

async def luca_comment_news(title: str, url: str) -> str:
    system = f"""{SOUL_LUCA}

Stai scrivendo un commento a caldo su una notizia videoludica per un canale Telegram.
Sei il primo che la legge e vuoi dire subito la tua.

Regole:
- Massimo 3 frasi brevi
- Non ripetere il titolo della notizia
- Parti direttamente con la tua reazione/analisi
- Sii opinionato: prendi una posizione, non fare il neutro
- Usa il formato Telegram: *grassetto* solo per parole chiave importanti
- Niente emoji tranne al massimo una alla fine se appropriata"""

    return await call_llm(
        system=system,
        messages=[{"role": "user", "content": f"Notizia: {title}"}],
        max_tokens=180,
    )


async def luca_daily_digest(news_items: list[dict]) -> str:
    if not news_items:
        return (
            "🎮 *Rassegna di Luca — buongiorno*\n\n"
            "Ieri era tutto silenzio nel settore. "
            "Neanche un comunicato stampa degno di nota. "
            "Godetevi la quiete — di solito non dura."
        )

    news_list = "\n".join(
        f"{i+1}. {item['title']}" for i, item in enumerate(news_items[:15])
    )

    system = f"""{SOUL_LUCA}

Stai scrivendo la tua rubrica quotidiana *Rassegna di Luca* per un canale Telegram.
Sono le 9:00. Hai letto tutto ieri. Ora dici la tua.

Struttura OBBLIGATORIA (prosa, non elenchi puntati):
1. Una frase d'apertura che cattura il tono della giornata di ieri nel settore
2. Le 3-5 notizie più significative, ognuna con 2-3 righe di commento critico
3. Una considerazione finale sul momento dell'industria (1-2 frasi)

Inizia sempre con: 🎮 *Rassegna di Luca —* [giorno e data]

Tono: critico, appassionato, con riferimenti storici quando pertinente.
Lunghezza: 280-380 parole. Usa *grassetto* per i titoli delle notizie commentate.
Scrivi in italiano. Prosa fluida come un editoriale."""

    yesterday_str = (date.today() - timedelta(days=1)).strftime("%A %d %B")
    return await call_llm(
        system=system,
        messages=[{
            "role": "user",
            "content": f"Data: {yesterday_str}\n\nNotizie del giorno:\n{news_list}"
        }],
        max_tokens=750,
    )


async def luca_answer_question(question: str) -> str:
    """
    Luca risponde a una domanda libera dell'utente nel topic news.
    Risponde come critico videoludico — con opinioni, contesto storico, senza filtri.
    """
    system = f"""{SOUL_LUCA}

Un utente ti ha scritto nel topic news/videogiochi del canale Telegram.
Rispondi come faresti in un editoriale: con competenza, opinioni nette e contesto.

Regole:
- Rispondi direttamente alla domanda senza preamboli
- Se la domanda riguarda un gioco, uno studio o una tendenza del settore, porta la tua prospettiva critica
- Puoi fare riferimenti storici ad altri giochi o momenti dell'industria
- Lunghezza: 3-6 frasi, mai oltre
- Formato Telegram: *grassetto* per titoli/nomi importanti
- Scrivi in italiano"""

    return await call_llm(
        system=system,
        messages=[{"role": "user", "content": question}],
        max_tokens=350,
    )


# ── FETCH RECENTI (per /news on-demand) ──────────────────────────────────────

async def fetch_recent_news(max_items: int = 20) -> list[dict]:
    """
    Scarica le ultime notizie da multiplayer.it senza filtrare per seen_news.
    Usato dal comando /news per il sunto on-demand delle ultime ore.
    """
    try:
        async with httpx.AsyncClient(
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"},
            follow_redirects=True,
        ) as client:
            resp = await client.get(NEWS_PAGE_URL)
            resp.raise_for_status()
    except Exception as e:
        logger.error(f"Errore fetch recenti: {e}")
        return []

    soup     = BeautifulSoup(resp.text, "html.parser")
    seen_urls: set[str]  = set()
    news:      list[dict] = []

    for a in soup.find_all("a", href=True):
        href: str = a["href"]
        if "/notizie/" not in href:
            continue

        url = (BASE_URL + href) if href.startswith("/") else href
        url = url.split("?")[0].rstrip("/")

        if url in seen_urls:
            continue
        seen_urls.add(url)

        if any(p in url.lower() for p in SKIP_PATTERNS):
            continue

        title = a.get_text(strip=True)
        if not title or len(title) < 15:
            continue

        news.append({"title": title, "url": url})
        if len(news) >= max_items:
            break

    logger.info(f"Notizie recenti trovate: {len(news)}")
    return news


async def luca_news_summary(news_items: list[dict], hours: int = 4) -> str:
    """
    Luca fa un sunto delle ultime N ore di notizie su multiplayer.it.
    Restituisce un messaggio con il sunto senza link individuali.
    """
    if not news_items:
        return (
            "🎮 *Sunto notizie*\n\n"
            "Niente di rilevante nelle ultime ore su Multiplayer.it. "
            "Il settore respira — o forse stanno tutti preparando qualcosa."
        )

    news_list = "\n".join(
        f"{i+1}. {item['title']}" for i, item in enumerate(news_items[:15])
    )

    system = f"""{SOUL_LUCA}

Hai appena controllato Multiplayer.it. Devi fare un sunto rapido delle ultime {hours} ore per chi te lo chiede sul canale Telegram.

Struttura OBBLIGATORIA:
1. Una riga d'apertura che dice quante notizie ci sono e il tono generale
2. Le 3-5 notizie più rilevanti con 1-2 frasi di commento ciascuna
3. Una riga finale opzionale se c'è un tema ricorrente

Inizia con: 🎮 *Ultime {hours} ore su Multiplayer.it*

Tono: diretto, critico, come faresti in una chat veloce con un collega.
Lunghezza: 150-250 parole. Scrivi in italiano. Usa *grassetto* per i titoli.
NON includere link o URL nel testo."""

    return await call_llm(
        system=system,
        messages=[{
            "role": "user",
            "content": f"Notizie da Multiplayer.it:\n{news_list}"
        }],
        max_tokens=500,
    )


# ── FORMATTAZIONE ─────────────────────────────────────────────────────────────

def format_news_message(title: str, url: str, comment: str) -> str:
    return (
        f"📰 *{title}*\n\n"
        f"{comment}\n\n"
        f"🔗 [Leggi su Multiplayer.it]({url})"
    )
