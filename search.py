import time
import random
from duckduckgo_search import DDGS


def web_search(query: str, max_results: int = 4) -> list[dict]:
    """
    Cerca su DuckDuckGo con retry automatico e delay per evitare il rate limit.
    """
    # Delay casuale tra 1 e 3 secondi — riduce il throttling da IP cloud
    time.sleep(random.uniform(1, 3))

    for attempt in range(3):
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=max_results))
            if results:
                return results
        except Exception:
            if attempt < 2:
                time.sleep(3 + attempt * 2)  # aspetta di più ad ogni retry

    # Fallback: dice all'LLM di usare la sua conoscenza
    return [{"title": "Ricerca non disponibile", "href": "", "body": f"Non è stato possibile cercare '{query}'. Usa la tua conoscenza per rispondere."}]


def format_results(results: list[dict]) -> str:
    """Formatta i risultati in testo leggibile da passare all'LLM."""
    if not results:
        return "Nessun risultato trovato."
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r.get('title', '')}\n{r.get('body', '')}\nFonte: {r.get('href', '')}")
    return "\n\n".join(lines)