"""
game_enricher.py — Arricchisce le risposte di Luca con dati esterni sui giochi.

Fonti:
- Metacritic      : voto critica e utenti
- Multiplayer.it  : voto dalla recensione (se esiste)
- HowLongToBeat   : durata storia / completismo
- PSNProfiles     : difficoltà platino e tasso di completamento
"""

import re
import logging
import asyncio
from dataclasses import dataclass, field

import httpx
from bs4 import BeautifulSoup

from agents import call_llm

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
}

TIMEOUT = 12


# ── DATA MODEL ────────────────────────────────────────────────────────────────

@dataclass
class GameData:
    name:            str  = ""
    metacritic_score: str = ""   # "92 / 100"  o ""
    metacritic_user:  str = ""   # "8.4"       o ""
    multiplayer_vote: str = ""   # "9.2"       o ""
    multiplayer_url:  str = ""
    multiplayer_review_text: str = ""  # testo recensione completo
    hltb_main:        str = ""   # "30 ore"    o ""
    hltb_complete:    str = ""   # "80 ore"    o ""
    psn_difficulty:   str = ""   # "4/10"      o ""
    psn_completion:   str = ""   # "3.5%"      o ""
    psn_url:          str = ""
    errors:    list[str] = field(default_factory=list)

    def has_data(self) -> bool:
        return any([
            self.metacritic_score,
            self.multiplayer_vote,
            self.hltb_main,
            self.psn_difficulty,
        ])

    def format_for_telegram(self) -> str:
        """Blocco dati formattato per Telegram, da appendere alla risposta di Luca."""
        if not self.has_data():
            return ""

        lines = [f"\n{'─' * 28}", f"📋 *{self.name}*\n"]

        if self.metacritic_score or self.metacritic_user:
            mc = self.metacritic_score or "n/d"
            usr = f" | utenti: {self.metacritic_user}" if self.metacritic_user else ""
            lines.append(f"🟡 *Metacritic:* {mc}{usr}")

        if self.multiplayer_vote:
            suffix = f" — [leggi]({self.multiplayer_url})" if self.multiplayer_url else ""
            lines.append(f"🎮 *Multiplayer.it:* {self.multiplayer_vote}/10{suffix}")

        if self.hltb_main or self.hltb_complete:
            storia   = self.hltb_main     or "n/d"
            completo = self.hltb_complete or "n/d"
            lines.append(f"⏱ *HowLongToBeat:* storia {storia} | 100% {completo}")

        if self.psn_difficulty or self.psn_completion:
            diff = self.psn_difficulty or "n/d"
            comp = f" | completato {self.psn_completion}" if self.psn_completion else ""
            url  = f" — [profilo]({self.psn_url})" if self.psn_url else ""
            lines.append(f"🏆 *Platino:* difficoltà {diff}{comp}{url}")

        return "\n".join(lines)


# ── UTILS ─────────────────────────────────────────────────────────────────────

