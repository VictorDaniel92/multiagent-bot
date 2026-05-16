import os
import logging
import datetime
from telegram import Update, BotCommand
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
from telegram.constants import ParseMode, ChatAction

from agents import run_pipeline
from memory import init_db, get_memory, set_memory, update_topics, format_memory_for_prompt
from news_agent import (
    fetch_new_multiplayer_news, luca_comment_news, luca_daily_digest,
    format_news_message, get_yesterday_news, mark_seen, init_news_db,
    luca_answer_question,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]

# Mappa topic_id Telegram → nome topic interno
# Vai su Telegram, crea i topic nel gruppo e metti qui i loro ID
# Puoi trovarli nei log quando mandi un messaggio in un topic
TOPIC_MAP = {
    None: "default",
    2:    "ricerca",
    4:    "coding",
    6:    "brainstorming",
    8:    "analisi",
    # ⬇ Crea il topic "News" nel gruppo e metti qui il suo thread_id
    # Puoi trovarlo nei log dopo aver mandato un messaggio in quel topic
    # Esempio: 10: "news",
}

# ID della chat del gruppo (per i job automatici)
# Trovalo nei log: effective_chat.id quando scrivi nel gruppo
GROUP_CHAT_ID: int = int(os.environ.get("GROUP_CHAT_ID", "0"))

# Thread ID del topic "News" — aggiornalo dopo aver creato il topic
NEWS_TOPIC_ID: int | None = int(os.environ.get("NEWS_TOPIC_ID", "0")) or None

# Emoji per ogni topic
TOPIC_EMOJI = {
    "ricerca":       "🔍",
    "coding":        "💻",
    "brainstorming": "🧠",
    "analisi":       "📊",
    "news":          "🎮",
    "default":       "💬",
}


# ── UTILS ─────────────────────────────────────────────────────────────────────

def get_topic(update: Update) -> str:
    """Determina il topic dal thread_id del messaggio."""
    thread_id = getattr(update.message, "message_thread_id", None)
    return TOPIC_MAP.get(thread_id, "default")


