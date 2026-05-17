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
    # PSN — dati base dalla lista trofei
    psn_difficulty:   str = ""   # "4/10"      o ""
    psn_completion:   str = ""   # "3.5%"      o ""
    psn_url:          str = ""
    # PSN — dati dalla guida
    psn_guide_url:        str  = ""
    psn_time_to_plat:     str  = ""   # "40-50 ore"
    psn_playthroughs:     str  = ""   # "2"
    psn_missable:         str  = ""   # "Sì (3)" / "No"
    psn_guide_difficulty: str  = ""   # "4/10"  (dalla guida, più preciso)
    errors:    list[str] = field(default_factory=list)

    def has_data(self) -> bool:
        return any([
            self.metacritic_score,
            self.multiplayer_vote,
            self.hltb_main,
            self.psn_difficulty,
            self.psn_guide_url,
        ])

    def format_for_telegram(self) -> str:
        """Blocco dati formattato per Telegram, da appendere alla risposta di Luca."""
        if not self.has_data():
            return ""

        lines = [f"\n{'─' * 28}", f"📋 *{self.name}*\n"]

        if self.metacritic_score or self.metacritic_user:
            mc  = self.metacritic_score or "n/d"
            usr = f" | utenti: {self.metacritic_user}" if self.metacritic_user else ""
            lines.append(f"🟡 *Metacritic:* {mc}{usr}")

        if self.multiplayer_vote:
            suffix = f" — [leggi]({self.multiplayer_url})" if self.multiplayer_url else ""
            lines.append(f"🎮 *Multiplayer.it:* {self.multiplayer_vote}/10{suffix}")

        if self.hltb_main or self.hltb_complete:
            storia   = self.hltb_main     or "n/d"
            completo = self.hltb_complete or "n/d"
            lines.append(f"⏱ *HowLongToBeat:* storia {storia} | 100% {completo}")

        # Sezione platino — preferisce dati guida se disponibili
        has_plat = any([
            self.psn_guide_url, self.psn_difficulty, self.psn_guide_difficulty,
            self.psn_time_to_plat, self.psn_playthroughs, self.psn_missable,
        ])
        if has_plat:
            lines.append("")
            guide_link = f"[📖 Guida Platino]({self.psn_guide_url})" if self.psn_guide_url else "🏆 *Platino*"
            lines.append(guide_link)

            diff = self.psn_guide_difficulty or self.psn_difficulty
            if diff:
                lines.append(f"  • Difficoltà: *{diff}*")
            if self.psn_time_to_plat:
                lines.append(f"  • Tempo stimato: *{self.psn_time_to_plat}*")
            if self.psn_playthroughs:
                lines.append(f"  • Playthrough necessari: *{self.psn_playthroughs}*")
            if self.psn_missable:
                lines.append(f"  • Trofei missabili: *{self.psn_missable}*")
            if self.psn_completion:
                lines.append(f"  • Platinato da: {self.psn_completion} dei giocatori")

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
    Step 1: trova la lista trofei su PSNProfiles e recupera completion rate.
    Step 2: cerca la guida al platino e ne estrae i dettagli.
    """
    query = game_name.replace(" ", "+")
    url   = f"https://psnprofiles.com/trophies?q={query}"

    resp = await _get(client, url)
    if not resp:
        data.errors.append("psnprofiles: timeout/errore")
        return

    soup = BeautifulSoup(resp.text, "html.parser")

    # ── Trova URL lista trofei ─────────────────────────────────────────────
    game_url = None
    first = soup.find("li", class_=re.compile(r"game-item|title-item")) \
          or soup.select_one("#games-list li, table.zebra tr:not(:first-child)")

    if first:
        link = first.find("a", href=re.compile(r"/trophies/"))
        if link:
            game_url = "https://psnprofiles.com" + link["href"]
    else:
        game_link = soup.find("a", href=re.compile(r"/trophies/\d+"))
        if game_link:
            game_url = "https://psnprofiles.com" + game_link["href"]

    if game_url:
        data.psn_url = game_url
        # Recupera completion rate dalla pagina trofei
        resp2 = await _get(client, game_url)
        if resp2:
            soup2 = BeautifulSoup(resp2.text, "html.parser")
            for el in soup2.find_all(string=re.compile(r"Platinum|completat", re.I)):
                parent = el.parent
                if parent:
                    m = re.search(r"(\d+(?:\.\d+)?)\s*%", parent.get_text())
                    if m:
                        data.psn_completion = f"{m.group(1)}%"
                        break

    # ── Step 2: cerca la guida al platino ─────────────────────────────────
    await _fetch_psn_guide(client, game_name, data)

    logger.debug(f"PSNProfiles: {game_name} → diff={data.psn_guide_difficulty}, "
                 f"time={data.psn_time_to_plat}, missable={data.psn_missable}")


async def _fetch_psn_guide(client: httpx.AsyncClient, game_name: str, data: GameData):
    """
    Cerca e scrapa la guida al platino su PSNProfiles.
    Le guide hanno URL tipo: https://psnprofiles.com/guide/XXXX-game-name
    e contengono una tabella con: difficulty, time, playthroughs, missable trophies.
    """
    # Cerca la guida
    query    = game_name.replace(" ", "+")
    guide_search = f"https://psnprofiles.com/guides?q={query}"

    resp = await _get(client, guide_search)
    if not resp:
        return

    soup = BeautifulSoup(resp.text, "html.parser")

    # Trova il primo link a una guida
    guide_url = None
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/guide/" in href and href != "/guides":
            guide_url = ("https://psnprofiles.com" + href) if href.startswith("/") else href
            break

    if not guide_url:
        logger.debug(f"PSNProfiles: nessuna guida trovata per '{game_name}'")
        return

    data.psn_guide_url = guide_url

    # Scrapa la guida
    resp2 = await _get(client, guide_url)
    if not resp2:
        return

    soup2 = BeautifulSoup(resp2.text, "html.parser")

    # Le guide PSNProfiles hanno una tabella "overview" con righe tipo:
    # "Estimated trophy difficulty" | "4/10"
    # "Approximate amount of time"  | "40-50 hours"
    # "Minimum number of playthroughs" | "2"
    # "Number of missable trophies" | "3" oppure "None"
    # "Online trophies"             | "0"

    # Mappa campi → attributo GameData
    field_map = {
        r"difficulty":      "psn_guide_difficulty",
        r"time.*plat|hours.*plat|approximate.*time": "psn_time_to_plat",
        r"playthrough":     "psn_playthroughs",
        r"missable":        "psn_missable",
    }

    # Cerca nella tabella overview
    for row in soup2.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        label = cells[0].get_text(" ", strip=True).lower()
        value = cells[1].get_text(" ", strip=True)

        for pattern, attr in field_map.items():
            if re.search(pattern, label, re.I) and value and not getattr(data, attr):
                # Normalizza tempo in italiano
                if attr == "psn_time_to_plat":
                    value = _normalize_time(value)
                # Normalizza missable
                if attr == "psn_missable":
                    value = _normalize_missable(value)
                setattr(data, attr, value)

    # Fallback: cerca come lista di definizioni <dt>/<dd>
    if not data.psn_guide_difficulty:
        for dt in soup2.find_all("dt"):
            label = dt.get_text(strip=True).lower()
            dd    = dt.find_next_sibling("dd")
            if not dd:
                continue
            value = dd.get_text(strip=True)
            for pattern, attr in field_map.items():
                if re.search(pattern, label, re.I) and value and not getattr(data, attr):
                    if attr == "psn_time_to_plat":
                        value = _normalize_time(value)
                    if attr == "psn_missable":
                        value = _normalize_missable(value)
                    setattr(data, attr, value)

    logger.debug(f"PSN Guide: {game_name} → {data.psn_guide_url} | "
                 f"diff={data.psn_guide_difficulty} time={data.psn_time_to_plat} "
                 f"miss={data.psn_missable} play={data.psn_playthroughs}")


def _normalize_time(value: str) -> str:
    """Converte '40-50 hours' → '40-50 ore'."""
    value = re.sub(r"\bhours?\b", "ore", value, flags=re.I)
    value = re.sub(r"\bminutes?\b", "min", value, flags=re.I)
    return value.strip()


def _normalize_missable(value: str) -> str:
    """Converte 'None' → 'No', numeri → 'Sì (N)'."""
    v = value.strip()
    if v.lower() in ("none", "0", "no"):
        return "No"
    if v.isdigit() and int(v) > 0:
        return f"Sì ({v})"
    return v


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
