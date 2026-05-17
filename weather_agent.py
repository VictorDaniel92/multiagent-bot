import logging
import httpx
from pathlib import Path
from datetime import datetime, timezone
from agents import call_llm

logger   = logging.getLogger(__name__)
SOULS_DIR = Path(__file__).parent / "souls"

# ── SOUL ──────────────────────────────────────────────────────────────────────

def _load_soul(name: str) -> str:
    path = SOULS_DIR / f"{name}.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""

SOUL_GIORGIO = _load_soul("meteo")

# ── CITTÀ CONOSCIUTE ──────────────────────────────────────────────────────────
# Coordinate delle città supportate + alias per il riconoscimento
CITIES = {
    "milano":        {"lat": 45.4642, "lon": 9.1900,  "name": "Milano"},
    "milan":         {"lat": 45.4642, "lon": 9.1900,  "name": "Milano"},
    "roma":          {"lat": 41.9028, "lon": 12.4964, "name": "Roma"},
    "rome":          {"lat": 41.9028, "lon": 12.4964, "name": "Roma"},
    "lecce":         {"lat": 40.3519, "lon": 18.1750, "name": "Lecce"},
    "palo del colle":{"lat": 41.0567, "lon": 16.6947, "name": "Palo del Colle"},
    "palo":          {"lat": 41.0567, "lon": 16.6947, "name": "Palo del Colle"},
    "napoli":        {"lat": 40.8518, "lon": 14.2681, "name": "Napoli"},
    "torino":        {"lat": 45.0703, "lon": 7.6869,  "name": "Torino"},
    "firenze":       {"lat": 43.7696, "lon": 11.2558, "name": "Firenze"},
    "bologna":       {"lat": 44.4949, "lon": 11.3426, "name": "Bologna"},
    "venezia":       {"lat": 45.4408, "lon": 12.3155, "name": "Venezia"},
    "bari":          {"lat": 41.1171, "lon": 16.8719, "name": "Bari"},
    "palermo":       {"lat": 38.1157, "lon": 13.3615, "name": "Palermo"},
}

# Codici meteo WMO → descrizione leggibile
WMO_CODES = {
    0:  "cielo sereno",
    1:  "prevalentemente sereno", 2: "parzialmente nuvoloso", 3: "nuvoloso",
    45: "nebbia", 48: "nebbia con brina",
    51: "pioggerella leggera", 53: "pioggerella moderata", 55: "pioggerella intensa",
    61: "pioggia leggera", 63: "pioggia moderata", 65: "pioggia intensa",
    71: "neve leggera", 73: "neve moderata", 75: "neve intensa",
    80: "rovesci leggeri", 81: "rovesci moderati", 82: "rovesci intensi",
    95: "temporale", 96: "temporale con grandine", 99: "temporale con grandine intensa",
}


# ── GEOCODING LLM ─────────────────────────────────────────────────────────────

async def extract_city(user_message: str) -> dict | None:
    """
    Usa l'LLM per estrarre il nome della città dal messaggio dell'utente.
    Restituisce il dict della città se trovata, None altrimenti.
    """
    known = ", ".join(sorted(set(v["name"] for v in CITIES.values())))

    result = await call_llm(
        system=f"""Estrai il nome della città dal messaggio.
Città supportate: {known}.
Rispondi SOLO con il nome della città in minuscolo (es: "milano", "roma", "palo del colle").
Se non c'è una città o non è supportata, rispondi: NESSUNA""",
        messages=[{"role": "user", "content": user_message}],
        max_tokens=20
    )

    city_key = result.strip().lower()
    if city_key == "nessuna" or city_key not in CITIES:
        # Prova match parziale
        for key in CITIES:
            if key in city_key or city_key in key:
                return CITIES[key]
        return None

    return CITIES[city_key]


# ── OPEN-METEO ────────────────────────────────────────────────────────────────

async def fetch_weather(lat: float, lon: float, hours: int = 8) -> dict | None:
    """
    Chiama Open-Meteo e restituisce i dati meteo orari.
    Gratuito, zero API key, aggiornato ogni ora.
    """
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude":   lat,
        "longitude":  lon,
        "hourly":     "temperature_2m,precipitation_probability,weathercode,windspeed_10m,apparent_temperature",
        "forecast_days": 1,
        "timezone":   "Europe/Rome",
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error(f"Errore Open-Meteo: {e}")
        return None


