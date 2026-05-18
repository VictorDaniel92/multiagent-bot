import os
import re
import uuid
import asyncio
import logging
import datetime
from telegram import Update, BotCommand, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    ChatMemberHandler, CallbackQueryHandler, filters, ContextTypes
)
from telegram.constants import ParseMode, ChatAction

from agents import run_pipeline
from memory import init_db, get_memory, set_memory, update_topics, format_memory_for_prompt
from session_memory import (
    init_episode_db, get_or_create_session,
    add_user_message, add_agent_message,
    get_session_context, contextual_guard,
    get_user_episode_pattern, get_today_narrative,
)
from mentor_agent import (
    mentor_analyze_agent, mentor_analyze_all,
    format_analysis_for_telegram, ANALYZABLE_AGENTS,
    mentor_extract_proposed_soul, apply_soul_change,
    reload_agent_soul, restore_soul_backup,
    init_mentor_db, save_mentor_proposal,
    mark_proposal_applied, mark_proposal_rejected,
)
from news_agent import (
    fetch_new_multiplayer_news, fetch_recent_news,
    luca_comment_news, luca_daily_digest, luca_summarize_recent,
    format_news_message, get_yesterday_news, mark_seen, init_news_db,
    luca_answer_question,
)
from weather_agent import (
    extract_city, giorgio_forecast, giorgio_morning_briefing, MORNING_CITIES
)
from marco_agent import marco_answer_question
from memory_vector import (
    init_vector_db, save_conversation,
    search_memories, format_memories_for_prompt,
    get_recent_conversations,
)
from webhook_server import start_webhook_server, set_bot_app
from sophia_orchestrator import (
    store_weather_briefing, sophia_check_weather_contradiction,
    sophia_check_cross_agent_link, sophia_check_unanswered,
    track_incoming_message, mark_message_answered,
)
from sophia_agent import (
    sophia_is_mentioned, sophia_route_request, sophia_format_agent_message,
    sophia_welcome, sophia_daily_recap, sophia_agent_status,
    sophia_log_activity, sophia_get_activity, sophia_reset_activity,
    sophia_parse_reminder, sophia_add_reminder, sophia_get_due_reminders,
)
from user_profiles import (
    init_profiles_db, get_profile, auto_update_profile,
    format_profile_for_prompt, sophia_parse_profile_declaration,
)
from agent_state import (
    init_agent_state_db, get_agent_state, save_agent_state,
    build_agent_context, update_perspective, update_mood,
    increment_interaction, extract_goals_from_message, add_goal,
    set_shared_state, mood_label,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]

# ── Topic map ──────────────────────────────────────────────────────────────────
TOPIC_MAP = {
    None: "general",   # General → Sophia receptionist
    2:    "ricerca",
    4:    "coding",
    6:    "brainstorming",
    8:    "analisi",
    57:   "news",      # Topic News    (Luca)
    99:   "meteo",     # Topic Meteo   (Giorgio)
    209:  "viaggi",    # Topic Viaggi  (Marco)
}

GROUP_CHAT_ID:  int      = int(os.environ.get("GROUP_CHAT_ID", "0"))
NEWS_TOPIC_ID:  int|None = int(os.environ.get("NEWS_TOPIC_ID",  "57")) or None
METEO_TOPIC_ID: int|None = int(os.environ.get("METEO_TOPIC_ID", "99")) or None

TOPIC_EMOJI = {
    "general":       "🌸",
    "ricerca":       "🔍",
    "coding":        "💻",
    "brainstorming": "🧠",
    "analisi":       "📊",
    "news":          "🎮",
    "meteo":         "🌤️",
    "viaggi":        "🗺️",
}


# ── Utils ──────────────────────────────────────────────────────────────────────

def get_topic(update: Update) -> str:
    thread_id = getattr(update.message, "message_thread_id", None)
    topic = TOPIC_MAP.get(thread_id, "ricerca")
    logger.info(f"[MSG] thread_id={thread_id} → topic={topic} | user_id={update.effective_user.id}")
    return topic


