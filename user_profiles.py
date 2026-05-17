"""
user_profiles.py — Profili utente ricchi e personalizzati

Due modalità:
- Manuale (te): dichiari i tuoi dati e vengono salvati strutturati
- Automatica (altri): l'LLM osserva le domande e costruisce il profilo nel tempo
"""
import json
import logging
from datetime import datetime
from sqlalchemy import Column, String, Text, DateTime, select
from sqlalchemy.orm import DeclarativeBase

import memory as mem
from agents import call_llm

logger = logging.getLogger(__name__)

# Il tuo user_id Telegram
OWNER_ID = 19968873


# ── MODELLO DB ────────────────────────────────────────────────────────────────

class ProfileBase(DeclarativeBase):
    pass


class UserProfile(ProfileBase):
    """
    Profilo ricco per utente — un'unica riga JSON per utente.
    Più flessibile della tabella chiave-valore per dati strutturati complessi.
    """
    __tablename__ = "user_profiles"

    user_id    = Column(String, primary_key=True)
    name       = Column(String,  nullable=True)
    data       = Column(Text,    nullable=False, default="{}")  # JSON
    updated_at = Column(DateTime, default=datetime.utcnow)


async def init_profiles_db():
    if not mem.engine:
        logger.warning("Engine DB non disponibile — profiles DB non inizializzato")
        return
    async with mem.engine.begin() as conn:
        await conn.run_sync(ProfileBase.metadata.create_all)

    # Pre-carica il tuo profilo se non esiste già
    await _seed_owner_profile()
    logger.info("DB profili inizializzato ✅")


# ── PROFILO OWNER (pre-caricato) ──────────────────────────────────────────────

OWNER_PROFILE = {
    "name":         "Victor",
    "city":         "Milano",
    "style":        "molto informale, diretto, senza fronzoli",
    "response_length": "concise — vai al punto",
    "interests": [
        "gaming",
        "anime",
        "tecnologia",
        "palestra",
        "padel",
    ],
    "gaming": {
        "preferred_genres":  "tutto tranne sportivi",
        "metacritic_filter": 75,          # considera solo giochi con score >= 75
        "platforms":         [],          # aggiungere se vuoi filtrare per piattaforma
    },
    "travel": {
        "active": False,                  # per ora non interessato a viaggi
        "base_city": "Milano",
    },
    "language": "italiano",
    "manual": True,                       # profilo dichiarato manualmente
}


async def _seed_owner_profile():
    """Inserisce il tuo profilo se non esiste già nel DB."""
    if not mem.SessionLocal:
        return
    async with mem.SessionLocal() as db:
        result = await db.execute(
            select(UserProfile).where(UserProfile.user_id == str(OWNER_ID))
        )
        existing = result.scalar_one_or_none()
        if not existing:
            db.add(UserProfile(
                user_id    = str(OWNER_ID),
                name       = OWNER_PROFILE["name"],
                data       = json.dumps(OWNER_PROFILE, ensure_ascii=False),
                updated_at = datetime.utcnow(),
            ))
            await db.commit()
            logger.info(f"Profilo owner (ID {OWNER_ID}) pre-caricato ✅")


# ── CRUD ──────────────────────────────────────────────────────────────────────

async def get_profile(user_id: int) -> dict:
    """Carica il profilo di un utente. Restituisce {} se non esiste."""
    if not mem.SessionLocal:
        return {}
    try:
        async with mem.SessionLocal() as db:
            result = await db.execute(
                select(UserProfile).where(UserProfile.user_id == str(user_id))
            )
            row = result.scalar_one_or_none()
            if not row:
                return {}
            return json.loads(row.data)
    except Exception as e:
        logger.error(f"Errore lettura profilo {user_id}: {e}")
        return {}


async def save_profile(user_id: int, name: str, data: dict):
    """Salva o aggiorna il profilo completo di un utente."""
    if not mem.SessionLocal:
        return
    try:
        async with mem.SessionLocal() as db:
            result = await db.execute(
                select(UserProfile).where(UserProfile.user_id == str(user_id))
            )
            existing = result.scalar_one_or_none()
            if existing:
                existing.name       = name
                existing.data       = json.dumps(data, ensure_ascii=False)
                existing.updated_at = datetime.utcnow()
            else:
                db.add(UserProfile(
                    user_id    = str(user_id),
                    name       = name,
                    data       = json.dumps(data, ensure_ascii=False),
                    updated_at = datetime.utcnow(),
                ))
            await db.commit()
    except Exception as e:
        logger.error(f"Errore salvataggio profilo {user_id}: {e}")


async def update_profile_field(user_id: int, key: str, value):
    """Aggiorna un singolo campo del profilo senza sovrascrivere il resto."""
    profile = await get_profile(user_id)
    profile[key] = value
    name = profile.get("name", str(user_id))
    await save_profile(user_id, name, profile)


# ── AGGIORNAMENTO AUTOMATICO (altri utenti) ───────────────────────────────────

