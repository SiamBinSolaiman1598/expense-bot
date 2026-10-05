"""Run the expense bot locally (polling). On PythonAnywhere, flask_app.py is used instead."""
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes, MessageHandler, filters

import core


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    reply = core.handle(update.effective_user.id, update.message.text)
    await update.message.reply_text(reply, parse_mode=ParseMode.HTML)


def main():
    app = Application.builder().token(core.TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT, on_text))
    print("Expense bot running. Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
