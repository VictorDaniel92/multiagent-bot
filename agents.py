import os
import httpx
import logging
from pathlib import Path
from search import web_search, format_results

logger = logging.getLogger(__name__)

GROQ_API_KEY = os.environ["GROQ_API_KEY"]
GROQ_MODEL   = "llama-3.3-70b-versatile"
GROQ_URL     = "https://api.groq.com/openai/v1/chat/completions"

SOULS_DIR = Path(__file__).parent / "souls"


# ── CARICAMENTO SOUL FILES ────────────────────────────────────────────────────

def load_soul(name: str) -> str:
    """
    Carica un file soul dalla cartella souls/.
    Se il file non esiste restituisce stringa vuota con warning.
    """
    path = SOULS_DIR / f"{name}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    logger.warning(f"Soul file non trovato: {path}")
    return ""

# Carica i soul all'avvio — così non li rilegge ad ogni messaggio
SOUL_MAX   = load_soul("max")
SOUL_SOFIA = load_soul("sofia")
SOUL_ALEX  = load_soul("alex")
SOUL_USER  = load_soul("user")


def reload_soul(name: str) -> bool:
    """
    Ricarica un soul dal filesystem e aggiorna il global corrispondente.
    Chiamato dal Mentor dopo aver applicato una modifica al soul.md.
    Ritorna True se il reload è riuscito, False altrimenti.
    """
    global SOUL_MAX, SOUL_SOFIA, SOUL_ALEX, SOUL_USER

    new_content = load_soul(name)
    if not new_content:
        logger.error(f"reload_soul: impossibile ricaricare '{name}' — file vuoto o mancante")
        return False

    mapping = {
        "max":   "SOUL_MAX",
        "sofia": "SOUL_SOFIA",
        "alex":  "SOUL_ALEX",
        "user":  "SOUL_USER",
    }

    if name not in mapping:
        logger.warning(f"reload_soul: '{name}' non ha un global dedicato, nessun reload in memoria")
        return True  # il file è stato scritto, anche se non c'è global da aggiornare

    if name == "max":
        SOUL_MAX = new_content
    elif name == "sofia":
        SOUL_SOFIA = new_content
    elif name == "alex":
        SOUL_ALEX = new_content
    elif name == "user":
        SOUL_USER = new_content

    logger.info(f"Soul '{name}' ricaricato in memoria ✅ ({len(new_content)} chars)")
    return True


# ── PIPELINE PER TOPIC ────────────────────────────────────────────────────────

# Ogni topic ha una configurazione diversa che cambia il comportamento degli agenti
TOPIC_CONFIGS = {
    "ricerca": {
        "max_queries":    3,
        "sofia_focus":    "Cerca informazioni fattuali e aggiornate. Cita le fonti.",
        "alex_style":     "Rispondi in modo informativo e preciso. Struttura la risposta con punti chiave.",
        "use_search":     True,
    },
    "coding": {
        "max_queries":    2,
        "sofia_focus":    "Cerca documentazione tecnica, esempi di codice e best practice.",
        "alex_style":     "Rispondi con esempi pratici di codice. Preferisci snippet funzionanti a spiegazioni teoriche.",
        "use_search":     True,
    },
    "brainstorming": {
        "max_queries":    1,
        "sofia_focus":    "Cerca idee, esempi creativi e approcci alternativi. Sii aperta a connessioni inaspettate.",
        "alex_style":     "Sii creativo e propositivo. Offri più prospettive. Non limitarti alla risposta ovvia.",
        "use_search":     False,  # Brainstorming usa principalmente la conoscenza interna
    },
    "analisi": {
        "max_queries":    2,
        "sofia_focus":    "Cerca dati, statistiche e opinioni di esperti. Segnala i punti di vista contrastanti.",
        "alex_style":     "Analizza il problema da più angolazioni. Struttura pro/contro. Sii obiettivo.",
        "use_search":     True,
    },
    "default": {
        "max_queries":    3,
        "sofia_focus":    "Cerca informazioni rilevanti e accurate.",
        "alex_style":     "Rispondi in modo chiaro e completo.",
        "use_search":     True,
    }
}


# ── TOPIC GUARD ───────────────────────────────────────────────────────────────

# Descrizioni dei topic usate dal guard per classificare la domanda
TOPIC_DESCRIPTIONS = {
    "ricerca":       "informazioni generali, notizie, fatti, storia, scienza, attualità",
    "coding":        "programmazione, codice, bug, script, linguaggi, API, software, tech",
    "brainstorming": "idee creative, spunti, inventare, proporre alternative, pensiero laterale",
    "analisi":       "analisi strutturata, confronto, pro e contro, valutazione, dati, statistiche",
    "news":          "notizie videogiochi, gaming, recensioni, annunci, industria videoludica",
}