def parse_next_hours(data: dict, hours: int = 8) -> list[dict]:
    """
    Estrae le prossime N ore dai dati Open-Meteo.
    Parte dall'ora corrente italiana.
    """
    now_hour = datetime.now().hour
    hourly   = data["hourly"]

    results = []
    for i, time_str in enumerate(hourly["time"]):
        hour = int(time_str.split("T")[1].split(":")[0])
        if hour < now_hour:
            continue
        results.append({
            "time":     time_str.split("T")[1][:5],  # "14:00"
            "temp":     hourly["temperature_2m"][i],
            "feels":    hourly["apparent_temperature"][i],
            "rain_pct": hourly["precipitation_probability"][i],
            "wind":     hourly["windspeed_10m"][i],
            "code":     hourly["weathercode"][i],
            "desc":     WMO_CODES.get(hourly["weathercode"][i], "condizioni variabili"),
        })
        if len(results) >= hours:
            break

    return results


def format_for_llm(city_name: str, hours_data: list[dict]) -> str:
    """Formatta i dati meteo in testo leggibile per l'LLM."""
    lines = [f"Dati meteo per {city_name} — prossime ore:\n"]
    for h in hours_data:
        lines.append(
            f"{h['time']}: {h['temp']}°C (percepita {h['feels']}°C), "
            f"{h['desc']}, vento {h['wind']} km/h, "
            f"probabilità pioggia {h['rain_pct']}%"
        )
    return "\n".join(lines)


# ── AGENTE GIORGIO ────────────────────────────────────────────────────────────

async def _get_meteo_thread_context(limit: int = 4) -> str:
    """Recupera le ultime previsioni già date nel topic meteo."""
    try:
        from memory_vector import get_recent_conversations
        recents = await get_recent_conversations(topic="meteo", agent="giorgio", limit=limit)
        if not recents:
            return ""
        lines = ["## Previsioni che hai già dato di recente (non ripetere le stesse identiche osservazioni)"]
        for r in recents:
            from memory_vector import _time_ago
            ago = _time_ago(r["created_at"])
            lines.append(f"- [{ago}] {r['question'][:60]} → {r['answer'][:100]}")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Errore thread context meteo: {e}")
        return ""


async def giorgio_forecast(city: dict, hours: int = 8) -> str:
    """Giorgio racconta il meteo delle prossime N ore per una città."""
    data = await fetch_weather(city["lat"], city["lon"], hours)
    if not data:
        return f"⚠️ Non riesco a recuperare i dati meteo per {city['name']} in questo momento."

    hours_data   = parse_next_hours(data, hours)
    weather_text = format_for_llm(city["name"], hours_data)
    thread_ctx   = await _get_meteo_thread_context()

    system = f"""{SOUL_GIORGIO}

Stai dando le previsioni meteo per le prossime {hours} ore nel tuo stile da TG.
Hai i dati precisi — usali, non inventare nulla.
Se hai già dato previsioni simili di recente, varia il tono e aggiungi dettagli nuovi.

{thread_ctx}"""

    return await call_llm(
        system=system,
        messages=[{"role": "user", "content": weather_text}],
        max_tokens=400,
    )


async def giorgio_morning_briefing(cities: list[dict]) -> str:
    """
    Giorgio fa il briefing meteo mattutino per più città.
    Usato dal job delle 7:00.
    """
    all_data = []
    for city in cities:
        data = await fetch_weather(city["lat"], city["lon"], 12)
        if data:
            hours_data = parse_next_hours(data, 12)
            all_data.append(format_for_llm(city["name"], hours_data))

    if not all_data:
        return "⚠️ Impossibile recuperare i dati meteo stamattina."

    combined = "\n\n".join(all_data)

    system = f"""{SOUL_GIORGIO}

Stai aprendo il TG mattutino con le previsioni per oggi.
Hai i dati per più città — fai una rassegna completa ma scorrevole.
Inizia con: 🌤️ *Buongiorno! Le previsioni di Giorgio per oggi*
Poi city per city, breve ma preciso. Chiudi con un consiglio generale."""

    return await call_llm(
        system=system,
        messages=[{"role": "user", "content": combined}],
        max_tokens=600,
    )


# Città del briefing mattutino
MORNING_CITIES = [
    CITIES["milano"],
    CITIES["palo del colle"],
    CITIES["lecce"],
    CITIES["roma"],
]
