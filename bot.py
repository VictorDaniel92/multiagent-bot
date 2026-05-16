import os
import re
import logging
import datetime
from telegram import Update, BotCommand, ChatMemberUpdated
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    ChatMemberHandler, filters, ContextTypes
)
from telegram.constants import ParseMode, ChatAction

from agents import run_pipeline
from memory import init_db, get_memory, set_memory, update_topics, format_memory_for_prompt
from news_agent import (
    fetch_new_multiplayer_news, fetch_recent_news,
    luca_comment_news, luca_daily_digest, luca_news_summary,
    format_news_message, get_yesterday_news, mark_seen, init_news_db,
    luca_answer_question,
)
from weather_agent import (
    extract_city, giorgio_forecast, giorgio_morning_briefing, MORNING_CITIES
)
from sophia_agent import (
    sophia_is_mentioned, sophia_route_request, sophia_format_agent_message,
    sophia_welcome, sophia_daily_recap, sophia_agent_status,
    sophia_log_activity, sophia_get_activity, sophia_reset_activity,
    sophia_parse_reminder, sophia_add_reminder, sophia_get_due_reminders,
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
    57:   "news",      # Topic News  (Luca)
    99:   "meteo",     # Topic Meteo (Giorgio)
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
}


# ── Utils ──────────────────────────────────────────────────────────────────────

def get_topic(update: Update) -> str:
    thread_id = getattr(update.message, "message_thread_id", None)
    return TOPIC_MAP.get(thread_id, "ricerca")


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

    # Logga attività per il recap serale
    sophia_log_activity(topic)

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

                logger.info(f"Sophia → {agent} (topic {topic_id}) ✅")

            except Exception as e:
                logger.error(f"Errore Sophia→{agent}: {e}", exc_info=True)

        return

    emoji = TOPIC_EMOJI.get(topic, "💬")

    # ── NEWS (Luca) ────────────────────────────────────────────────────────
    if topic == "news":
        from agents import call_llm
        import json as _json
        guard_result = await call_llm(
            system='Classificatore. La domanda riguarda videogiochi/gaming/industria videoludica? Rispondi SOLO con JSON: {"ok": true} oppure {"ok": false}',
            messages=[{"role": "user", "content": f"Domanda: {question}"}],
            max_tokens=20
        )
        try:
            ok = _json.loads(guard_result.strip()).get("ok", True)
        except Exception:
            ok = True

        if not ok:
            await message.reply_text(
                "🎮 Questo topic è dedicato ai *videogiochi* e all'industria gaming.\n\n"
                "Per altre domande usa il topic giusto:\n"
                "🔍 Ricerca · 💻 Coding · 🧠 Brainstorming · 📊 Analisi",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        status_msg = await message.reply_text("🎮 *Luca* sta pensando...", parse_mode=ParseMode.MARKDOWN)
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            answer = await luca_answer_question(question)
            await status_msg.delete()
            await message.reply_text(answer, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.error(f"Errore Luca: {e}", exc_info=True)
            await status_msg.edit_text("⚠️ Errore. Riprova tra qualche secondo.")
        return

    # ── METEO (Giorgio) ────────────────────────────────────────────────────
    if topic == "meteo":
        from agents import call_llm
        import json as _json
        guard_result = await call_llm(
            system='Classificatore. La domanda riguarda meteo/tempo atmosferico/previsioni? Rispondi SOLO con JSON: {"ok": true} oppure {"ok": false}',
            messages=[{"role": "user", "content": f"Domanda: {question}"}],
            max_tokens=20
        )
        try:
            ok = _json.loads(guard_result.strip()).get("ok", True)
        except Exception:
            ok = True

        if not ok:
            await message.reply_text(
                "🌤️ Questo topic è dedicato al *meteo*.\n\n"
                "Chiedimi il tempo di una città, es: _\"che tempo fa a Roma?\"_\n\n"
                "Per altre domande usa il topic giusto.",
                parse_mode=ParseMode.MARKDOWN
            )
            return

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
        except Exception as e:
            logger.error(f"Errore Giorgio: {e}", exc_info=True)
            await status_msg.edit_text("⚠️ Errore meteo. Riprova tra qualche secondo.")
        return

    # ── TOPIC GUARD (ricerca, coding, brainstorming, analisi) ──────────────
    from agents import topic_guard
    guard = await topic_guard(question, topic)
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

    # ── PIPELINE GENERALE ─────────────────────────────────────────────────
    memory_context = await format_memory_for_prompt(user_id)
    status_msg = await message.reply_text(f"⏳ Avvio pipeline {emoji}...")

    try:
        result = await run_pipeline_with_updates(
            question, topic, memory_context, status_msg, context.bot, chat_id
        )
        await status_msg.delete()

        answer = result["answer"]
        if len(answer) > 4000:
            answer = answer[:4000] + "\n\n_(risposta troncata)_"
        await message.reply_text(answer)

        if result["queries"]:
            await update_topics(user_id, result["queries"][:2])

        if context.user_data.get("show_behind") and result["queries"]:
            await message.reply_text(build_behind_scenes(result), parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        logger.error(f"Errore pipeline: {e}", exc_info=True)
        await status_msg.edit_text("⚠️ Errore. Riprova tra qualche secondo.")


async def run_pipeline_with_updates(question, topic, memory_context, status_msg, bot, chat_id):
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
    answer = await alex_answer(question, queries, briefing, topic, memory_context)

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
            "🌤️ *Giorgio* — Meteo (topic Meteo)\n\n"
            "Nel gruppo ricerca/analisi:\n"
            "🎯 *Max* · 🔍 *Sofia* · ✍️ *Alex*",
            parse_mode=ParseMode.MARKDOWN
        )


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
        summary    = await luca_news_summary(news_items, hours=4)
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

async def post_init(app):
    await init_db()
    await init_news_db()
    await app.bot.set_my_commands([
        BotCommand("start",   "Benvenuto e info"),
        BotCommand("agenti",  "Chi c'è nel gruppo"),
        BotCommand("memoria", "Cosa ricordo di te"),
        BotCommand("nota",    "Aggiungi una nota su di te"),
        BotCommand("dietro",  "Toggle ragionamento interno"),
        BotCommand("news",    "Ultime notizie gaming"),
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

    # Promemoria sempre attivi
    app.job_queue.run_repeating(job_check_reminders, interval=60, first=30, name="reminders")
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
    app.add_handler(CommandHandler("nota",    cmd_nota))
    app.add_handler(CommandHandler("dietro",  cmd_dietro))
    app.add_handler(CommandHandler("news",    cmd_news))

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
