import os
import asyncio
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
from telegram.constants import ParseMode, ChatAction

from agents import run_pipeline

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]


# ── HANDLERS ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Ciao! Sono un sistema multi-agente.\n\n"
        "Mandami qualsiasi domanda e tre agenti collaboreranno per risponderti:\n\n"
        "🎯 *Max* — pianifica le ricerche\n"
        "🔍 *Sofia* — cerca e sintetizza\n"
        "✍️ *Alex* — scrive la risposta finale\n\n"
        "Puoi usare /dietro per vedere il ragionamento interno degli agenti.\n\n"
        "Cosa vuoi sapere?",
        parse_mode=ParseMode.MARKDOWN
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handler principale: riceve il messaggio, avvia il pipeline,
    aggiorna l'utente con messaggi progressivi mentre gli agenti lavorano.
    """
    question = update.message.text.strip()
    if not question:
        return

    chat_id = update.effective_chat.id

    # Messaggio di stato iniziale — verrà aggiornato man mano
    status_msg = await update.message.reply_text("⏳ Avvio il pipeline...", parse_mode=ParseMode.MARKDOWN)

    try:
        # Invia "typing..." mentre elabora
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

        # Avvia il pipeline — i tre agenti lavorano in sequenza
        # Usiamo asyncio per aggiornare lo stato mentre aspettiamo
        result = await run_pipeline_with_updates(question, status_msg, context.bot, chat_id)

        # Cancella il messaggio di stato
        await status_msg.delete()

        # Risposta finale di Alex
        answer = result["answer"]

        # Telegram ha un limite di 4096 caratteri per messaggio
        if len(answer) > 4000:
            answer = answer[:4000] + "\n\n_(risposta troncata)_"

        await update.message.reply_text(answer)

        # Se l'utente ha usato /dietro in precedenza, mostra anche il ragionamento
        show_behind = context.user_data.get("show_behind", False)
        if show_behind and result["queries"]:
            behind = build_behind_scenes(result)
            await update.message.reply_text(behind, parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        logger.error(f"Errore nel pipeline: {e}", exc_info=True)
        await status_msg.edit_text("⚠️ Si è verificato un errore. Riprova tra qualche secondo.")


async def run_pipeline_with_updates(question: str, status_msg, bot, chat_id: int) -> dict:
    """
    Esegue il pipeline aggiornando il messaggio di stato ad ogni step.
    Questo dà all'utente feedback visivo mentre gli agenti lavorano.
    """
    from agents import max_plan, sofia_synthesize, alex_answer

    # Step 1: Max
    await status_msg.edit_text("🎯 *Max* sta pianificando le ricerche...", parse_mode=ParseMode.MARKDOWN)
    await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    queries = await max_plan(question)

    # Step 2: Sofia
    if queries:
        query_preview = ", ".join(f'"{q}"' for q in queries[:2])
        await status_msg.edit_text(
            f"🔍 *Sofia* sta cercando: {query_preview}...",
            parse_mode=ParseMode.MARKDOWN
        )
        await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        briefing, _ = await sofia_synthesize(question, queries)
    else:
        briefing = "Nessuna ricerca necessaria per questa domanda."

    # Step 3: Alex
    await status_msg.edit_text("✍️ *Alex* sta scrivendo la risposta...", parse_mode=ParseMode.MARKDOWN)
    await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    answer = await alex_answer(question, queries, briefing)

    return {"queries": queries, "briefing": briefing, "answer": answer}


def build_behind_scenes(result: dict) -> str:
    """Costruisce il messaggio 'dietro le quinte' con il ragionamento degli agenti."""
    lines = ["🔬 *Dietro le quinte*\n"]

    if result["queries"]:
        lines.append("🎯 *Max ha cercato:*")
        for q in result["queries"]:
            lines.append(f"  • `{q}`")
        lines.append("")

    lines.append("🔍 *Sofia ha sintetizzato:*")
    briefing_short = result["briefing"][:600] + ("..." if len(result["briefing"]) > 600 else "")
    lines.append(briefing_short)

    return "\n".join(lines)


async def toggle_behind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /dietro — attiva/disattiva la visualizzazione del ragionamento interno."""
    current = context.user_data.get("show_behind", False)
    context.user_data["show_behind"] = not current

    if not current:
        await update.message.reply_text(
            "🔬 Modalità *dietro le quinte* attivata!\n"
            "Da ora vedrò anche il ragionamento degli agenti dopo ogni risposta.\n"
            "Usa /dietro di nuovo per disattivarla.",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text("✅ Modalità *dietro le quinte* disattivata.", parse_mode=ParseMode.MARKDOWN)


async def agenti(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /agenti — mostra info sugli agenti disponibili."""
    await update.message.reply_text(
        "🤖 *Gli agenti del sistema*\n\n"
        "🎯 *Max* — Il Pianificatore\n"
        "Freddo, diretto, efficiente. Analizza la tua domanda e decide esattamente cosa cercare sul web. Non parla mai con l'utente direttamente.\n\n"
        "🔍 *Sofia* — La Ricercatrice\n"
        "Curiosa ed entusiasta. Esegue le ricerche web e trasforma i risultati grezzi in un briefing strutturato per Alex. Ama trovare connessioni inaspettate.\n\n"
        "✍️ *Alex* — Il Comunicatore\n"
        "Preciso e affidabile. Legge il piano di Max e il briefing di Sofia, poi scrive la risposta finale per te. È lui che vedi sempre.\n\n"
        "_Usa /dietro per vedere cosa fanno Max e Sofia in background._",
        parse_mode=ParseMode.MARKDOWN
    )


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("agenti",  agenti))
    app.add_handler(CommandHandler("dietro",  toggle_behind))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot avviato!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()