import os
import httpx


GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
GOOGLE_CSE_ID  = os.environ.get("GOOGLE_CSE_ID", "")
GOOGLE_URL     = "https://www.googleapis.com/customsearch/v1"


def web_search(query: str, max_results: int = 4) -> list[dict]:
    """
    Cerca usando Google Custom Search API.
    Restituisce una lista di dizionari con title, href, body.
    Gratuita fino a 100 ricerche/giorno.
    """
    if not GOOGLE_API_KEY or not GOOGLE_CSE_ID:
        return [{"title": "Configurazione mancante", "href": "", "body": "GOOGLE_API_KEY o GOOGLE_CSE_ID non configurati."}]

    try:
        # Chiamata sincrona — search.py viene chiamato dentro asyncio ma in thread separato
        import urllib.request
        import urllib.parse
        import json

        params = urllib.parse.urlencode({
            "key": GOOGLE_API_KEY,
            "cx":  GOOGLE_CSE_ID,
            "q":   query,
            "num": min(max_results, 10),  # Google CSE max 10 per chiamata
        })

        url = f"{GOOGLE_URL}?{params}"
        with urllib.request.urlopen(url, timeout=10) as response:
            data = json.loads(response.read())

        items = data.get("items", [])
        return [
            {
                "title": item.get("title", ""),
                "href":  item.get("link", ""),
                "body":  item.get("snippet", ""),
            }
            for item in items
        ]

    except Exception as e:
        return [{"title": "Errore ricerca", "href": "", "body": f"Ricerca Google fallita: {e}. Usa la tua conoscenza per rispondere."}]


def format_results(results: list[dict]) -> str:
    """Formatta i risultati in testo leggibile da passare all'LLM."""
    if not results:
        return "Nessun risultato trovato."
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r.get('title', '')}\n{r.get('body', '')}\nFonte: {r.get('href', '')}")
    return "\n\n".join(lines)