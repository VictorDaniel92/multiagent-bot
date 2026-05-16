from duckduckgo_search import DDGS

def web_search(query: str, max_results: int = 4) -> list[dict]:
    """
    Cerca su DuckDuckGo e restituisce una lista di risultati.
    Ogni risultato ha: title, href, body.
    Gratuito, nessuna API key necessaria.
    """
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        return results
    except Exception as e:
        return [{"title": "Errore", "href": "", "body": f"Ricerca fallita: {e}"}]


def format_results(results: list[dict]) -> str:
    """Formatta i risultati in testo leggibile da passare all'LLM."""
    if not results:
        return "Nessun risultato trovato."
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r.get('title', '')}\n{r.get('body', '')}\nFonte: {r.get('href', '')}")
    return "\n\n".join(lines)
