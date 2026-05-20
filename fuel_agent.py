"""
fuel_agent.py — Prezzi benzina per città con confronto media mensile

Fonte: Ministero delle Imprese e del Made in Italy (Mise)
API pubblica, zero API key, dati ufficiali aggiornati giornalmente.
"""
import os
import io
import csv
import json
import logging
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import Column, String, Float, DateTime, Integer, select
from sqlalchemy.orm import DeclarativeBase

import memory as mem
from agents import call_llm

logger    = logging.getLogger(__name__)
SOULS_DIR = Path(__file__).parent / "souls"


# ── SOUL SOPHIA ───────────────────────────────────────────────────────────────

def _load_soul(name: str) -> str:
    path = SOULS_DIR / f"{name}.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""

SOUL_SOPHIA = _load_soul("sophia")


# ── CITTÀ SUPPORTATE ──────────────────────────────────────────────────────────

# Mappa città → provincia Mise (codice provincia italiano)
CITIES = {
    "milano":        {"province": "MI", "name": "Milano"},
    "milan":         {"province": "MI", "name": "Milano"},
    "palo del colle":{"province": "BA", "name": "Palo del Colle (BA)"},
    "palo":          {"province": "BA", "name": "Palo del Colle (BA)"},
    "bari":          {"province": "BA", "name": "Bari"},
    "roma":          {"province": "RM", "name": "Roma"},
    "napoli":        {"province": "NA", "name": "Napoli"},
    "torino":        {"province": "TO", "name": "Torino"},
}

# Le due città di default per il confronto
DEFAULT_CITIES = ["milano", "palo del colle"]

# URL API Mise — prezzi medi per provincia, aggiornati ogni giorno
MISE_URL = "https://carburanti.mise.gov.it/ospzApi/api/inRete/proMese"


# ── DB STORICO PREZZI ─────────────────────────────────────────────────────────

class FuelBase(DeclarativeBase):
    pass


class FuelPrice(FuelBase):
    """Storico prezzi benzina per provincia — un record per giorno per provincia."""
    __tablename__ = "fuel_prices"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    province   = Column(String,  nullable=False, index=True)
    fuel_type  = Column(String,  nullable=False)  # benzina | gasolio | gpl
    price      = Column(Float,   nullable=False)  # €/litro
    date       = Column(DateTime, default=datetime.utcnow, index=True)


async def init_fuel_db():
    if not mem.engine:
        return
    async with mem.engine.begin() as conn:
        await conn.run_sync(FuelBase.metadata.create_all)
    logger.info("DB carburanti inizializzato ✅")


async def save_price(province: str, fuel_type: str, price: float):
    """Salva un rilevamento di prezzo nel DB."""
    if not mem.SessionLocal:
        return
    try:
        async with mem.SessionLocal() as db:
            db.add(FuelPrice(
                province  = province,
                fuel_type = fuel_type,
                price     = price,
                date      = datetime.utcnow(),
            ))
            await db.commit()
    except Exception as e:
        logger.error(f"Errore salvataggio prezzo: {e}")


async def get_monthly_average(province: str, fuel_type: str) -> float | None:
    """Recupera la media dei prezzi dell'ultimo mese dal DB storico."""
    if not mem.SessionLocal:
        return None
    try:
        cutoff = datetime.utcnow() - timedelta(days=30)
        async with mem.SessionLocal() as db:
            result = await db.execute(
                select(FuelPrice).where(
                    FuelPrice.province  == province,
                    FuelPrice.fuel_type == fuel_type,
                    FuelPrice.date      >= cutoff,
                )
            )
            rows = result.scalars().all()
        if not rows:
            return None
        return sum(r.price for r in rows) / len(rows)
    except Exception as e:
        logger.error(f"Errore media mensile: {e}")
        return None


# ── FETCH PREZZI MISE ─────────────────────────────────────────────────────────

def _fetch_mise_prices(province: str) -> dict | None:
    """
    Chiama l'API Mise e restituisce i prezzi medi per provincia.
    Restituisce: {benzina: float, gasolio: float, gpl: float} o None se errore.
    """
    try:
        now   = datetime.now()
        url   = f"{MISE_URL}/{now.year}/{now.month:02d}/{province}"
        req   = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; FuelBot/1.0)"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        # L'API restituisce lista di record per tipo carburante
        prices = {}
        for item in data:
            tipo  = item.get("carburante", "").lower()
            price = item.get("prezzoSelf", 0) or item.get("prezzo", 0)
            if price and price > 0:
                if "benzin" in tipo:
                    prices["benzina"] = round(float(price), 3)
                elif "gasoli" in tipo or "diesel" in tipo:
                    prices["gasolio"] = round(float(price), 3)
                elif "gpl" in tipo or "gas" in tipo:
                    prices["gpl"] = round(float(price), 3)

        return prices if prices else None

    except Exception as e:
        logger.error(f"Errore fetch Mise {province}: {e}")
        return None


