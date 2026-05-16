import os
import httpx
from search import web_search, format_results

GROQ_API_KEY = os.environ["GROQ_API_KEY"]
GROQ_MODEL   = "llama-3.3-70b-versatile"
GROQ_URL     = "https://api.groq.com/openai/v1/chat/completions"


async def call_llm(system: str, messages: list[dict], max_tokens: int = 1024) -> str:
    """Chiamata base all'API Groq. Tutti gli agenti la usano."""
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model":      GROQ_MODEL,
                "max_tokens": max_tokens,
                "messages":   [{"role": "system", "content": system}] + messages,
            }
        )
        data = response.json()
        if response.status_code != 200:
            raise RuntimeError(data.get("error", {}).get("message", "Errore API Groq"))
        return data["choices"][0]["message"]["content"].strip()


# ── AGENTE 1: MAX ─────────────────────────────────────────────────────────────
# Max è il pianificatore. Freddo e diretto, decide cosa cercare.
# Non sa ancora nulla della risposta — il suo unico job è trasformare
# la domanda dell'utente in 1-3 query di ricerca ottimali.

MAX_SYSTEM = """Sei Max, un assistente analitico e diretto.
Il tuo unico compito è decidere quali query di ricerca web usare per rispondere alla domanda dell'utente.

Regole:
- Rispondi SOLO con le query, una per riga, senza spiegazioni
- Massimo 3 query
- Le query devono essere in italiano o inglese a seconda di cosa funziona meglio
- Sii specifico: preferisci "champion league 2024 vincitore" a "calcio"
- Se la domanda non richiede ricerca (es. calcoli, opinioni), rispondi con: NESSUNA_RICERCA"""

async def max_plan(user_question: str) -> list[str]:
    """
    Max decide le query di ricerca.
    Restituisce una lista di stringhe (le query) oppure lista vuota se non serve ricerca.
    """
    result = await call_llm(
        system=MAX_SYSTEM,
        messages=[{"role": "user", "content": f"Domanda dell'utente: {user_question}"}],
        max_tokens=200
    )

    if "NESSUNA_RICERCA" in result:
        return []

    # Ogni riga non vuota è una query
    queries = [line.strip() for line in result.strip().splitlines() if line.strip()]
    return queries[:3]  # massimo 3


# ── AGENTE 2: SOFIA ───────────────────────────────────────────────────────────
# Sofia è la ricercatrice. Curiosa ed entusiasta, legge i risultati grezzi
# e li sintetizza in un briefing chiaro per Alex.

SOFIA_SYSTEM = """Sei Sofia, una ricercatrice curiosa ed entusiasta.
Hai appena eseguito delle ricerche web e devi sintetizzare i risultati in un briefing
chiaro e strutturato per un collega che dovrà rispondere all'utente.

Regole:
- Estrai solo le informazioni rilevanti alla domanda
- Organizza per punti chiave
- Segnala se le fonti si contraddicono
- Sii concisa: massimo 300 parole
- Non rispondere all'utente direttamente — stai scrivendo per un collega"""

async def sofia_synthesize(user_question: str, queries: list[str]) -> tuple[str, str]:
    """
    Sofia esegue le ricerche e sintetizza i risultati.
    Restituisce (briefing, raw_results) dove raw_results è il testo grezzo delle ricerche.
    """
    # Esegue tutte le query e raccoglie i risultati
    all_results = []
    for query in queries:
        results = web_search(query, max_results=3)
        all_results.append(f"Query: '{query}'\n{format_results(results)}")

    raw = "\n\n---\n\n".join(all_results)

    briefing = await call_llm(
        system=SOFIA_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"Domanda originale: {user_question}\n\nRisultati delle ricerche:\n{raw}"
        }],
        max_tokens=500
    )

    return briefing, raw


# ── AGENTE 3: ALEX ────────────────────────────────────────────────────────────
# Alex è il comunicatore finale. Analitico e preciso, legge il piano di Max
# e il briefing di Sofia, e costruisce la risposta definitiva per l'utente.

ALEX_SYSTEM = """Sei Alex, un assistente preciso e affidabile.
Hai ricevuto da due colleghi:
- Max ha pianificato le query di ricerca
- Sofia ha sintetizzato i risultati

Il tuo compito è rispondere all'utente in modo chiaro, accurato e ben strutturato.

Regole:
- Rispondi direttamente alla domanda
- Usa i dati del briefing di Sofia, non inventare
- Formatta bene la risposta con Markdown (grassetto, liste) dove utile
- Alla fine aggiungi una riga "📎 Fonti:" con i domini delle fonti principali se disponibili
- Tono: professionale ma accessibile
- Lingua: sempre italiano"""

async def alex_answer(user_question: str, max_queries: list[str], sofia_briefing: str) -> str:
    """
    Alex produce la risposta finale per l'utente.
    Riceve il contesto completo del lavoro degli altri agenti.
    """
    context = f"""Domanda dell'utente: {user_question}

Piano di ricerca di Max (query usate):
{chr(10).join(f'- {q}' for q in max_queries) if max_queries else '- Nessuna ricerca necessaria'}

Briefing di Sofia (sintesi delle ricerche):
{sofia_briefing}"""

    answer = await call_llm(
        system=ALEX_SYSTEM,
        messages=[{"role": "user", "content": context}],
        max_tokens=1024
    )

    return answer


# ── PIPELINE COMPLETA ─────────────────────────────────────────────────────────

async def run_pipeline(user_question: str) -> dict:
    """
    Esegue il pipeline completo: Max → Sofia → Alex.
    Restituisce un dizionario con i contributi di ogni agente
    così il bot può mostrarli progressivamente su Telegram.
    """

    # Step 1: Max pianifica
    queries = await max_plan(user_question)

    # Step 2: Sofia ricerca e sintetizza (solo se ci sono query)
    if queries:
        briefing, _ = await sofia_synthesize(user_question, queries)
    else:
        briefing = "Nessuna ricerca necessaria per questa domanda."

    # Step 3: Alex risponde
    answer = await alex_answer(user_question, queries, briefing)

    return {
        "queries":  queries,
        "briefing": briefing,
        "answer":   answer,
    }
