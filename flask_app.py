"""Webhook web app for PythonAnywhere. Telegram POSTs each message here."""
import json
import os
import urllib.request

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
    chat_id = msg["chat"]["id"]
    *first, last = core.handle(msg["from"]["id"], msg["text"])
    # Long replies come in several parts: send the earlier ones through the Telegram API...
    for text in first:
        send_message(chat_id, text)
    # ...and answer the last one inside the webhook response
    return jsonify(method="sendMessage", chat_id=chat_id, text=last, parse_mode="HTML")


def send_message(chat_id, text):
    body = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{core.TOKEN}/sendMessage", body,
                                 {"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=20).close()
