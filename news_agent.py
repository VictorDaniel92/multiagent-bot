import re
import asyncio
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

def _parse_minutes_ago(text: str) -> int | None:
    """
    Converte timestamp relativi italiani in minuti.
    Es: "13 minuti fa" → 13, "un'ora fa" → 60, "2 ore fa" → 120
    Ritorna None se non riconosce il formato.
    """
    text = text.lower().strip()
    import re
    if m := re.match(r"(\d+)\s+minut", text):
        return int(m.group(1))
    if "un'ora" in text or "un ora" in text:
        return 60
    if m := re.match(r"(\d+)\s+or", text):
        return int(m.group(1)) * 60
    if "ieri" in text:
        return 60 * 24
    return None


async def _fetch_homepage_html() -> str | None:
    """Scarica la homepage di multiplayer.it e restituisce l'HTML."""
    try:
        async with httpx.AsyncClient(
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"},
            follow_redirects=True,
        ) as client:
            resp = await client.get("https://multiplayer.it/")
            resp.raise_for_status()
            return resp.text
    except Exception as e:
        logger.error(f"Errore fetch homepage: {e}")
        return None


async def fetch_recent_news(hours: int = 4) -> list[dict]:
    """
    Scarica la homepage e restituisce le notizie pubblicate
    nelle ultime `hours` ore, con timestamp relativo.
    Non filtra per "già viste" — serve per il sunto manuale.
    """
    html = await _fetch_homepage_html()
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    max_minutes = hours * 60
    results: list[dict] = []
    seen_urls: set[str] = set()

    # La homepage mostra gli articoli come blocchi con timestamp vicino al link
    # Cerchiamo tutti i tag che contengono sia un link /notizie/ che un testo temporale
    for container in soup.find_all(True):
        text = container.get_text(" ", strip=True)

        # Cerca timestamp nel testo del contenitore
        import re
        time_match = re.search(
            r"(\d+\s+minut[io]?\s+fa|un['']\s*ora\s+fa|\d+\s+ore?\s+fa|ieri)",
            text, re.IGNORECASE
        )
        if not time_match:
            continue

        minutes_ago = _parse_minutes_ago(time_match.group(0))
        if minutes_ago is None or minutes_ago > max_minutes:
            continue

        # Trova il link notizia dentro questo contenitore
        for a in container.find_all("a", href=True):
            href: str = a["href"]
            if "/notizie/" not in href:
                continue

            url = (BASE_URL + href) if href.startswith("/") else href
            url = url.split("?")[0].rstrip("/")

            if url in seen_urls:
                continue
            if any(p in url.lower() for p in SKIP_PATTERNS):
                continue

            title = a.get_text(strip=True)
            if not title or len(title) < 15:
                continue

            seen_urls.add(url)
            results.append({
                "title":       title,
                "url":         url,
                "minutes_ago": minutes_ago,
            })

    # Ordina dalla più recente
    results.sort(key=lambda x: x["minutes_ago"])
    logger.info(f"Notizie ultime {hours}h trovate: {len(results)}")
    return results

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


async def luca_summarize_recent(news_items: list[dict], hours: int = 4) -> str:
    """
    Luca fa un sunto delle notizie delle ultime N ore su richiesta esplicita.
    """
    if not news_items:
        return (
            f"🎮 Nelle ultime {hours} ore da *Multiplayer.it* non è uscito nulla "
            f"di rilevante. O è una giornata tranquilla, o il settore sta trattenendo "
            f"il respiro prima di qualcosa di grosso."
        )

    news_list = "\n".join(
        f"- {item['title']} ({item['minutes_ago']} min fa)"
        for item in news_items[:12]
    )

    system = f"""{SOUL_LUCA}

Un utente ti ha chiesto un sunto delle ultime {hours} ore di notizie videoludiche da Multiplayer.it.
Rispondi come faresti in un rapido briefing redazionale: cosa è successo, cosa conta davvero.

Struttura (prosa, non elenchi):
1. Una frase che inquadra il tono delle ultime ore
2. Le notizie più significative con il tuo commento critico (2-3 righe ciascuna)
3. Eventuale collegamento tra le notizie se c'è un filo comune

Inizia con: 🎮 *Sunto ultime {hours} ore —*

Lunghezza: 200-300 parole. Usa *grassetto* per i titoli dei giochi/notizie principali.
Scrivi in italiano. Sii diretto — niente introduzioni inutili."""

    return await call_llm(
        system=system,
        messages=[{"role": "user", "content": f"Notizie:\n{news_list}"}],
        max_tokens=500,
    )


