import os
import json
import urllib.request
import urllib.parse
import logging

logger = logging.getLogger(__name__)

SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "")
SERPER_URL     = "https://google.serper.dev/search"


def web_search(query: str, max_results: int = 4) -> list[dict]:
    """
    Cerca usando Serper (Google Search API).
    Gratuito fino a 2500 ricerche/mese, nessuna configurazione extra.
    """
    if not SERPER_API_KEY:
        logger.error("SERPER_API_KEY mancante!")
        return [{"title": "Config mancante", "href": "", "body": "SERPER_API_KEY non configurata."}]

    try:
        payload = json.dumps({"q": query, "num": max_results}).encode("utf-8")
        req = urllib.request.Request(
            SERPER_URL,
            data=payload,
            headers={
                "X-API-KEY":    SERPER_API_KEY,
                "Content-Type": "application/json",
            },
            method="POST"
        )

        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read())

        items = data.get("organic", [])
        logger.info(f"Serper: {len(items)} risultati per '{query}'")
        return [
            {
                "title": item.get("title", ""),
                "href":  item.get("link", ""),
                "body":  item.get("snippet", ""),
            }
            for item in items[:max_results]
        ]

    except Exception as e:
        logger.error(f"Errore Serper: {e}")
        return [{"title": "Errore ricerca", "href": "", "body": f"Ricerca fallita: {e}. Usa la tua conoscenza."}]


def format_results(results: list[dict]) -> str:
    """Formatta i risultati in testo leggibile da passare all'LLM."""
    if not results:
        return "Nessun risultato trovato."
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r.get('title', '')}\n{r.get('body', '')}\nFonte: {r.get('href', '')}")
    return "\n\n".join(lines)