async def topic_guard(question: str, current_topic: str) -> dict:
    """
    Verifica se la domanda è coerente col topic attuale.
    Ritorna {"match": bool, "suggested": str, "reason": str}
    """
    if current_topic not in TOPIC_DESCRIPTIONS:
        return {"match": True, "suggested": current_topic, "reason": ""}

    topic_list = "\n".join(
        f'- "{t}": {desc}' for t, desc in TOPIC_DESCRIPTIONS.items()
    )

    system = """Sei un classificatore di messaggi per un bot Telegram multi-topic.
Il tuo unico compito è capire se una domanda è nel topic giusto.
Rispondi SOLO con JSON valido, nessun testo aggiuntivo."""

    prompt = f"""Topic attuale: "{current_topic}" ({TOPIC_DESCRIPTIONS[current_topic]})

Topic disponibili:
{topic_list}

Domanda dell'utente: "{question}"

Rispondi con questo JSON:
{{"match": true/false, "suggested": "topic_più_adatto", "reason": "breve spiegazione in italiano"}}

- match: true se la domanda è ragionevolmente nel topic attuale (anche parzialmente)
- suggested: il topic più adatto tra {list(TOPIC_DESCRIPTIONS.keys())}
- Se match è true, suggested può essere uguale al topic attuale"""

    import json, re
    raw = await call_llm(
        system=system,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=150
    )
    try:
        data = json.loads(re.search(r'\{.*\}', raw, re.DOTALL).group())
        return {
            "match":     bool(data.get("match", True)),
            "suggested": data.get("suggested", current_topic),
            "reason":    data.get("reason", ""),
        }
    except Exception:
        # In caso di errore di parsing, lascia passare
        return {"match": True, "suggested": current_topic, "reason": ""}


# ── LLM BASE ─────────────────────────────────────────────────────────────────

async def call_llm(system: str, messages: list[dict], max_tokens: int = 1024) -> str:
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


# ── AGENTE MAX ────────────────────────────────────────────────────────────────

async def max_plan(user_question: str, topic: str = "default", memory_context: str = "") -> list[str]:
    """
    Max decide le query di ricerca basandosi sul suo SOUL e sulla config del topic.
    """
    config = TOPIC_CONFIGS.get(topic, TOPIC_CONFIGS["default"])

    # Se il topic non usa la ricerca, Max non pianifica nulla
    if not config["use_search"]:
        return []

    system = f"""{SOUL_MAX}

{SOUL_USER}

{memory_context}

## Contesto operativo
Stai lavorando nel topic: {topic.upper()}
Massimo query consentite: {config['max_queries']}

Rispondi SOLO con le query di ricerca, una per riga, senza spiegazioni.
Se la domanda non richiede ricerca esterna, rispondi con: NESSUNA_RICERCA"""

    result = await call_llm(
        system=system,
        messages=[{"role": "user", "content": f"Domanda: {user_question}"}],
        max_tokens=200
    )

    if "NESSUNA_RICERCA" in result:
        return []

    queries = [line.strip() for line in result.strip().splitlines() if line.strip()]
    return queries[:config["max_queries"]]


# ── AGENTE SOFIA ──────────────────────────────────────────────────────────────

async def sofia_synthesize(
    user_question: str,
    queries: list[str],
    topic: str = "default",
    memory_context: str = ""
) -> str:
    """
    Sofia esegue le ricerche e sintetizza i risultati.
    Il focus cambia in base al topic.
    """
    config = TOPIC_CONFIGS.get(topic, TOPIC_CONFIGS["default"])

    # Esegue tutte le query
    all_results = []
    for query in queries:
        results = web_search(query, max_results=3)
        all_results.append(f"Query: '{query}'\n{format_results(results)}")

    raw = "\n\n---\n\n".join(all_results) if all_results else "Nessuna ricerca eseguita."

    system = f"""{SOUL_SOFIA}

{SOUL_USER}

{memory_context}

## Focus per questo topic
{config['sofia_focus']}

Stai scrivendo un briefing per Alex, non per l'utente.
Massimo 300 parole. Sii selettiva — includi solo ciò che è rilevante."""

    return await call_llm(
        system=system,
        messages=[{
            "role": "user",
            "content": f"Domanda originale: {user_question}\n\nRisultati ricerche:\n{raw}"
        }],
        max_tokens=600
    )


# ── AGENTE ALEX ───────────────────────────────────────────────────────────────

async def alex_answer(
    user_question: str,
    max_queries: list[str],
    sofia_briefing: str,
    topic: str = "default",
    memory_context: str = ""
) -> str:
    """
    Alex produce la risposta finale per l'utente.
    Lo stile cambia in base al topic.
    """
    config = TOPIC_CONFIGS.get(topic, TOPIC_CONFIGS["default"])

    system = f"""{SOUL_ALEX}

{memory_context}

## Stile per questo topic
{config['alex_style']}

Adatta sempre il tono, la lunghezza e il livello di dettaglio al profilo utente sopra.
Rispondi sempre in italiano. Sii diretto — inizia subito con la risposta."""

    context = f"""Domanda: {user_question}

Query usate da Max:
{chr(10).join(f'- {q}' for q in max_queries) if max_queries else '- Nessuna ricerca (risposta dalla conoscenza interna)'}

Briefing di Sofia:
{sofia_briefing}"""

    return await call_llm(
        system=system,
        messages=[{"role": "user", "content": context}],
        max_tokens=1024
    )


# ── PIPELINE COMPLETO ─────────────────────────────────────────────────────────

async def run_pipeline(user_question: str, topic: str = "default", memory_context: str = "") -> dict:
    """
    Esegue Max → Sofia → Alex con la configurazione del topic corretto.
    """
    queries = await max_plan(user_question, topic, memory_context)

    if queries:
        briefing = await sofia_synthesize(user_question, queries, topic, memory_context)
    else:
        briefing = "Nessuna ricerca eseguita — risposta basata sulla conoscenza interna."

    answer = await alex_answer(user_question, queries, briefing, topic, memory_context)

    return {
        "queries":  queries,
        "briefing": briefing,
        "answer":   answer,
        "topic":    topic,
    }