# ── Handlers ───────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id   = update.effective_user.id
    user_name = update.effective_user.first_name
    await set_memory(user_id, "name", user_name)
    await update.message.reply_text(
        f"👋 Ciao {user_name}!\n\n"
        f"In *General* trovi *Sophia* 🌸 — la receptionist del gruppo.\n"
        f"Menzionala e lei ti smista dall'agente giusto!\n\n"
        f"*Agenti specializzati:*\n"
        f"🎮 *Luca* — Gaming & News (topic News)\n"
        f"🌤️ *Giorgio* — Meteo (topic Meteo)\n"
        f"🗺️ *Marco* — Viaggi & Itinerari (topic Viaggi)\n"
        f"🔍 *Max+Sofia+Alex* — Ricerca, Coding, Brainstorming, Analisi\n\n"
        f"*Comandi:*\n"
        f"/agenti — chi c'è nel gruppo\n"
        f"/memoria — cosa ricordo di te\n"
        f"/dietro — toggle ragionamento interno\n"
        f"/nota [testo] — aggiungi una nota su di te\n"
        f"/news — ultime notizie gaming",
        parse_mode=ParseMode.MARKDOWN
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return

    question  = message.text.strip()
    user_id   = update.effective_user.id
    user_name = update.effective_user.first_name or "utente"
    chat_id   = update.effective_chat.id
    topic     = get_topic(update)
    thread_id = getattr(message, "message_thread_id", None)

    # ── GESTIONE MODIFICA SOUL (risposta al flusso ✏️ Modifica del Mentor) ──
    pending_edit = context.user_data.get("pending_mentor_edit")
    if pending_edit:
        if question.strip().lower() == "/annulla":
            context.user_data.pop("pending_mentor_edit", None)
            await message.reply_text("❌ Modifica annullata. Il soul rimane invariato.")
            return

        agent_name   = pending_edit["agent"]
        proposal_id  = pending_edit["proposal_id"]
        new_soul     = question.strip()

        ok_write  = apply_soul_change(agent_name, new_soul)
        ok_reload = reload_agent_soul(agent_name) if ok_write else False

        context.user_data.pop("pending_mentor_edit", None)
        context.application.bot_data.get("mentor_proposals", {}).pop(proposal_id, None)

        if ok_write and ok_reload:
            await message.reply_text(
                f"✅ *Soul di {agent_name.capitalize()} aggiornato con la tua versione e ricaricato.*",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await message.reply_text("⚠️ Errore durante la scrittura. Riprova.")
        return
    # ─────────────────────────────────────────────────────────────────────────

    # Logga attività per il recap serale
    sophia_log_activity(topic)

    # Traccia il messaggio come "in attesa di risposta" (per orchestratore)
    if topic != "general":
        track_incoming_message(
            message.message_id, question, topic, thread_id, chat_id
        )

    # ── GENERAL — Solo Sophia, solo se menzionata ──────────────────────────
    if topic == "general":
        if not sophia_is_mentioned(question):
            return  # Sophia non si intromette mai

        # Pulisce la menzione dalla domanda
        clean_question = re.sub(r'\b(sophia|sofia)[,:]?\s*', '', question, flags=re.IGNORECASE).strip()
        if not clean_question:
            await message.reply_text("Dimmi tutto! 😊 Come posso aiutarti?")
            return

        # Controlla se è un promemoria
        if re.search(r'\bricordami\b', clean_question, re.IGNORECASE):
            reminder_data = await sophia_parse_reminder(clean_question)
            if reminder_data:
                try:
                    when = datetime.datetime.fromisoformat(reminder_data["when_iso"])
                    sophia_add_reminder(user_id, chat_id, thread_id, when, reminder_data["what"])
                    await message.reply_text(
                        f"✅ Fatto! Ti ricorderò di *{reminder_data['what']}* {reminder_data['when_str']} 🗓",
                        parse_mode=ParseMode.MARKDOWN
                    )
                except Exception as e:
                    logger.error(f"Errore promemoria: {e}")
                    await message.reply_text(
                        "Non sono riuscita a capire la data. Prova con: "
                        "_'ricordami domani alle 9 di...'_",
                        parse_mode=ParseMode.MARKDOWN
                    )
            else:
                await message.reply_text(
                    "Non ho capito bene quando vuoi il promemoria. "
                    "Prova così: _'Sophia ricordami domani alle 9 di controllare il meteo'_",
                    parse_mode=ParseMode.MARKDOWN
                )
            return

        # Controlla se è una dichiarazione di profilo ("Sophia, abito a Milano, mi piace il gaming")
        profile_triggers = r'\b(abito|vivo|sono di|mi chiamo|mi piace|preferisco|mi interessano|gioco a|lavoro a)\b'
        if re.search(profile_triggers, clean_question, re.IGNORECASE):
            updated = await sophia_parse_profile_declaration(user_id, user_name, clean_question)
            if updated:
                city      = updated.get("city", "")
                interests = ", ".join(updated.get("interests", [])[:4])
                details   = f"città: {city}" if city else ""
                if interests:
                    details += f", interessi: {interests}" if details else f"interessi: {interests}"
                await message.reply_text(
                    f"🌸 Grazie {user_name}! Ho aggiornato il tuo profilo"
                    f"{(' — ' + details) if details else ''}.\n"
                    f"Gli agenti useranno queste informazioni per personalizzare le risposte.",
                    parse_mode=ParseMode.MARKDOWN
                )
                return

        # Controlla se è una domanda sulla memoria del gruppo
        memory_triggers = r'\b(si è parlato|hai detto|ricordi|ha detto|parlato di|discusso|ieri|stamattina|oggi|recente|ultima volta)\b'
        if re.search(memory_triggers, clean_question, re.IGNORECASE):
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            from sophia_agent import sophia_answer_with_memory
            answer = await sophia_answer_with_memory(clean_question)
            await message.reply_text(answer, parse_mode=ParseMode.MARKDOWN)
            return

        # Routing intelligente
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        routing = await sophia_route_request(clean_question)

        # Risponde in General
        await message.reply_text(routing["sophia_reply"], parse_mode=ParseMode.MARKDOWN)

        # Se ha trovato un agente, lo chiama direttamente e manda la risposta nel topic
        if routing["agent"] and routing["topic_id"] and GROUP_CHAT_ID:
            agent      = routing["agent"]
            topic_id   = routing["topic_id"]
            sub_q      = routing["rephrased_question"]
            intro      = sophia_format_agent_message(agent, sub_q, user_name)

            try:
                # Annuncio nel topic ("Sophia gira la domanda a...")
                await context.bot.send_message(
                    chat_id=GROUP_CHAT_ID,
                    message_thread_id=topic_id,
                    text=intro,
                    parse_mode=ParseMode.MARKDOWN,
                )

                # Esegue l'agente reale e posta la risposta nello stesso topic
                await context.bot.send_chat_action(chat_id=GROUP_CHAT_ID, action=ChatAction.TYPING)

                if agent == "giorgio":
                    city = await extract_city(sub_q)
                    logger.info(f"[Sophia→Giorgio] sub_q={sub_q!r} city={city}")
                    if city:
                        try:
                            answer = await giorgio_forecast(city, hours=8)
                        except Exception as geo_err:
                            logger.error(f"[giorgio_forecast] {geo_err}", exc_info=True)
                            answer = f"⚠️ Errore meteo: `{geo_err}`"
                    else:
                        answer = "🌍 Non ho trovato la città nella domanda."
                    await context.bot.send_message(
                        chat_id=GROUP_CHAT_ID,
                        message_thread_id=topic_id,
                        text=answer,
                        parse_mode=ParseMode.MARKDOWN,
                    )

                elif agent == "luca":
                    answer = await luca_answer_question(sub_q)
                    await context.bot.send_message(
                        chat_id=GROUP_CHAT_ID,
                        message_thread_id=topic_id,
                        text=answer,
                        parse_mode=ParseMode.MARKDOWN,
                    )

                elif agent == "marco":
                    answer = await marco_answer_question(sub_q)
                    await context.bot.send_message(
                        chat_id=GROUP_CHAT_ID,
                        message_thread_id=topic_id,
                        text=answer,
                        parse_mode=ParseMode.MARKDOWN,
                    )

                logger.info(f"Sophia → {agent} (topic {topic_id}) ✅")

            except Exception as e:
                logger.error(f"Errore Sophia→{agent}: {e}", exc_info=True)

        return

    emoji = TOPIC_EMOJI.get(topic, "💬")

    # ── NEWS (Luca) ────────────────────────────────────────────────────────
    if topic == "news":
        # Guard contestuale — passa il contesto della sessione
        session_ctx  = await get_session_context(user_id, "news", max_messages=3)
        guard_result = await contextual_guard(
            question, "news", user_id,
            "videogiochi, gaming, industria videoludica"
        )
        if not guard_result["ok"]:
            await message.reply_text(
                "🎮 Questo topic è dedicato ai *videogiochi* e all'industria gaming.\n\n"
                "Per altre domande usa il topic giusto:\n"
                "🔍 Ricerca · 💻 Coding · 🧠 Brainstorming · 📊 Analisi",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        await add_user_message(user_id, "news", question)
        status_msg = await message.reply_text("🎮 *Luca* sta pensando...", parse_mode=ParseMode.MARKDOWN)
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            profile     = await get_profile(user_id)
            profile_ctx = format_profile_for_prompt(profile)

            # Contesto di stato di Luca
            luca_ctx = await build_agent_context("luca", user_id)
            new_goals = await extract_goals_from_message("luca", question)
            for goal in new_goals:
                await add_goal("luca", user_id, goal)

            answer = await luca_answer_question(
                question, profile_context=profile_ctx + "\n\n" + luca_ctx
            )
            await status_msg.delete()
            await message.reply_text(answer, parse_mode=ParseMode.MARKDOWN)
            mark_message_answered(message.message_id)
            await save_conversation(user_id=user_id, topic="news", agent="luca",
                                    question=question, answer=answer)
            await add_agent_message(user_id, "news", answer[:300], agent="luca")

            # Aggiorna stato Luca (silenzioso)
            async def _update_luca_state():
                await increment_interaction("luca", user_id)
                state = await get_agent_state("luca", user_id)
                new_perspective = await update_perspective(
                    "luca", user_id, state["perspective"],
                    f"D: {question[:200]}\nR: {answer[:300]}",
                )
                await save_agent_state("luca", user_id, perspective=new_perspective)
                await set_shared_state("luca",
                    current_focus="notizie gaming / multiplayer.it",
                    recent_insight=answer[:120],
                )
                await update_mood("luca", user_id, delta=+0.02)
            asyncio.ensure_future(_update_luca_state())

            if os.environ.get("ENABLE_CROSS_AGENT", "false").lower() == "true":
                asyncio.create_task(sophia_check_cross_agent_link(
                    context.bot, GROUP_CHAT_ID, "luca", "news", question, answer
                ))
        except Exception as e:
            logger.error(f"Errore Luca: {e}", exc_info=True)
            await status_msg.edit_text("⚠️ Errore. Riprova tra qualche secondo.")
        return

    # ── METEO (Giorgio) ────────────────────────────────────────────────────
    if topic == "meteo":
        guard_result = await contextual_guard(
            question, "meteo", user_id,
            "meteo, tempo atmosferico, previsioni, temperatura, pioggia"
        )
        if not guard_result["ok"]:
            await message.reply_text(
                "🌤️ Questo topic è dedicato al *meteo*.\n\n"
                "Chiedimi il tempo di una città, es: _\"che tempo fa a Roma?\"_\n\n"
                "Per altre domande usa il topic giusto.",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        await add_user_message(user_id, "meteo", question)
        status_msg = await message.reply_text("🌤️ *Giorgio* sta controllando le previsioni...", parse_mode=ParseMode.MARKDOWN)
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            city = await extract_city(question)
            if not city:
                await status_msg.delete()
                await message.reply_text(
                    "🌍 Non ho trovato la città. Prova con: _\"che tempo fa a Milano?\"_",
                    parse_mode=ParseMode.MARKDOWN
                )
                return
            answer = await giorgio_forecast(city, hours=8)
            await status_msg.delete()
            await message.reply_text(answer, parse_mode=ParseMode.MARKDOWN)
            mark_message_answered(message.message_id)
            await save_conversation(user_id=user_id, topic="meteo", agent="giorgio",
                                    question=question, answer=answer)
            await add_agent_message(user_id, "meteo", answer[:300], agent="giorgio")
        except Exception as e:
            logger.error(f"Errore Giorgio: {e}", exc_info=True)
            await status_msg.edit_text("⚠️ Errore meteo. Riprova tra qualche secondo.")
        return

    # ── VIAGGI (Marco) ─────────────────────────────────────────────────────
    if topic == "viaggi":
        from agents import call_llm
        import json as _json
        guard_result = await call_llm(
            system='Classificatore. La domanda riguarda viaggi, mete, itinerari, cosa fare/vedere/mangiare in una città? Rispondi SOLO con JSON: {"ok": true} oppure {"ok": false}',
            messages=[{"role": "user", "content": f"Domanda: {question}"}],
            max_tokens=20
        )
        try:
            ok = _json.loads(guard_result.strip()).get("ok", True)
        except Exception:
            ok = True

        if not ok:
            await message.reply_text(
                "🗺️ Questo topic è dedicato ai *viaggi*!\n\n"
                "Chiedimi itinerari, mete, cosa vedere o mangiare in una città.\n"
                "Per altre domande usa il topic giusto.",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        status_msg = await message.reply_text(
            "🗺️ *Marco* sta costruendo il tuo itinerario...",
            parse_mode=ParseMode.MARKDOWN
        )
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            answer = await marco_answer_question(question)
            await status_msg.delete()
            await message.reply_text(answer, parse_mode=ParseMode.MARKDOWN)
            mark_message_answered(message.message_id)
            await save_conversation(user_id=user_id, topic="viaggi", agent="marco",
                                    question=question, answer=answer)
            if os.environ.get("ENABLE_CROSS_AGENT", "false").lower() == "true":
                asyncio.create_task(sophia_check_cross_agent_link(
                    context.bot, GROUP_CHAT_ID, "marco", "viaggi", question, answer
                ))
        except Exception as e:
            logger.error(f"Errore Marco: {e}", exc_info=True)
            await status_msg.edit_text("⚠️ Errore. Riprova tra qualche secondo.")
        return
    from agents import topic_guard

    # Recupera il contesto della sessione per questo utente+topic
    session_ctx = await get_session_context(user_id, topic, max_messages=3)

    guard = await topic_guard(question, topic, session_context=session_ctx)
    if not guard["match"]:
        suggested_emoji = TOPIC_EMOJI.get(guard["suggested"], "💬")
        await message.reply_text(
            f"⚠️ Questa domanda non è nel topic giusto!\n\n"
            f"Sei in {emoji} *{topic}*, ma sembra più adatta a "
            f"{suggested_emoji} *{guard['suggested']}*.\n\n"
            f"_{guard['reason']}_\n\n"
            f"Scrivi lì la stessa domanda 🙂",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # Traccia il messaggio utente nella sessione episodica
    await add_user_message(user_id, topic, question)

    # ── PIPELINE GENERALE ─────────────────────────────────────────────────
    # Carica profilo ricco — sostituisce il vecchio format_memory_for_prompt
    profile        = await get_profile(user_id)
    memory_context = format_profile_for_prompt(profile)

    # Integra con ricordi semantici (vector memory)
    past_memories = await search_memories(question, user_id=user_id, topic=topic, limit=3)
    if past_memories:
        memory_context += "\n\n" + format_memories_for_prompt(past_memories)

    # Integra con pattern episodici (comportamento a lungo termine)
    episode_pattern = await get_user_episode_pattern(user_id, days=14)
    if episode_pattern:
        memory_context += f"\n\n## Pattern comportamentali recenti\n{episode_pattern}"

    # ── STATO AGENTE ──────────────────────────────────────────────────────
    agent_context = await build_agent_context("alex", user_id)
    # Estrai eventuali obiettivi dal messaggio e salvali
    new_goals = await extract_goals_from_message("alex", question)
    for goal in new_goals:
        await add_goal("alex", user_id, goal)
    # ─────────────────────────────────────────────────────────────────────

    status_msg = await message.reply_text(f"⏳ Avvio pipeline {emoji}...")

    try:
        result = await run_pipeline_with_updates(
            question, topic, memory_context, status_msg, context.bot, chat_id,
            agent_context=agent_context,
        )
        await status_msg.delete()

        answer = result["answer"]
        if len(answer) > 4000:
            answer = answer[:4000] + "\n\n_(risposta troncata)_"
        await message.reply_text(answer)

        if result["queries"]:
            await update_topics(user_id, result["queries"][:2])

        # Salva la conversazione nel vector store
        await save_conversation(
            user_id=user_id,
            topic=topic,
            agent="alex",
            question=question,
            answer=result["answer"],
        )

        # Traccia la risposta nella sessione episodica
        await add_agent_message(user_id, topic, result["answer"][:300], agent="alex")

        # ── AGGIORNA STATO AGENTE ─────────────────────────────────────────
        import asyncio as _asyncio
        async def _update_alex_state():
            # Incrementa contatore
            await increment_interaction("alex", user_id)
            # Aggiorna prospettiva sull'utente
            state = await get_agent_state("alex", user_id)
            new_perspective = await update_perspective(
                "alex", user_id,
                state["perspective"],
                f"D: {question[:200]}\nR: {result['answer'][:300]}",
            )
            await save_agent_state("alex", user_id, perspective=new_perspective)
            # Aggiorna stato condiviso
            await set_shared_state(
                "alex",
                current_focus=topic,
                recent_insight=result["answer"][:120],
            )
            # Mood: leggero boost per ogni interazione completata
            await update_mood("alex", user_id, delta=+0.02)

        _asyncio.ensure_future(_update_alex_state())
        # ─────────────────────────────────────────────────────────────────

        # Aggiornamento automatico profilo (silenzioso, non blocca)
        _asyncio.ensure_future(
            auto_update_profile(user_id, update.effective_user.first_name, question, topic)
        )

        if context.user_data.get("show_behind") and result["queries"]:
            await message.reply_text(build_behind_scenes(result), parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        logger.error(f"Errore pipeline: {e}", exc_info=True)
        await status_msg.edit_text("⚠️ Errore. Riprova tra qualche secondo.")


async def run_pipeline_with_updates(question, topic, memory_context, status_msg, bot, chat_id, agent_context=""):
    from agents import max_plan, sofia_synthesize, alex_answer

    await status_msg.edit_text("🎯 *Max* sta pianificando...", parse_mode=ParseMode.MARKDOWN)
    await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    queries = await max_plan(question, topic, memory_context)

    if queries:
        preview = ", ".join(f'"{q}"' for q in queries[:2])
        await status_msg.edit_text(f"🔍 *Sofia* sta cercando: {preview}...", parse_mode=ParseMode.MARKDOWN)
        await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        briefing = await sofia_synthesize(question, queries, topic, memory_context)
    else:
        briefing = "Nessuna ricerca — risposta dalla conoscenza interna."

    await status_msg.edit_text("✍️ *Alex* sta scrivendo...", parse_mode=ParseMode.MARKDOWN)
    await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    answer = await alex_answer(question, queries, briefing, topic, memory_context, agent_context)

    return {"queries": queries, "briefing": briefing, "answer": answer, "topic": topic}


def build_behind_scenes(result):
    lines = [f"🔬 *Dietro le quinte* [{result['topic']}]\n"]
    if result["queries"]:
        lines.append("🎯 *Max ha pianificato:*")
        for q in result["queries"]:
            lines.append(f"  • `{q}`")
        lines.append("")
    lines.append("🔍 *Sofia ha sintetizzato:*")
    briefing_short = result["briefing"][:500] + ("..." if len(result["briefing"]) > 500 else "")
    lines.append(briefing_short)
    return "\n".join(lines)


# ── Benvenuto nuovo membro ─────────────────────────────────────────────────────

async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result: ChatMemberUpdated = update.chat_member
    if not result:
        return
    old_status = result.old_chat_member.status
    new_status = result.new_chat_member.status
    if old_status in ("left", "kicked") and new_status in ("member", "restricted"):
        new_member_name = result.new_chat_member.user.first_name or "nuovo arrivato"
        try:
            welcome_msg = await sophia_welcome(new_member_name)
            await context.bot.send_message(
                chat_id=result.chat.id,
                text=welcome_msg,
                parse_mode=ParseMode.MARKDOWN,
            )
            logger.info(f"Sophia ha accolto {new_member_name}")
        except Exception as e:
            logger.error(f"Errore benvenuto: {e}")


# ── Comandi ────────────────────────────────────────────────────────────────────

async def cmd_memoria(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    memory  = await get_memory(user_id)
    if not memory:
        await update.message.reply_text("Non ricordo ancora nulla di te. Inizia a chattare!")
        return
    lines = ["🧠 *Cosa ricordo di te:*\n"]
    for key, value in memory.items():
        if isinstance(value, list):
            value = ", ".join(value)
        lines.append(f"• *{key}*: {value}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_profilo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra il profilo ricco dell'utente."""
    user_id = update.effective_user.id
    profile = await get_profile(user_id)
    if not profile:
        await update.message.reply_text(
            "Non ho ancora un profilo per te.\n\n"
            "Puoi dichiararlo in General: _'Sophia, abito a Milano, mi piace il gaming'_\n"
            "Oppure verrà costruito automaticamente nel tempo.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    lines = ["👤 *Il tuo profilo:*\n"]
    if profile.get("name"):        lines.append(f"• Nome: {profile['name']}")
    if profile.get("city"):        lines.append(f"• Città: {profile['city']}")
    if profile.get("style"):       lines.append(f"• Stile: {profile['style']}")
    if profile.get("response_length"): lines.append(f"• Risposte: {profile['response_length']}")
    interests = profile.get("interests", [])
    if interests:                  lines.append(f"• Interessi: {', '.join(interests)}")
    gaming = profile.get("gaming", {})
    if gaming.get("metacritic_filter"):
        lines.append(f"• Filtro Metacritic: ≥ {gaming['metacritic_filter']}")
    if gaming.get("preferred_genres"):
        lines.append(f"• Generi gaming: {gaming['preferred_genres']}")
    travel = profile.get("travel", {})
    if travel:
        active = "sì" if travel.get("active", True) else "no"
        lines.append(f"• Viaggi attivi: {active}")
    if profile.get("manual"):
        lines.append("\n_Profilo dichiarato manualmente ✓_")
    else:
        lines.append("\n_Profilo costruito automaticamente_")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_oggi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sophia ricostruisce la sequenza narrativa delle conversazioni di oggi."""
    user_id    = update.effective_user.id
    status_msg = await update.message.reply_text("🌸 *Sophia* sta ricostruendo la tua giornata...", parse_mode=ParseMode.MARKDOWN)
    try:
        narrative = await get_today_narrative(user_id)
        await status_msg.delete()
        await update.message.reply_text(narrative, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Errore /oggi: {e}")
        await status_msg.edit_text("⚠️ Errore nel recupero delle conversazioni.")


async def cmd_nota(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    nota    = " ".join(context.args) if context.args else ""
    if not nota:
        await update.message.reply_text("Uso: /nota [testo]\nEsempio: /nota preferisco risposte brevi")
        return
    await set_memory(user_id, "notes", nota)
    await update.message.reply_text(f"✅ Nota salvata: _{nota}_", parse_mode=ParseMode.MARKDOWN)


async def cmd_agenti(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = get_topic(update)
    if topic == "general":
        # In General risponde Sophia
        await update.message.reply_text(sophia_agent_status(), parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(
            "🤖 *Gli agenti del sistema*\n\n"
            "🌸 *Sophia* — Receptionist (General)\n"
            "Ti smista verso l'agente giusto.\n\n"
            "🎮 *Luca* — Gaming & News (topic News)\n"
            "🌤️ *Giorgio* — Meteo (topic Meteo)\n"
            "🗺️ *Marco* — Viaggi & Itinerari (topic Viaggi)\n\n"
            "Nel gruppo ricerca/analisi:\n"
            "🎯 *Max* · 🔍 *Sofia* · ✍️ *Alex*",
            parse_mode=ParseMode.MARKDOWN
        )


def _mentor_keyboard(agent_name: str, proposal_id: str) -> InlineKeyboardMarkup:
    """Tastiera inline per approvare/modificare/rifiutare una proposta del Mentor."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Applica",  callback_data=f"mentor_apply:{agent_name}:{proposal_id}"),
        InlineKeyboardButton("✏️ Modifica", callback_data=f"mentor_edit:{agent_name}:{proposal_id}"),
        InlineKeyboardButton("❌ Rifiuta",  callback_data=f"mentor_reject:{agent_name}:{proposal_id}"),
    ]])


async def _run_mentor_for_agent(
    agent_name: str, limit: int,
    reply_fn,           # coroutine per inviare il messaggio di analisi
    bot_data: dict,
) -> None:
    """
    Analizza un agente, estrae la proposta, la salva in bot_data
    e invia il report con i bottoni inline.
    """
    from agents import load_soul

    report = await mentor_analyze_agent(agent_name, limit=limit)
    analysis_text = format_analysis_for_telegram(report)

    # Estrae la proposta di soul completo
    current_soul  = load_soul(agent_name)
    proposed_soul = await mentor_extract_proposed_soul(
        agent_name, report["analysis"], current_soul
    )

    # Salva la proposta in bot_data con ID univoco
    proposal_id = uuid.uuid4().hex[:8]
    bot_data.setdefault("mentor_proposals", {})[proposal_id] = {
        "agent":         agent_name,
        "proposed_soul": proposed_soul,
        "analysis":      report["analysis"],
    }

    # Salva nel DB per storico permanente (anti-rollback)
    summary = report["analysis"][:200].replace("\n", " ")
    diff    = proposed_soul[:150].replace("\n", " ")
    await save_mentor_proposal(proposal_id, agent_name, summary, diff)

    keyboard = _mentor_keyboard(agent_name, proposal_id)
    await reply_fn(
        text=analysis_text + f"\n\n_ID proposta: `{proposal_id}`_",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )


async def cmd_mentor_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/mentor_debug — mostra quante conversazioni ci sono nel DB per agente."""
    from memory_vector import _pool
    if not _pool:
        await update.message.reply_text("❌ Vector DB non disponibile.")
        return
    try:
        async with _pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT agent, COUNT(*) as n, MAX(created_at) as last
                FROM conversations
                GROUP BY agent
                ORDER BY n DESC
            """)
        if not rows:
            await update.message.reply_text("📭 Tabella `conversations` vuota.")
            return
        lines = ["📊 *Conversazioni nel DB per agente:*\n"]
        for r in rows:
            lines.append(f"• `{r['agent']}`: {r['n']} conversazioni (ultima: {str(r['last'])[:16]})")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"❌ Errore query: {e}")


async def cmd_mentor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /mentor        → analizza l'agente del topic in cui viene scritto
    /mentor alex   → forza un agente specifico
    /mentor alex 30 → agente specifico con N conversazioni
    """
    # Mappa topic → agente di default
    TOPIC_TO_AGENT = {
        "news":          "luca",
        "meteo":         "giorgio",
        "viaggi":        "marco",
        "ricerca":       "alex",
        "coding":        "alex",
        "brainstorming": "alex",
        "analisi":       "alex",
        "default":       "alex",
    }

    args       = context.args or []
    agent_name = args[0].lower() if args else None
    limit      = int(args[1]) if len(args) > 1 and args[1].isdigit() else 20

    # Se non specificato, prendi l'agente del topic corrente
    if not agent_name:
        topic      = get_topic(update)
        agent_name = TOPIC_TO_AGENT.get(topic, "alex")

    if agent_name not in ANALYZABLE_AGENTS:
        nomi = ", ".join(f"`{a}`" for a in ANALYZABLE_AGENTS)
        await update.message.reply_text(
            f"❌ Agente non riconosciuto: `{agent_name}`\n\nAgenti disponibili: {nomi}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    status = await update.message.reply_text(
        f"🧠 *Mentor* sta analizzando *{agent_name.capitalize()}*\n"
        f"_(ultime {limit} conversazioni)_",
        parse_mode=ParseMode.MARKDOWN,
    )

    try:
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id, action=ChatAction.TYPING
        )
        await status.delete()
        await _run_mentor_for_agent(
            agent_name=agent_name,
            limit=limit,
            reply_fn=update.message.reply_text,
            bot_data=context.application.bot_data,
        )
    except Exception as e:
        logger.error(f"Errore /mentor: {e}", exc_info=True)
        await update.message.reply_text("⚠️ Errore durante l'analisi. Riprova.")


async def handle_mentor_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Gestisce i bottoni inline ✅ Applica / ✏️ Modifica / ❌ Rifiuta
    """
    query = update.callback_query
    await query.answer()

    data = query.data  # formato: mentor_apply:alex:abc12345
    parts = data.split(":")
    if len(parts) != 3:
        return

    action, agent_name, proposal_id = parts
    proposals = context.application.bot_data.get("mentor_proposals", {})
    proposal  = proposals.get(proposal_id)

    if not proposal:
        await query.edit_message_text(
            "⚠️ Proposta non trovata o scaduta. Riesegui `/mentor`.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # ── ✅ APPLICA ────────────────────────────────────────────────────────────
    if action == "mentor_apply":
        await mark_proposal_applied(proposal_id)
        proposed_soul = proposal["proposed_soul"]

        # Su Railway il filesystem è effimero — mandiamo il contenuto da pushare su git
        await query.edit_message_text(
            f"✅ *Proposta per {agent_name.capitalize()} approvata.*\n\n"
            f"Copia il testo qui sotto in `souls/{agent_name}.md` e pusha su git.\n"
            f"Railway ricaricherà il soul al prossimo deploy.",
            parse_mode=ParseMode.MARKDOWN,
        )
        # Manda il soul completo come messaggio separato facile da copiare
        soul_msg = f"```\n{proposed_soul}\n```"
        if len(soul_msg) > 4096:
            soul_msg = soul_msg[:4090] + "\n```"
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=soul_msg,
            parse_mode=ParseMode.MARKDOWN,
        )

        proposals.pop(proposal_id, None)

    # ── ✏️ MODIFICA ───────────────────────────────────────────────────────────
    elif action == "mentor_edit":
        # Invia il soul proposto come testo, chiede all'utente di modificarlo e rimandarlo
        await query.edit_message_text(
            f"✏️ *Modifica il soul di {agent_name.capitalize()}*\n\n"
            f"Copia il testo qui sotto, modifica quello che vuoi e rimandamelo "
            f"come risposta in questa chat. Scrivi `/annulla` per annullare.",
            parse_mode=ParseMode.MARKDOWN,
        )
        # Invia il soul proposto come messaggio separato (più facile da copiare)
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"```\n{proposal['proposed_soul']}\n```",
            parse_mode=ParseMode.MARKDOWN,
        )
        # Salva stato di attesa modifica in user_data
        context.user_data["pending_mentor_edit"] = {
            "agent":       agent_name,
            "proposal_id": proposal_id,
        }

    # ── ❌ RIFIUTA ────────────────────────────────────────────────────────────
    elif action == "mentor_reject":
        await mark_proposal_rejected(proposal_id)
        await query.edit_message_text(
            f"❌ *Proposta per {agent_name.capitalize()} rifiutata.*\n"
            f"Il soul rimane invariato.",
            parse_mode=ParseMode.MARKDOWN,
        )
        proposals.pop(proposal_id, None)


async def job_mentor_weekly(context):
    """
    Job domenicale: analizza UN agente per settimana in rotazione,
    per non sprecare il daily token limit.
    """
    if not GROUP_CHAT_ID:
        logger.warning("GROUP_CHAT_ID non configurato — skip job mentor")
        return

    # Rotazione settimanale: usa il numero della settimana per scegliere l'agente
    import datetime as _dt
    week_number = _dt.date.today().isocalendar()[1]
    agents_list = list(ANALYZABLE_AGENTS.keys())
    agent_name  = agents_list[week_number % len(agents_list)]

    logger.info(f"Job mentor settimanale: analisi di '{agent_name}' (settimana {week_number})")
    try:
        async def send_to_group(text, parse_mode, reply_markup=None):
            await context.bot.send_message(
                chat_id=GROUP_CHAT_ID,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )

        await _run_mentor_for_agent(
            agent_name=agent_name,
            limit=15,
            reply_fn=send_to_group,
            bot_data=context.application.bot_data,
        )
    except Exception as e:
        logger.error(f"Errore job mentor: {e}", exc_info=True)


async def cmd_dietro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current = context.user_data.get("show_behind", False)
    context.user_data["show_behind"] = not current
    stato = "attivata ✅" if not current else "disattivata ❌"
    await update.message.reply_text(f"🔬 Modalità *dietro le quinte* {stato}", parse_mode=ParseMode.MARKDOWN)


async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = await update.message.reply_text("🎮 *Luca* sta leggendo le ultime notizie...", parse_mode=ParseMode.MARKDOWN)
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        news_items = await fetch_recent_news(max_items=20)
        summary    = await luca_summarize_recent(news_items, hours=4)
        await status.delete()
        await update.message.reply_text(summary, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Errore /news: {e}", exc_info=True)
        await status.edit_text("⚠️ Errore nel recupero delle notizie.")


# ── Job automatici ─────────────────────────────────────────────────────────────

async def job_morning_weather(context):
    if not GROUP_CHAT_ID or not METEO_TOPIC_ID:
        return
    try:
        briefing = await giorgio_morning_briefing(MORNING_CITIES)
        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID, message_thread_id=METEO_TOPIC_ID,
            text=briefing, parse_mode=ParseMode.MARKDOWN,
        )
        # Salva briefing per il check contraddizioni di Sophia
        for city in MORNING_CITIES:
            store_weather_briefing(city["name"].lower(), briefing)
        # Sophia controlla contraddizioni con ieri
        asyncio.create_task(
            sophia_check_weather_contradiction(context.bot, GROUP_CHAT_ID)
        )
    except Exception as e:
        logger.error(f"Errore job meteo: {e}")


async def job_check_news(context):
    if not GROUP_CHAT_ID or not NEWS_TOPIC_ID:
        return
    new_news = await fetch_new_multiplayer_news(max_items=10)
    for item in (new_news or []):
        try:
            comment = await luca_comment_news(item["title"], item["url"])
            msg     = format_news_message(item["title"], item["url"], comment)
            await context.bot.send_message(
                chat_id=GROUP_CHAT_ID, message_thread_id=NEWS_TOPIC_ID,
                text=msg, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=False,
            )
            mark_seen(item["url"], item["title"])
        except Exception as e:
            logger.error(f"Errore notizia: {e}")


async def job_daily_digest(context):
    if not GROUP_CHAT_ID or not NEWS_TOPIC_ID:
        return
    try:
        digest = await luca_daily_digest(get_yesterday_news())
        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID, message_thread_id=NEWS_TOPIC_ID,
            text=digest, parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.error(f"Errore digest: {e}")


async def job_sophia_daily_recap(context):
    """Ogni sera alle 18:00 Sophia manda il recap in General."""
    if not GROUP_CHAT_ID:
        return
    try:
        recap = await sophia_daily_recap(sophia_get_activity())
        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID, text=recap, parse_mode=ParseMode.MARKDOWN,
        )
        sophia_reset_activity()
        logger.info("Recap Sophia ✅")
    except Exception as e:
        logger.error(f"Errore recap Sophia: {e}")


async def job_check_reminders(context):
    """Ogni minuto controlla i promemoria scaduti."""
    for r in sophia_get_due_reminders():
        try:
            kwargs = {
                "chat_id": r["chat_id"],
                "text": f"🔔 *Promemoria!*\n\n_{r['text']}_",
                "parse_mode": ParseMode.MARKDOWN,
            }
            if r.get("thread_id"):
                kwargs["message_thread_id"] = r["thread_id"]
            await context.bot.send_message(**kwargs)
        except Exception as e:
            logger.error(f"Errore promemoria: {e}")


# ── Main ───────────────────────────────────────────────────────────────────────

async def job_sophia_check_unanswered(context):
    """Ogni 5 minuti Sophia controlla messaggi rimasti senza risposta."""
    if not GROUP_CHAT_ID:
        return
    await sophia_check_unanswered(context.bot, GROUP_CHAT_ID)


async def post_init(app):
    await init_db()
    await init_news_db()
    await init_vector_db()
    await init_profiles_db()
    await init_episode_db()
    await init_agent_state_db()
    await init_mentor_db()

    # Avvia il server webhook in parallelo
    set_bot_app(app)
    await start_webhook_server()
    await app.bot.set_my_commands([
        BotCommand("start",   "Benvenuto e info"),
        BotCommand("agenti",  "Chi c'è nel gruppo"),
        BotCommand("profilo", "Il tuo profilo utente"),
        BotCommand("memoria", "Cosa ricordo di te"),
        BotCommand("nota",    "Aggiungi una nota su di te"),
        BotCommand("oggi",    "Riassunto narrativo della giornata"),
        BotCommand("dietro",  "Toggle ragionamento interno"),
        BotCommand("news",    "Ultime notizie gaming"),
        BotCommand("mentor",  "Analisi e miglioramento agenti"),
    ])

    if os.environ.get("ENABLE_NEWS_JOB", "false").lower() == "true":
        app.job_queue.run_repeating(job_check_news, interval=1800, first=60, name="check_news")
        app.job_queue.run_daily(job_daily_digest, time=datetime.time(7, 0, 0), name="daily_digest")

    if os.environ.get("ENABLE_METEO_JOB", "false").lower() == "true":
        app.job_queue.run_daily(
            job_morning_weather, time=datetime.time(5, 0, 0), name="morning_weather",
        )

    if os.environ.get("ENABLE_SOPHIA_RECAP", "false").lower() == "true":
        app.job_queue.run_daily(
            job_sophia_daily_recap, time=datetime.time(16, 0, 0), name="sophia_recap",
        )

    if os.environ.get("ENABLE_MENTOR_JOB", "false").lower() == "true":
        app.job_queue.run_daily(
            job_mentor_weekly,
            time=datetime.time(8, 0, 0),   # domenica 08:00 UTC = 10:00 CEST
            days=(6,),                      # 6 = domenica
            name="mentor_weekly",
        )

    # Promemoria sempre attivi
    app.job_queue.run_repeating(job_check_reminders, interval=60, first=30, name="reminders")

    # Sophia orchestratore — messaggi senza risposta (ogni 5 minuti)
    app.job_queue.run_repeating(
        job_sophia_check_unanswered, interval=300, first=60, name="sophia_unanswered"
    )
    logger.info("Bot inizializzato ✅")


def main():
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("agenti",  cmd_agenti))
    app.add_handler(CommandHandler("memoria", cmd_memoria))
    app.add_handler(CommandHandler("profilo", cmd_profilo))
    app.add_handler(CommandHandler("oggi",    cmd_oggi))
    app.add_handler(CommandHandler("nota",    cmd_nota))
    app.add_handler(CommandHandler("dietro",  cmd_dietro))
    app.add_handler(CommandHandler("news",    cmd_news))
    app.add_handler(CommandHandler("mentor",       cmd_mentor))
    app.add_handler(CommandHandler("mentor_debug", cmd_mentor_debug))
    app.add_handler(CallbackQueryHandler(handle_mentor_callback, pattern=r"^mentor_"))

    # Benvenuto nuovi membri
    app.add_handler(ChatMemberHandler(handle_new_member, ChatMemberHandler.CHAT_MEMBER))

    # Messaggi
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot avviato!")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "edited_message", "chat_member"],
    )


if __name__ == "__main__":
    main()
