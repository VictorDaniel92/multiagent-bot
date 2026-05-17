"""
marco_agent.py — Marco, l'agente viaggi.

Energico, travolgente, ti riempie di attività anche quando vuoi rilassarti.
Collabora con Giorgio per le previsioni meteo sulla destinazione.
"""

import logging
from pathlib import Path
from agents import call_llm

logger    = logging.getLogger(__name__)
SOULS_DIR = Path(__file__).parent / "souls"


def _load_soul(name: str) -> str:
    path = SOULS_DIR / f"{name}.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""

SOUL_MARCO = _load_soul("marco")


# ── Collaborazione con Giorgio ─────────────────────────────────────────────────

async def _get_weather_for_city(city_name: str) -> str:
    """
    Chiede a Giorgio le previsioni per la città di destinazione.
    Restituisce una stringa con il meteo o stringa vuota se non disponibile.
    """
    try:
        from weather_agent import extract_city, giorgio_forecast
        city = await extract_city(city_name)
        if not city:
            return ""
        forecast = await giorgio_forecast(city, hours=24)
        return forecast
    except Exception as e:
        logger.error(f"Errore collaborazione Marco→Giorgio: {e}")
        return ""


async def _get_travel_thread_context(limit: int = 4) -> str:
    """Recupera le ultime conversazioni nel topic viaggi per non ripetersi."""
    try:
        from memory_vector import get_recent_conversations, _time_ago
        recents = await get_recent_conversations(topic="viaggi", agent="marco", limit=limit)
        if not recents:
            return ""
        lines = ["## Itinerari che hai già suggerito di recente (varia, non ripetere le stesse mete)"]
        for r in recents:
            ago = _time_ago(r["created_at"])
            lines.append(f"- [{ago}] {r['question'][:80]} → {r['answer'][:100]}")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Errore thread context viaggi: {e}")
        return ""


# ── Funzioni principali ────────────────────────────────────────────────────────

async def marco_plan_trip(question: str, city_hint: str | None = None) -> str:
    """
    Marco risponde a una richiesta di viaggio/itinerario.
    Se riesce a capire la città, chiede il meteo a Giorgio e lo integra.

    Args:
        question:  la domanda dell'utente
        city_hint: città già estratta da Sophia (opzionale)
    """
    # Estrae la città dalla domanda se non già fornita
    destination = city_hint
    if not destination:
        destination = await _extract_destination(question)

    # Chiede il meteo a Giorgio (collaborazione inter-agente)
    weather_ctx = ""
    if destination:
        logger.info(f"Marco chiede meteo a Giorgio per: {destination}")
        weather_raw = await _get_weather_for_city(destination)
        if weather_raw:
            weather_ctx = f"\n\n## Previsioni meteo (da Giorgio) per {destination}:\n{weather_raw[:600]}"

    thread_ctx = await _get_travel_thread_context()

    system = f"""{SOUL_MARCO}

Un utente ti ha chiesto aiuto per pianificare un viaggio o un'attività.
Rispondi con il tuo stile travolgente: dai un itinerario concreto, pieno di energia,
con orari, prezzi indicativi e consigli pratici.

Se hai le previsioni meteo, integrале naturalmente nell'itinerario —
non lamentarti del tempo, trova sempre il lato positivo o adatta il piano.

Formato:
- Inizia con una frase d'impatto che cattura l'entusiasmo
- Poi l'itinerario vero, strutturato ma dinamico
- Chiudi con una frase che sprona a partire subito
- Usa *grassetto* per i nomi dei posti
- Max 300 parole
- Scrivi in italiano

{thread_ctx}
{weather_ctx}"""

    return await call_llm(
        system=system,
        messages=[{"role": "user", "content": question}],
        max_tokens=500,
    )


async def marco_quick_tip(question: str) -> str:
    """
    Marco risponde a domande rapide su viaggi (non itinerari completi).
    Es: "vale la pena andare a Lecce?", "cosa mangio a Napoli?"
    """
    thread_ctx = await _get_travel_thread_context(limit=3)

    system = f"""{SOUL_MARCO}

Risposta rapida e diretta — massimo 4 frasi.
Sii concreto, opinionato, entusiasta. Niente intro, vai dritto al punto.
Scrivi in italiano. Usa *grassetto* per nomi di posti o piatti.

{thread_ctx}"""

    return await call_llm(
        system=system,
        messages=[{"role": "user", "content": question}],
        max_tokens=200,
    )


async def marco_answer_question(question: str) -> str:
    """
    Entry point principale — decide se è una domanda rapida o un itinerario completo.
    """
    # Classifica la domanda
    classification = await call_llm(
        system="""Classifica la domanda sul viaggio. Rispondi SOLO con JSON:
{"type": "itinerary"} se chiede un piano/itinerario/cosa fare per un periodo
{"type": "quick"} se è una domanda rapida su una meta, cibo, consiglio singolo""",
        messages=[{"role": "user", "content": question}],
        max_tokens=30,
    )

    import json, re
    try:
        q_type = json.loads(re.search(r'\{.*\}', classification).group()).get("type", "quick")
    except Exception:
        q_type = "quick"

    if q_type == "itinerary":
        return await marco_plan_trip(question)
    else:
        return await marco_quick_tip(question)


async def _extract_destination(question: str) -> str | None:
    """Estrae la città/destinazione dalla domanda."""
    result = await call_llm(
        system="""Estrai la città o destinazione italiana dalla domanda di viaggio.
Rispondi SOLO con il nome della città in minuscolo (es: "roma", "napoli", "lecce").
Se non c'è una destinazione specifica o è estera, rispondi: NESSUNA""",
        messages=[{"role": "user", "content": question}],
        max_tokens=20,
    )
    city = result.strip().lower()
    return None if city == "nessuna" or not city else city
