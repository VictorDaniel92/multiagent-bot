import os
import json
import urllib.request
import urllib.parse
import logging

logger = logging.getLogger(__name__)

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
GOOGLE_CSE_ID  = os.environ.get("GOOGLE_CSE_ID", "")
GOOGLE_URL     = "https://www.googleapis.com/customsearch/v1"


def web_search(query: str, max_results: int = 4) -> list[dict]:
    if not GOOGLE_API_KEY or not GOOGLE_CSE_ID:
        logger.error("GOOGLE_API_KEY o GOOGLE_CSE_ID mancanti!")
        return [{"title": "Config mancante", "href": "", "body": "Variabili d'ambiente non configurate."}]

    # Log per debug — mostra i primi 8 caratteri delle credenziali
    logger.info(f"API KEY inizia con: {GOOGLE_API_KEY[:8]}...")
    logger.info(f"CSE ID inizia con: {GOOGLE_CSE_ID[:8]}...")

    params = urllib.parse.urlencode({
        "key": GOOGLE_API_KEY,
        "cx":  GOOGLE_CSE_ID,
        "q":   query,
        "num": min(max_results, 10),
    })

    url = f"{GOOGLE_URL}?{params}"
    logger.info(f"Chiamata Google: {GOOGLE_URL}?q={query}&cx={GOOGLE_CSE_ID[:8]}...")

    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            data = json.loads(response.read())

        items = data.get("items", [])
        logger.info(f"Risultati trovati: {len(items)}")
        return [
            {"title": item.get("title", ""), "href": item.get("link", ""), "body": item.get("snippet", "")}
            for item in items
        ]

    except urllib.error.HTTPError as e:
        # Legge il body dell'errore per capire cosa dice Google
        error_body = e.read().decode("utf-8")
        logger.error(f"Google HTTP {e.code}: {error_body}")
        return [{"title": "Errore ricerca", "href": "", "body": f"Google ha risposto con errore {e.code}. Usa la tua conoscenza."}]

    except Exception as e:
        logger.error(f"Errore generico ricerca: {e}")
        return [{"title": "Errore ricerca", "href": "", "body": f"Ricerca fallita: {e}. Usa la tua conoscenza."}]


def format_results(results: list[dict]) -> str:
    if not results:
        return "Nessun risultato trovato."
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r.get('title', '')}\n{r.get('body', '')}\nFonte: {r.get('href', '')}")
    return "\n\n".join(lines)