async def auto_update_profile(user_id: int, user_name: str, message: str, topic: str):
    """
    Osserva silenziosamente il messaggio dell'utente e aggiorna il profilo.
    Chiamato dopo ogni risposta — non blocca il flusso principale.
    Non tocca il profilo owner che è manuale.
    """
    if user_id == OWNER_ID:
        return  # il tuo profilo è manuale, non va toccato in automatico

    profile = await get_profile(user_id)

    # Costruisce contesto per l'LLM
    current = json.dumps(profile, ensure_ascii=False, indent=2) if profile else "{}"

    result = await call_llm(
        system="""Sei un sistema di profilazione silenzioso. Analizza il messaggio dell'utente
e aggiorna il profilo JSON con informazioni nuove che emergono implicitamente.

Esempi di cosa estrarre:
- Città menzionate → "city"
- Interessi → aggiungi a "interests" (lista)
- Stile di risposta preferito → "style"
- Lingua preferita → "language"
- Dominio principale (gaming, tech, cucina...) → "primary_domain"

Regole:
- Non inventare informazioni non presenti nel messaggio
- Non rimuovere informazioni esistenti
- Se non c'è nulla di nuovo, rispondi: NO_UPDATE
- Altrimenti rispondi SOLO con il JSON aggiornato completo""",
        messages=[{
            "role": "user",
            "content": f"Profilo attuale:\n{current}\n\nMessaggio nel topic '{topic}':\n{message}"
        }],
        max_tokens=400,
    )

    if "NO_UPDATE" in result:
        return

    try:
        clean   = result.strip().strip("```json").strip("```").strip()
        updated = json.loads(clean)
        updated["name"] = updated.get("name", user_name)
        await save_profile(user_id, updated["name"], updated)
        logger.debug(f"Profilo auto-aggiornato per {user_name} ({user_id})")
    except Exception as e:
        logger.debug(f"Auto-update profilo fallito per {user_id}: {e}")


# ── FORMATO PER PROMPT ────────────────────────────────────────────────────────

def format_profile_for_prompt(profile: dict) -> str:
    """
    Converte il profilo in testo conciso da iniettare nel system prompt degli agenti.
    Più ricco del format_memory_for_prompt esistente.
    """
    if not profile:
        return ""

    lines = ["## Profilo utente"]

    if profile.get("name"):
        lines.append(f"- Nome: {profile['name']}")
    if profile.get("city"):
        lines.append(f"- Città: {profile['city']}")
    if profile.get("style"):
        lines.append(f"- Stile preferito: {profile['style']}")
    if profile.get("response_length"):
        lines.append(f"- Risposte: {profile['response_length']}")
    if profile.get("language"):
        lines.append(f"- Lingua: {profile['language']}")

    interests = profile.get("interests", [])
    if interests:
        lines.append(f"- Interessi: {', '.join(interests[:6])}")

    # Contesto gaming specifico
    gaming = profile.get("gaming", {})
    if gaming:
        lines.append(f"- Gaming: preferisce tutto tranne sportivi")
        if gaming.get("metacritic_filter"):
            lines.append(f"- Filtro Metacritic: solo giochi con score ≥ {gaming['metacritic_filter']}")

    # Viaggi
    travel = profile.get("travel", {})
    if travel and not travel.get("active", True):
        lines.append("- Viaggi: non interessato al momento")

    return "\n".join(lines)


# ── PARSING DICHIARAZIONE MANUALE ─────────────────────────────────────────────

async def sophia_parse_profile_declaration(user_id: int, user_name: str, message: str) -> dict | None:
    """
    Sophia può chiamare questa funzione quando un utente si dichiara.
    Es: "Sophia, abito a Milano, preferisco risposte brevi, mi interessano i giochi indie"
    Restituisce il profilo aggiornato o None se non è una dichiarazione.
    """
    result = await call_llm(
        system="""Sei un parser di dichiarazioni utente. Estrai informazioni personali dal testo.
Se non è una dichiarazione su se stesso, rispondi: NON_DECLARATION
Altrimenti rispondi SOLO con JSON con i campi trovati tra:
{
  "city": "...",
  "interests": [...],
  "style": "...",
  "response_length": "...",
  "language": "...",
  "gaming": {"preferred_genres": "...", "metacritic_filter": 75},
  "travel": {"active": true/false}
}
Includi solo i campi effettivamente menzionati.""",
        messages=[{"role": "user", "content": message}],
        max_tokens=300,
    )

    if "NON_DECLARATION" in result:
        return None

    try:
        clean   = result.strip().strip("```json").strip("```").strip()
        updates = json.loads(clean)

        # Merge con profilo esistente
        profile = await get_profile(user_id)
        for key, value in updates.items():
            if isinstance(value, list) and key in profile and isinstance(profile[key], list):
                # Merge liste senza duplicati
                profile[key] = list(dict.fromkeys(profile[key] + value))
            else:
                profile[key] = value

        profile["name"]   = profile.get("name", user_name)
        profile["manual"] = True
        await save_profile(user_id, profile["name"], profile)
        return profile

    except Exception as e:
        logger.error(f"Errore parsing dichiarazione profilo: {e}")
        return None
