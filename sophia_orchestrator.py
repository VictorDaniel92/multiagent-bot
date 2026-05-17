"""
sophia_orchestrator.py — Sophia come orchestratore attivo.

Tre comportamenti proattivi:
1. METEO — dopo il briefing mattutino confronta con ieri e segnala contraddizioni
2. VIAGGIO — dopo ogni risposta di Luca/Marco valuta se c'è un collegamento utile
3. SENZA RISPOSTA — ogni 5 minuti controlla messaggi senza risposta entro 5 min
"""

import logging
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from agents import call_llm

logger    = logging.getLogger(__name__)
SOULS_DIR = Path(__file__).parent / "souls"

SOUL_SOPHIA = (SOULS_DIR / "sophia.md").read_text(encoding="utf-8") \
    if (SOULS_DIR / "sophia.md").exists() else ""

# ── Stato interno ──────────────────────────────────────────────────────────────

# Set di message_id già sollecitati da Sophia (no spam)
_already_nudged: set[int] = set()

# Ultimo briefing meteo inviato {city: summary_text}
_last_weather_briefing: dict[str, str] = {}

# Messaggi in attesa di risposta: {message_id: {text, topic, thread_id, chat_id, timestamp}}
_pending_messages: dict[int, dict] = {}


# ── 1. METEO — contraddizione con ieri ────────────────────────────────────────

def store_weather_briefing(city: str, summary: str):
    """Chiamato dopo job_morning_weather per memorizzare il briefing di oggi."""
    _last_weather_briefing[city] = summary
    logger.info(f"Briefing meteo memorizzato per {city}")


async def sophia_check_weather_contradiction(bot, group_chat_id: int) -> None:
    """
    Confronta il meteo di oggi con quello di ieri.
    Se c'è una contraddizione significativa, Sophia lo segnala in General.
    """
    if not _last_weather_briefing:
        return

    try:
        from weather_agent import fetch_weather, parse_next_hours, CITIES

        contradictions = []
        for city_name, yesterday_summary in _last_weather_briefing.items():
            # Trova le coordinate
            city_key  = city_name.lower()
            city_data = CITIES.get(city_key)
            if not city_data:
                continue

            # Meteo attuale
            data = await fetch_weather(city_data["lat"], city_data["lon"], hours=6)
            if not data:
                continue

            hours_data   = parse_next_hours(data, 6)
            current_desc = hours_data[0]["desc"] if hours_data else ""

            # Chiede all'LLM se c'è contraddizione
            check = await call_llm(
                system="""Confronta due descrizioni meteo per la stessa città.
C'è una contraddizione significativa (es: ieri sole oggi pioggia, ieri caldo oggi freddo)?
Rispondi SOLO con JSON: {"contradiction": true, "note": "spiegazione breve"} oppure {"contradiction": false}""",
                messages=[{"role": "user", "content":
                    f"Ieri: {yesterday_summary[:200]}\nOggi: {current_desc}"
                }],
                max_tokens=80,
            )

            import json, re
            try:
                result = json.loads(re.search(r'\{.*\}', check, re.DOTALL).group())
                if result.get("contradiction"):
                    contradictions.append({
                        "city": city_data["name"],
                        "note": result.get("note", ""),
                        "current": current_desc,
                    })
            except Exception:
                pass

        if not contradictions:
            logger.info("Nessuna contraddizione meteo rilevata")
            return

        # Sophia scrive in General
        contradiction_text = "\n".join(
            f"- {c['city']}: {c['note']}" for c in contradictions
        )

        msg = await call_llm(
            system=f"""{SOUL_SOPHIA}

Hai notato che le previsioni meteo di oggi contraddicono quelle di ieri per alcune città.
Scrivi un messaggio breve e leggero per il gruppo General che lo fa notare,
suggerendo di controllare il topic Meteo per gli aggiornamenti.
Max 3 righe. Tono curioso, non allarmista. Usa Markdown Telegram.""",
            messages=[{"role": "user", "content":
                f"Contraddizioni rilevate:\n{contradiction_text}"
            }],
            max_tokens=150,
        )

        await bot.send_message(
            chat_id=group_chat_id,
            text=msg,
            parse_mode="Markdown",
        )
        logger.info("Sophia ha segnalato contraddizione meteo ✅")

    except Exception as e:
        logger.error(f"Errore check contraddizione meteo: {e}", exc_info=True)


# ── 2. VIAGGIO — collegamento cross-agente ─────────────────────────────────────

