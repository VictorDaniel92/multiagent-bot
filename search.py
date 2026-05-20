import os
import json
import asyncio
import logging
import httpx

logger = logging.getLogger(__name__)

SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "")
SERPER_URL     = "https://google.serper.dev/search"

# Client persistente per Serper
_serper_client: httpx.AsyncClient | None = None

async def _get_serper_client() -> httpx.AsyncClient:
    global _serper_client
    if _serper_client is None or _serper_client.is_closed:
        _serper_client = httpx.AsyncClient(timeout=10)
    return _serper_client


async def async_web_search(query: str, max_results: int = 4) -> list[dict]:
    """Versione asincrona di web_search — usa httpx direttamente."""
    if not SERPER_API_KEY:
        logger.error("SERPER_API_KEY mancante!")
        return [{"title": "Config mancante", "href": "", "body": "SERPER_API_KEY non configurata."}]

    try:
        client = await _get_serper_client()
        response = await client.post(
            SERPER_URL,
            headers={
                "X-API-KEY":    SERPER_API_KEY,
                "Content-Type": "application/json",
            },
            json={"q": query, "num": max_results},
        )
        data  = response.json()
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


def web_search(query: str, max_results: int = 4) -> list[dict]:
    """
    Versione sincrona mantenuta per compatibilità con sofia_synthesize e max_plan
    che girano dentro asyncio ma chiamano web_search in modo sincrono.
    Internamente usa httpx sincrono — più efficiente di urllib.
    """
    if not SERPER_API_KEY:
        logger.error("SERPER_API_KEY mancante!")
        return [{"title": "Config mancante", "href": "", "body": "SERPER_API_KEY non configurata."}]

    try:
        with httpx.Client(timeout=10) as client:
            response = client.post(
                SERPER_URL,
                headers={
                    "X-API-KEY":    SERPER_API_KEY,
                    "Content-Type": "application/json",
                },
                json={"q": query, "num": max_results},
            )
        data  = response.json()
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
