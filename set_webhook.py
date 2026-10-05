"""Point Telegram at your PythonAnywhere web app.

Usage:  python set_webhook.py yourusername
"""
import json
import sys
import urllib.parse
import urllib.request

import core
from flask_app import WEBHOOK_SECRET

if len(sys.argv) != 2:
    sys.exit("Usage: python set_webhook.py <pythonanywhere-username>")
if not core.TOKEN or not WEBHOOK_SECRET:
    sys.exit("Set TELEGRAM_BOT_TOKEN and WEBHOOK_SECRET in .env first.")

url = f"https://{sys.argv[1].lower()}.pythonanywhere.com/webhook"
data = urllib.parse.urlencode({
    "url": url,
    "secret_token": WEBHOOK_SECRET,
    "allowed_updates": json.dumps(["message"]),
    "drop_pending_updates": "true",
}).encode()
with urllib.request.urlopen(f"https://api.telegram.org/bot{core.TOKEN}/setWebhook", data) as r:
    print(json.load(r))
print("Webhook set to", url)