# ── HANDLERS ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id   = update.effective_user.id
    user_name = update.effective_user.first_name

    # Salva il nome nella memoria
    await set_memory(user_id, "name", user_name)

    await update.message.reply_text(
        f"👋 Ciao {user_name}! Sono un sistema multi-agente.\n\n"
        f"Tre agenti collaborano per risponderti:\n\n"
        f"🎯 *Max* — pianifica le ricerche\n"
        f"🔍 *Sofia* — cerca e sintetizza\n"
        f"✍️ *Alex* — scrive la risposta finale\n\n"
        f"*Topic disponibili:*\n"
        f"🔍 Ricerca generale\n"
        f"💻 Coding & Tech\n"
        f"🧠 Brainstorming\n"
        f"📊 Analisi\n\n"
        f"Comandi:\n"
        f"/agenti — info sugli agenti\n"
        f"/memoria — cosa ricordo di te\n"
        f"/dietro — toggle ragionamento interno\n"
        f"/nota [testo] — aggiungi una nota su di te",
        parse_mode=ParseMode.MARKDOWN
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    question  = update.message.text.strip()
    if not question:
        return

    user_id  = update.effective_user.id
    chat_id  = update.effective_chat.id
    topic    = get_topic(update)
    logger.info(f"Messaggio da topic thread_id={getattr(update.message, 'message_thread_id', None)}, topic={topic}")
    emoji    = TOPIC_EMOJI.get(topic, "💬")

    # ── TOPIC GUARD ──────────────────────────────────────────────────────────
    if topic != "default":
        from agents import topic_guard
        guard = await topic_guard(question, topic)
        if not guard["match"]:
            suggested     = guard["suggested"]
            suggested_emoji = TOPIC_EMOJI.get(suggested, "💬")
            await update.message.reply_text(
                f"⚠️ Questa domanda non è nel topic giusto!\n\n"
                f"Sei nel topic {emoji} *{topic}*, ma sembra più adatta a "
                f"{suggested_emoji} *{suggested}*.\n\n"
                f"_{guard['reason']}_\n\n"
                f"Scrivi lì la stessa domanda e ti rispondo al meglio 🙂",
                parse_mode=ParseMode.MARKDOWN
            )
            return
    # ─────────────────────────────────────────────────────────────────────────

    # Carica la memoria utente per iniettarla nel contesto
    memory_context = await format_memory_for_prompt(user_id)

    # ── ROUTING PER TOPIC ────────────────────────────────────────────────────
    # Ogni topic ha il suo agente dedicato
    if topic == "news":
        status_msg = await update.message.reply_text("🎮 *Luca* sta pensando...", parse_mode=ParseMode.MARKDOWN)
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            answer = await luca_answer_question(question)
            await status_msg.delete()
            await update.message.reply_text(answer, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.error(f"Errore risposta Luca: {e}", exc_info=True)
            await status_msg.edit_text("⚠️ Errore. Riprova tra qualche secondo.")
        return
    # ─────────────────────────────────────────────────────────────────────────

    status_msg = await update.message.reply_text(f"⏳ Avvio pipeline {emoji}...")

    try:
        result = await run_pipeline_with_updates(
            question, topic, memory_context, status_msg, context.bot, chat_id
        )

        await status_msg.delete()

        answer = result["answer"]
        if len(answer) > 4000:
            answer = answer[:4000] + "\n\n_(risposta troncata)_"

        await update.message.reply_text(answer)

        # Aggiorna memoria con i topic cercati
        if result["queries"]:
            await update_topics(user_id, result["queries"][:2])

        # Mostra ragionamento interno se attivo
        if context.user_data.get("show_behind") and result["queries"]:
            behind = build_behind_scenes(result)
            await update.message.reply_text(behind, parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        logger.error(f"Errore pipeline: {e}", exc_info=True)
        await status_msg.edit_text("⚠️ Errore. Riprova tra qualche secondo.")


async def run_pipeline_with_updates(
    question: str, topic: str, memory_context: str,
    status_msg, bot, chat_id: int
) -> dict:
    """Esegue il pipeline aggiornando il messaggio di stato ad ogni step."""
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


def build_behind_scenes(result: dict) -> str:
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


async def cmd_memoria(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra cosa il sistema ricorda dell'utente."""
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
    """Salva una nota personale nella memoria."""
    user_id = update.effective_user.id
    nota    = " ".join(context.args) if context.args else ""

    if not nota:
        await update.message.reply_text("Uso: /nota [testo]\nEsempio: /nota preferisco risposte brevi")
        return

    await set_memory(user_id, "notes", nota)
    await update.message.reply_text(f"✅ Nota salvata: _{nota}_", parse_mode=ParseMode.MARKDOWN)


async def cmd_agenti(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Gli agenti del sistema*\n\n"
        "🎯 *Max* — Il Pianificatore\n"
        "Freddo e diretto. Decide cosa cercare sul web. Non parla mai con te direttamente.\n\n"
        "🔍 *Sofia* — La Ricercatrice\n"
        "Curiosa ed entusiasta. Esegue le ricerche e sintetizza i risultati per Alex.\n\n"
        "✍️ *Alex* — Il Comunicatore\n"
        "Preciso e affidabile. Legge il lavoro degli altri e scrive la risposta finale.\n\n"
        "🗂 *Topic disponibili:*\n"
        "🔍 Ricerca — informazioni generali\n"
        "💻 Coding — codice e tech\n"
        "🧠 Brainstorming — idee creative\n"
        "📊 Analisi — ragionamento strutturato",
        parse_mode=ParseMode.MARKDOWN
    )


async def cmd_dietro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current = context.user_data.get("show_behind", False)
    context.user_data["show_behind"] = not current
    stato = "attivata ✅" if not current else "disattivata ❌"
    await update.message.reply_text(
        f"🔬 Modalità *dietro le quinte* {stato}",
        parse_mode=ParseMode.MARKDOWN
    )


# ── JOB: NEWS AUTOMATICHE ─────────────────────────────────────────────────────

async def job_check_news(context):
    """
    Controlla ogni 30 minuti se ci sono nuove notizie su multiplayer.it.
    Le invia nel topic News con il commento di Luca.
    """
    if not GROUP_CHAT_ID or not NEWS_TOPIC_ID:
        logger.warning("GROUP_CHAT_ID o NEWS_TOPIC_ID non configurati — skip job news")
        return

    new_news = await fetch_new_multiplayer_news(max_items=10)
    if not new_news:
        logger.info("Nessuna notizia nuova trovata")
        return

    for item in new_news:
        try:
            comment = await luca_comment_news(item["title"], item["url"])
            msg     = format_news_message(item["title"], item["url"], comment)

            await context.bot.send_message(
                chat_id=GROUP_CHAT_ID,
                message_thread_id=NEWS_TOPIC_ID,
                text=msg,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=False,
            )
            mark_seen(item["url"], item["title"])
            logger.info(f"Inviata notizia: {item['title'][:60]}")

        except Exception as e:
            logger.error(f"Errore invio notizia '{item['title'][:40]}': {e}")


async def job_daily_digest(context):
    """
    Ogni mattina alle 9:00 invia la rassegna stampa di Luca
    con le notizie più importanti del giorno prima.
    """
    if not GROUP_CHAT_ID or not NEWS_TOPIC_ID:
        logger.warning("GROUP_CHAT_ID o NEWS_TOPIC_ID non configurati — skip digest")
        return

    try:
        yesterday_news = get_yesterday_news()
        digest = await luca_daily_digest(yesterday_news)

        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            message_thread_id=NEWS_TOPIC_ID,
            text=digest,
            parse_mode=ParseMode.MARKDOWN,
        )
        logger.info(f"Digest inviato ({len(yesterday_news)} notizie di ieri)")

    except Exception as e:
        logger.error(f"Errore invio digest: {e}")


# ── MAIN ──────────────────────────────────────────────────────────────────────

async def post_init(app):
    """Inizializza il DB, imposta i comandi e schedula i job automatici."""
    await init_db()
    await init_news_db()
    await app.bot.set_my_commands([
        BotCommand("start",    "Benvenuto e info"),
        BotCommand("agenti",   "Info sugli agenti"),
        BotCommand("memoria",  "Cosa ricordo di te"),
        BotCommand("nota",     "Aggiungi una nota su di te"),
        BotCommand("dietro",   "Toggle ragionamento interno"),
    ])

    # ── Job: controlla news ogni 30 minuti ────────────────────────────────────
    app.job_queue.run_repeating(
        job_check_news,
        interval=1800,   # 30 minuti
        first=30,        # primo check dopo 30 secondi dall'avvio
        name="check_news",
    )

    # ── Job: digest mattutino alle 9:00 (ora italiana = UTC+2 in estate) ─────
    app.job_queue.run_daily(
        job_daily_digest,
        time=datetime.time(7, 0, 0),  # 07:00 UTC = 09:00 CEST
        name="daily_digest",
    )

    logger.info("Job news schedulati ✅")


def main():
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",    start))
    app.add_handler(CommandHandler("agenti",   cmd_agenti))
    app.add_handler(CommandHandler("memoria",  cmd_memoria))
    app.add_handler(CommandHandler("nota",     cmd_nota))
    app.add_handler(CommandHandler("dietro",   cmd_dietro))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot avviato!")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "edited_message"],
    )


if __name__ == "__main__":
    main()

