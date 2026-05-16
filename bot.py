import os
import logging
from telegram import Update, BotCommand
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
from telegram.constants import ParseMode, ChatAction

from agents import run_pipeline
from memory import init_db, get_memory, set_memory, update_topics, format_memory_for_prompt

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]

# Mappa topic_id Telegram → nome topic interno
# Vai su Telegram, crea i topic nel gruppo e metti qui i loro ID
# Puoi trovarli nei log quando mandi un messaggio in un topic
TOPIC_MAP = {
    None: "default",  # chat privata o topic sconosciuto
    # Esempio: 5: "coding", 7: "brainstorming"
    # Li aggiungeremo dopo aver creato i topic nel gruppo
}

# Emoji per ogni topic — usate nei messaggi di stato
TOPIC_EMOJI = {
    "ricerca":       "🔍",
    "coding":        "💻",
    "brainstorming": "🧠",
    "analisi":       "📊",
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
    emoji    = TOPIC_EMOJI.get(topic, "💬")

    # Carica la memoria utente per iniettarla nel contesto
    memory_context = await format_memory_for_prompt(user_id)

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


# ── MAIN ──────────────────────────────────────────────────────────────────────

async def post_init(app):
    """Inizializza il DB e imposta i comandi del bot."""
    await init_db()
    await app.bot.set_my_commands([
        BotCommand("start",    "Benvenuto e info"),
        BotCommand("agenti",   "Info sugli agenti"),
        BotCommand("memoria",  "Cosa ricordo di te"),
        BotCommand("nota",     "Aggiungi una nota su di te"),
        BotCommand("dietro",   "Toggle ragionamento interno"),
    ])


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
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