async def _get(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response | None:
    try:
        r = await client.get(url, headers=HEADERS, timeout=TIMEOUT, **kwargs)
        r.raise_for_status()
        return r
    except Exception as e:
        logger.debug(f"GET {url} → {e}")
        return None


def _slug(name: str) -> str:
    """Converte nome gioco in slug URL: 'Elden Ring' → 'elden-ring'"""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


# ── STEP 0: RILEVA IL NOME DEL GIOCO DALLA DOMANDA ───────────────────────────

async def detect_game_name(question: str) -> str | None:
    """
    Usa il LLM per capire se la domanda riguarda un gioco specifico
    e restituisce il nome canonico in inglese (per le ricerche).
    Ritorna None se non c'è un titolo specifico.
    """
    raw = await call_llm(
        system=(
            "Sei un estrattore di nomi di videogiochi. "
            "Dalla domanda dell'utente estrai il titolo ESATTO del gioco, "
            "in inglese se esiste una versione inglese del titolo. "
            "Rispondi SOLO con il titolo del gioco, nient'altro. "
            "Se non c'è un titolo specifico, rispondi esattamente: NONE"
        ),
        messages=[{"role": "user", "content": question}],
        max_tokens=30,
    )
    name = raw.strip().strip('"').strip("'")
    if name.upper() == "NONE" or not name:
        return None
    return name


# ── SCRAPER 1: METACRITIC ─────────────────────────────────────────────────────

async def fetch_metacritic(client: httpx.AsyncClient, game_name: str, data: GameData):
    """
    Cerca su Metacritic e recupera metascore + user score.
    URL di ricerca: https://www.metacritic.com/search/{query}/
    """
    query = game_name.replace(" ", "+")
    url   = f"https://www.metacritic.com/search/{query}/"

    resp = await _get(client, url)
    if not resp:
        data.errors.append("metacritic: timeout/errore")
        return

    soup = BeautifulSoup(resp.text, "html.parser")

    # I risultati di ricerca Metacritic hanno data-testid="search-result-item"
    # Il primo risultato di tipo "game" è il più rilevante
    for item in soup.find_all(attrs={"data-testid": "search-result-item"})[:5]:
        category = item.get("data-testid", "")
        text      = item.get_text(" ", strip=True).lower()

        # Verifica che sia un gioco e non film/serie
        if "game" not in category and "games" not in text:
            continue

        # Metascore
        score_el = item.find(attrs={"data-testid": "score"}) \
                or item.find(class_=re.compile(r"metascore|c-siteReviewScore"))
        if score_el:
            score_text = score_el.get_text(strip=True)
            if score_text.isdigit():
                data.metacritic_score = f"{score_text}/100"

        # User score (spesso in un secondo elemento con "user")
        user_el = item.find(class_=re.compile(r"user.?score|userScore"))
        if user_el:
            data.metacritic_user = user_el.get_text(strip=True)

        if data.metacritic_score:
            logger.debug(f"Metacritic: {game_name} → {data.metacritic_score}")
            return

    # Fallback: cerca direttamente nella pagina del gioco
    slug = _slug(game_name)
    direct_url = f"https://www.metacritic.com/game/{slug}/"
    resp2 = await _get(client, direct_url)
    if not resp2:
        return

    soup2 = BeautifulSoup(resp2.text, "html.parser")

    # Metascore
    meta_el = soup2.find(attrs={"data-testid": "score-details-metascore-score"}) \
           or soup2.find(class_=re.compile(r"c-siteReviewScore.*xlarger"))
    if meta_el:
        t = meta_el.get_text(strip=True)
        if t.isdigit() or re.match(r"\d+", t):
            data.metacritic_score = f"{re.match(r'[0-9]+', t).group()}/100"

    # User score
    user_el = soup2.find(attrs={"data-testid": "score-details-userscore-score"})
    if user_el:
        data.metacritic_user = user_el.get_text(strip=True)

    logger.debug(f"Metacritic fallback: {game_name} → {data.metacritic_score}")


# ── SCRAPER 2: MULTIPLAYER.IT ─────────────────────────────────────────────────

async def fetch_multiplayer(client: httpx.AsyncClient, game_name: str, data: GameData):
    """
    Cerca la recensione su Multiplayer.it.
    URL: https://multiplayer.it/cerca/?q={query}
    """
    query = game_name.replace(" ", "+")
    url   = f"https://multiplayer.it/cerca/?q={query}"

    resp = await _get(client, url)
    if not resp:
        data.errors.append("multiplayer.it: timeout/errore")
        return

    soup = BeautifulSoup(resp.text, "html.parser")

    # Cerca link che contengono "/recensioni/" nei risultati
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/recensioni/" not in href:
            continue

        review_url = ("https://multiplayer.it" + href) if href.startswith("/") else href

        # Apri la pagina della recensione per trovare il voto
        resp2 = await _get(client, review_url)
        if not resp2:
            continue

        soup2 = BeautifulSoup(resp2.text, "html.parser")

        # Il voto è spesso in un elemento con class "score", "voto", o schema.org ratingValue
        vote_el = (
            soup2.find(attrs={"itemprop": "ratingValue"})
            or soup2.find(class_=re.compile(r"\bvoto\b|\bscore\b|\brating\b", re.I))
        )

        if vote_el:
            vote_text = vote_el.get_text(strip=True).replace(",", ".")
            m = re.search(r"\b(\d+(?:\.\d+)?)\b", vote_text)
            if m:
                v = float(m.group(1))
                # Normalizza su 10 se è su 100
                if v > 10:
                    v = v / 10
                data.multiplayer_vote = str(round(v, 1))
                data.multiplayer_url  = review_url
                logger.debug(f"Multiplayer.it: {game_name} → {data.multiplayer_vote}")
                return

    logger.debug(f"Multiplayer.it: nessuna recensione trovata per '{game_name}'")


# ── SCRAPER 3: HOWLONGTOBEAT ─────────────────────────────────────────────────

async def fetch_hltb(client: httpx.AsyncClient, game_name: str, data: GameData):
    """
    Recupera i tempi di completamento da HowLongToBeat.
    Usa l'API di ricerca POST non ufficiale.
    """
    # HLTB richiede un referrer corretto e header specifici
    headers = {
        **HEADERS,
        "Referer":      "https://howlongtobeat.com/",
        "Origin":       "https://howlongtobeat.com",
        "Content-Type": "application/json",
    }

    payload = {
        "searchType":  "games",
        "searchTerms": game_name.split(),
        "searchPage":  1,
        "size":        5,
        "searchOptions": {
            "games": {
                "userId":     0,
                "platform":   "",
                "sortCategory": "popular",
                "rangeCategory": "main",
                "rangeTime":  {"min": None, "max": None},
                "gameplay":   {"perspective": "", "flow": "", "genre": ""},
                "rangeYear":  {"min": "", "max": ""},
                "modifier":   "",
            },
            "users":      {"sortCategory": "postcount"},
            "lists":      {"sortCategory": "follows"},
            "filter":     "",
            "sort":       0,
            "randomizer": 0,
        },
    }

    try:
        resp = await client.post(
            "https://howlongtobeat.com/api/search",
            json=payload,
            headers=headers,
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        results = resp.json().get("data", [])
    except Exception as e:
        logger.debug(f"HLTB API: {e}")
        data.errors.append("hltb: errore API")
        return

    if not results:
        logger.debug(f"HLTB: nessun risultato per '{game_name}'")
        return

    game = results[0]

    def _fmt_hours(seconds) -> str:
        if not seconds:
            return ""
        h = round(seconds / 3600)
        return f"{h} ore" if h != 1 else "1 ora"

    data.hltb_main     = _fmt_hours(game.get("comp_main"))
    data.hltb_complete = _fmt_hours(game.get("comp_100"))

    logger.debug(f"HLTB: {game_name} → storia {data.hltb_main}, 100% {data.hltb_complete}")


# ── SCRAPER 4: PSNPROFILES ────────────────────────────────────────────────────

async def fetch_psnprofiles(client: httpx.AsyncClient, game_name: str, data: GameData):
    """
    Recupera difficoltà platino e tasso di completamento da PSNProfiles.
    URL: https://psnprofiles.com/trophies?q={query}
    """
    query = game_name.replace(" ", "+")
    url   = f"https://psnprofiles.com/trophies?q={query}"

    resp = await _get(client, url)
    if not resp:
        data.errors.append("psnprofiles: timeout/errore")
        return

    soup = BeautifulSoup(resp.text, "html.parser")

    # Primo risultato della lista trofei
    first = soup.find("li", class_=re.compile(r"game-item|title-item")) \
          or soup.select_one("#games-list li, table.zebra tr:not(:first-child)")

    if not first:
        # Prova link diretto alla pagina del gioco
        game_link = soup.find("a", href=re.compile(r"/trophies/\d+"))
        if not game_link:
            logger.debug(f"PSNProfiles: nessun risultato per '{game_name}'")
            return
        game_url = "https://psnprofiles.com" + game_link["href"]
    else:
        link = first.find("a", href=re.compile(r"/trophies/"))
        if not link:
            return
        game_url = "https://psnprofiles.com" + link["href"]

    # Apri la pagina del gioco
    resp2 = await _get(client, game_url)
    if not resp2:
        return

    soup2 = BeautifulSoup(resp2.text, "html.parser")

    # Difficoltà platino (spesso in un tag con "difficulty" o "Platinum Difficulty")
    diff_el = soup2.find(class_=re.compile(r"difficulty", re.I)) \
           or soup2.find(string=re.compile(r"Platinum Difficulty", re.I))
    if diff_el:
        parent = diff_el.parent if isinstance(diff_el, str) else diff_el
        m = re.search(r"(\d+(?:\.\d+)?)\s*/\s*10", parent.get_text())
        if m:
            data.psn_difficulty = f"{m.group(1)}/10"

    # Tasso di completamento (% di utenti che hanno il platino)
    for el in soup2.find_all(string=re.compile(r"Platinum|completat", re.I)):
        parent = el.parent
        if parent:
            m = re.search(r"(\d+(?:\.\d+)?)\s*%", parent.get_text())
            if m:
                data.psn_completion = f"{m.group(1)}%"
                break

    data.psn_url = game_url
    logger.debug(f"PSNProfiles: {game_name} → {data.psn_difficulty}, {data.psn_completion}")


# ── SCRAPER 5: TESTO RECENSIONE MULTIPLAYER.IT ───────────────────────────────

async def fetch_multiplayer_review_text(client: httpx.AsyncClient, game_name: str, data: GameData) -> str:
    """
    Recupera il testo completo della recensione di Multiplayer.it per un gioco.
    Ritorna il testo estratto (max ~2000 chars) o stringa vuota se non trovato.
    """
    query = game_name.replace(" ", "+")
    url   = f"https://multiplayer.it/cerca/?q={query}"

    resp = await _get(client, url)
    if not resp:
        return ""

    soup = BeautifulSoup(resp.text, "html.parser")

    # Trova il link alla recensione
    review_url = None
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/recensioni/" in href:
            review_url = ("https://multiplayer.it" + href) if href.startswith("/") else href
            break

    if not review_url:
        logger.debug(f"Multiplayer.it: nessuna recensione trovata per '{game_name}'")
        return ""

    resp2 = await _get(client, review_url)
    if not resp2:
        return ""

    soup2 = BeautifulSoup(resp2.text, "html.parser")

    # Estrae il corpo della recensione — cerca i tag più probabili
    body = (
        soup2.find("div", class_=re.compile(r"article.?body|review.?body|content.?text|article.?content", re.I))
        or soup2.find("article")
        or soup2.find("div", class_=re.compile(r"text|body|content", re.I))
    )

    if not body:
        return ""

    # Rimuove script, style, nav
    for tag in body.find_all(["script", "style", "nav", "aside", "figure"]):
        tag.decompose()

    text = body.get_text(" ", strip=True)

    # Pulisce spazi multipli
    text = re.sub(r"\s+", " ", text).strip()

    # Limita a 2500 caratteri per non esplodere il contesto
    if len(text) > 2500:
        text = text[:2500] + "..."

    logger.debug(f"Multiplayer.it review text: {game_name} → {len(text)} chars")
    return text


# ── ENTRY POINT PRINCIPALE ────────────────────────────────────────────────────

async def enrich_game_data(game_name: str) -> GameData:
    """
    Recupera tutti i dati disponibili per un gioco in parallelo.
    Include voti, tempi di completamento, difficoltà platino e testo recensione.
    """
    data = GameData(name=game_name)

    async with httpx.AsyncClient(
        headers=HEADERS,
        follow_redirects=True,
        timeout=TIMEOUT,
    ) as client:
        review_text_task = fetch_multiplayer_review_text(client, game_name, data)

        results = await asyncio.gather(
            fetch_metacritic(client, game_name, data),
            fetch_multiplayer(client, game_name, data),
            fetch_hltb(client, game_name, data),
            fetch_psnprofiles(client, game_name, data),
            review_text_task,
            return_exceptions=True,
        )

        # Il 5° risultato è il testo della recensione
        if isinstance(results[4], str) and results[4]:
            data.multiplayer_review_text = results[4]

    return data
