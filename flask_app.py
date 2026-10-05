"""Webhook web app for PythonAnywhere. Telegram POSTs each message here."""
import os

from flask import Flask, abort, jsonify, request

import core

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

app = Flask(__name__)


@app.get("/")
def index():
    return "Expense bot is running."


@app.post("/webhook")
def webhook():
    if not WEBHOOK_SECRET or request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        abort(403)
    msg = (request.get_json(silent=True) or {}).get("message") or {}
    if "text" not in msg:
        return "ok"
    reply = core.handle(msg["from"]["id"], msg["text"])
    # Answer inside the webhook response, so the server never has to call Telegram itself
    return jsonify(method="sendMessage", chat_id=msg["chat"]["id"], text=reply, parse_mode="HTML")