# ── FORMATTAZIONE ─────────────────────────────────────────────────────────────
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


# ── FORMATTAZIONE ─────────────────────────────────────────────────────────────

async def luca_answer_question(question: str, profile_context: str = "") -> str:
    """
    Luca risponde a una domanda libera nel topic news.
    Se la domanda riguarda un gioco specifico, arricchisce la risposta
    con dati da Metacritic, Multiplayer.it, HowLongToBeat e PSNProfiles.
    Se la domanda riguarda il platino, mostra la guida dettagliata.
    """
    from game_enricher import detect_game_name, enrich_game_data

    # Rileva se è una domanda sul platino
    is_platinum_question = bool(re.search(
        r'\b(platino|platinum|trofei|trophy|trophies|missabili?|playthrough|completare al 100)\b',
        question, re.I
    ))

    game_name, _ = await asyncio.gather(
        detect_game_name(question),
        asyncio.sleep(0),
    )

    review_context = ""
    stats_block    = ""

    if game_name:
        logger.info(f"Luca: arricchimento dati per '{game_name}' (platinum={is_platinum_question})")
        game_data = await enrich_game_data(game_name)
        stats_block = game_data.format_for_telegram()

        if game_data.multiplayer_review_text:
            review_context = (
                f"\n\n## Recensione di Multiplayer.it per {game_name}:\n"
                f"{game_data.multiplayer_review_text}\n\n"
                f"Usa questa recensione come base per la tua risposta — "
                f"puoi citare aspetti specifici, concordare o dissentire con giudizio critico."
            )

        # Risposta dedicata al platino
        if is_platinum_question and game_data.psn_guide_url:
            return await _luca_platinum_answer(question, game_name, game_data, profile_context)

    system = f"""{SOUL_LUCA}

{profile_context}

Un utente ti ha scritto nel topic news/videogiochi del canale Telegram.
Rispondi come faresti in un editoriale: con competenza, opinioni nette e contesto.

Regole:
- Rispondi direttamente alla domanda senza preamboli
- Se hai la recensione di Multiplayer.it, usala come base ma esprimi la TUA voce critica
- Puoi fare riferimenti storici ad altri giochi o momenti dell'industria
- Lunghezza: 3-6 frasi, mai oltre
- Formato Telegram: *grassetto* per titoli/nomi importanti
- Scrivi in italiano{review_context}"""

    answer = await call_llm(
        system=system,
        messages=[{"role": "user", "content": question}],
        max_tokens=350,
    )

    return answer + (stats_block if stats_block else "")


async def _luca_platinum_answer(question: str, game_name: str, game_data, profile_context: str) -> str:
    """Risposta dedicata alle domande sul platino, con dati dalla guida PSNProfiles."""

    # Costruisce contesto guida
    guide_lines = []
    if game_data.psn_guide_difficulty:
        guide_lines.append(f"Difficoltà: {game_data.psn_guide_difficulty}")
    if game_data.psn_time_to_plat:
        guide_lines.append(f"Tempo stimato: {game_data.psn_time_to_plat}")
    if game_data.psn_playthroughs:
        guide_lines.append(f"Playthrough necessari: {game_data.psn_playthroughs}")
    if game_data.psn_missable:
        guide_lines.append(f"Trofei missabili: {game_data.psn_missable}")
    if game_data.psn_completion:
        guide_lines.append(f"Platinato da: {game_data.psn_completion} dei giocatori")

    guide_context = "\n".join(guide_lines) if guide_lines else "Dati non disponibili"

    system = f"""{SOUL_LUCA}

{profile_context}

Stai rispondendo a una domanda sul platino di un gioco.
Hai i dati della guida ufficiale su PSNProfiles — usali per dare una risposta precisa e utile.
Aggiungi il tuo commento personale: vale la pena farlo? È frustrante o godibile?
Max 4 frasi + i dati. Scrivi in italiano. Formato Telegram."""

    answer = await call_llm(
        system=system,
        messages=[{"role": "user", "content":
            f"Domanda: {question}\n\nDati guida per {game_name}:\n{guide_context}"
        }],
        max_tokens=300,
    )

    # Appende sempre il blocco dati completo con link alla guida
    stats_block = game_data.format_for_telegram()
    return answer + (stats_block if stats_block else "")


def format_news_message(title: str, url: str, comment: str) -> str:
    return (
        f"📰 *{title}*\n\n"
        f"{comment}\n\n"
        f"🔗 [Leggi su Multiplayer.it]({url})"
    )