async def sophia_check_cross_agent_link(
    bot,
    group_chat_id: int,
    source_agent: str,
    source_topic: str,
    question: str,
    answer: str,
    viaggi_topic_id: int = 209,
    news_topic_id:   int = 57,
) -> None:
    """
    Dopo una risposta di Luca o Marco, Sophia valuta se c'è un collegamento
    utile con un altro agente e lo segnala nel topic appropriato.

    Es: Luca parla di un gioco ambientato a Tokyo → Marco potrebbe suggerire Tokyo come meta
    Es: Marco pianifica un trip a Milano → Giorgio potrebbe dare il meteo
    """
    try:
        # Chiede all'LLM se c'è un collegamento interessante
        other_agent = "marco" if source_agent == "luca" else "luca"
        other_desc  = {
            "marco": "agente viaggi che suggerisce itinerari e mete",
            "luca":  "agente gaming che parla di videogiochi e industria",
        }[other_agent]

        check = await call_llm(
            system=f"""Valuta se la risposta di un agente contiene un collegamento interessante
per un altro agente specializzato.
Agente sorgente: {source_agent}
Altro agente: {other_agent} ({other_desc})

Rispondi SOLO con JSON:
{{"link": true, "reason": "spiegazione breve del collegamento"}}
oppure {{"link": false}}

Sii selettivo — segnala solo collegamenti genuinamente utili e non forzati.""",
            messages=[{"role": "user", "content":
                f"Domanda: {question}\nRisposta: {answer[:300]}"
            }],
            max_tokens=100,
        )

        import json, re
        try:
            result = json.loads(re.search(r'\{.*\}', check, re.DOTALL).group())
        except Exception:
            return

        if not result.get("link"):
            return

        reason = result.get("reason", "")
        logger.info(f"Sophia rileva collegamento {source_agent}→{other_agent}: {reason}")

        # Sophia scrive nel topic dell'altro agente
        target_topic_id = viaggi_topic_id if other_agent == "marco" else news_topic_id

        msg = await call_llm(
            system=f"""{SOUL_SOPHIA}

Hai notato un collegamento interessante tra una risposta di {source_agent.capitalize()}
e quello che potrebbe fare {other_agent.capitalize()}.
Scrivi un messaggio breve e curioso nel topic di {other_agent.capitalize()} per segnalarlo.
Max 2 righe. Tono leggero, come una nota tra colleghi. Usa Markdown Telegram.""",
            messages=[{"role": "user", "content":
                f"Collegamento: {reason}\n"
                f"Contesto originale: {question[:100]}"
            }],
            max_tokens=120,
        )

        await bot.send_message(
            chat_id=group_chat_id,
            message_thread_id=target_topic_id,
            text=msg,
            parse_mode="Markdown",
        )
        logger.info(f"Sophia ha segnalato collegamento cross-agente ✅")

    except Exception as e:
        logger.error(f"Errore check cross-agent: {e}", exc_info=True)


# ── 3. MESSAGGI SENZA RISPOSTA ─────────────────────────────────────────────────

def track_incoming_message(message_id: int, text: str, topic: str,
                           thread_id: int | None, chat_id: int) -> None:
    """
    Registra un messaggio in arrivo come "in attesa di risposta".
    Chiamato in handle_message PRIMA che l'agente risponda.
    """
    _pending_messages[message_id] = {
        "text":      text,
        "topic":     topic,
        "thread_id": thread_id,
        "chat_id":   chat_id,
        "timestamp": datetime.utcnow(),
    }


def mark_message_answered(message_id: int) -> None:
    """
    Marca un messaggio come risposto.
    Chiamato dopo che l'agente ha risposto con successo.
    """
    _pending_messages.pop(message_id, None)


async def sophia_check_unanswered(bot, group_chat_id: int) -> None:
    """
    Controlla se ci sono messaggi rimasti senza risposta per più di 5 minuti.
    Sophia interviene UNA SOLA VOLTA per messaggio (tracciato in _already_nudged).
    """
    now     = datetime.utcnow()
    cutoff  = now - timedelta(minutes=5)

    for message_id, info in list(_pending_messages.items()):
        # Già sollecitato
        if message_id in _already_nudged:
            _pending_messages.pop(message_id, None)  # pulizia
            continue

        # Non ancora scaduto
        if info["timestamp"] > cutoff:
            continue

        # Troppo vecchio (>30 min) — non ha più senso
        if info["timestamp"] < now - timedelta(minutes=30):
            _pending_messages.pop(message_id, None)
            continue

        # Segnala
        try:
            _already_nudged.add(message_id)
            _pending_messages.pop(message_id, None)

            topic_label = info["topic"].capitalize()

            msg = await call_llm(
                system=f"""{SOUL_SOPHIA}

Qualcuno ha scritto nel topic {topic_label} ma nessun agente ha ancora risposto.
Scrivi un messaggio breve in General per far notare che c'è una domanda in attesa.
Max 2 righe. Tono leggero, non drammatico. Usa Markdown Telegram.
Non citare il messaggio originale per intero.""",
                messages=[{"role": "user", "content":
                    f"Topic: {topic_label}\n"
                    f"Domanda senza risposta: {info['text'][:120]}"
                }],
                max_tokens=100,
            )

            await bot.send_message(
                chat_id=group_chat_id,
                text=msg,
                parse_mode="Markdown",
            )
            logger.info(f"Sophia ha segnalato messaggio senza risposta (id={message_id}) ✅")

        except Exception as e:
            logger.error(f"Errore segnalazione messaggio senza risposta: {e}")