def _fetch_national_average() -> dict | None:
    """Recupera la media nazionale dal Mise."""
    try:
        now = datetime.now()
        url = f"https://carburanti.mise.gov.it/ospzApi/api/inRete/nazMese/{now.year}/{now.month:02d}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; FuelBot/1.0)"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        prices = {}
        for item in data:
            tipo  = item.get("carburante", "").lower()
            price = item.get("prezzoSelf", 0) or item.get("prezzo", 0)
            if price and price > 0:
                if "benzin" in tipo:
                    prices["benzina"] = round(float(price), 3)
                elif "gasoli" in tipo or "diesel" in tipo:
                    prices["gasolio"] = round(float(price), 3)
        return prices if prices else None

    except Exception as e:
        logger.error(f"Errore fetch media nazionale: {e}")
        return None


# ── ANALISI E RISPOSTA ────────────────────────────────────────────────────────

async def get_fuel_report(city_keys: list[str] = None) -> str:
    """
    Genera un report prezzi benzina per le città richieste
    con confronto vs media mensile e media nazionale.
    """
    if not city_keys:
        city_keys = DEFAULT_CITIES

    # Raccoglie dati per ogni città
    city_data = []
    for key in city_keys:
        city = CITIES.get(key.lower())
        if not city:
            continue

        prices   = _fetch_mise_prices(city["province"])
        monthly  = await get_monthly_average(city["province"], "benzina")

        if prices:
            # Salva nel DB per costruire lo storico
            for fuel_type, price in prices.items():
                await save_price(city["province"], fuel_type, price)

        city_data.append({
            "name":     city["name"],
            "province": city["province"],
            "prices":   prices or {},
            "monthly_avg": monthly,
        })

    # Media nazionale
    national = _fetch_national_average()

    # Costruisce il testo da passare all'LLM
    data_text = f"Data: {datetime.now().strftime('%d/%m/%Y')}\n\n"

    for c in city_data:
        data_text += f"**{c['name']}**\n"
        if c["prices"]:
            for fuel, price in c["prices"].items():
                data_text += f"  - {fuel.capitalize()} self: {price:.3f} €/L\n"
            if c["monthly_avg"]:
                benzina = c["prices"].get("benzina")
                if benzina:
                    diff = benzina - c["monthly_avg"]
                    sign = "+" if diff > 0 else ""
                    data_text += f"  - Media ultimi 30gg: {c['monthly_avg']:.3f} €/L ({sign}{diff:.3f})\n"
            else:
                data_text += "  - (nessuno storico mensile disponibile ancora)\n"
        else:
            data_text += "  - Dati non disponibili\n"
        data_text += "\n"

    if national:
        data_text += f"**Media nazionale:**\n"
        for fuel, price in national.items():
            data_text += f"  - {fuel.capitalize()}: {price:.3f} €/L\n"

    # Sophia analizza e risponde
    response = await call_llm(
        system=f"""{SOUL_SOPHIA}

Stai riportando i prezzi dei carburanti a un utente che abita a Milano
e ha famiglia a Palo del Colle (Bari).

Analizza i dati e rispondi in modo chiaro e pratico:
- Indica i prezzi attuali per città
- Confronta con la media mensile se disponibile (usa frecce ⬆️⬇️➡️)
- Confronta con la media nazionale
- Dai un consiglio pratico se il prezzo è particolarmente alto o basso
- Se non ci sono dati storici ancora, dillo onestamente

Formato Telegram, max 200 parole, usa *grassetto* per i prezzi chiave.
Inizia con ⛽""",
        messages=[{"role": "user", "content": data_text}],
        max_tokens=400,
    )

    return response.strip()


async def extract_city_from_question(question: str) -> list[str]:
    """Estrae le città dalla domanda dell'utente."""
    known = ", ".join(CITIES.keys())
    result = await call_llm(
        system=f"""Estrai le città dalla domanda. Città supportate: {known}.
Rispondi SOLO con i nomi in minuscolo separati da virgola.
Se non ci sono città specifiche, rispondi: DEFAULT""",
        messages=[{"role": "user", "content": question}],
        max_tokens=20,
    )
    if "DEFAULT" in result:
        return DEFAULT_CITIES
    return [c.strip() for c in result.split(",") if c.strip() in CITIES]
