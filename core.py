"""Expense storage, parsing and summary tables. Shared by bot.py (local) and flask_app.py (server)."""
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_USER_ID = os.environ.get("ALLOWED_USER_ID", "").strip()
TZ = ZoneInfo(os.environ.get("TIMEZONE", "UTC").strip() or "UTC")
DB_PATH = os.path.join(BASE_DIR, "expenses.db")

# "name - amount", also accepts "name : amount", "name = amount" or "name 20"
LINE_RE = re.compile(r"^\s*(.+?)\s*(?:[-:=]\s*|\s)(\d+(?:\.\d+)?)\s*$")

HELP = (
    "Send expenses as:\n<code>tea - 20</code>\n<code>bus fare - 45</code>\n"
    "(one or many lines per message)\n\n"
    "Commands:\n/today – today's summary\n/month – this month's summary\n"
    "/undo – delete the last entry"
)


def now():
    return datetime.now(TZ)


# ---------- database ----------
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    try:
        with conn:  # commits on success, rolls back on error
            conn.execute(
                """CREATE TABLE IF NOT EXISTS expenses (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       user_id INTEGER NOT NULL,
                       name TEXT NOT NULL,
                       amount REAL NOT NULL,
                       spent_on TEXT NOT NULL,
                       created_at TEXT NOT NULL
                   )"""
            )
            yield conn
    finally:
        conn.close()


def add_expense(user_id, name, amount):
    t = now()
    with db() as conn:
        conn.execute(
            "INSERT INTO expenses (user_id, name, amount, spent_on, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, name, amount, t.date().isoformat(), t.isoformat(timespec="seconds")),
        )


def rows_for(user_id, start, end):
    with db() as conn:
        return conn.execute(
            "SELECT id, name, amount, spent_on FROM expenses "
            "WHERE user_id = ? AND spent_on BETWEEN ? AND ? ORDER BY id",
            (user_id, start, end),
        ).fetchall()


# ---------- formatting ----------
def fmt_amount(x):
    return f"{x:,.2f}".rstrip("0").rstrip(".")


def table(rows, title, show_date=False):
    if not rows:
        return f"<b>{escape(title)}</b>\nNo expenses yet."
    header = ["#", "Date", "Cost", "Amount"] if show_date else ["#", "Cost", "Amount"]
    body = []
    for i, (_id, name, amount, spent_on) in enumerate(rows, 1):
        r = [str(i), spent_on[5:], name[:18], fmt_amount(amount)]
        body.append(r if show_date else [r[0], r[2], r[3]])
    total = sum(r[2] for r in rows)
    footer = ([""] * (len(header) - 2)) + ["TOTAL", fmt_amount(total)]

    widths = [max(len(row[c]) for row in [header, footer, *body]) for c in range(len(header))]

    def line(row):
        cells = [row[c].rjust(widths[c]) if c in (0, len(row) - 1) else row[c].ljust(widths[c])
                 for c in range(len(row))]
        return " | ".join(cells)

    sep = "-+-".join("-" * w for w in widths)
    text = "\n".join([line(header), sep, *map(line, body), sep, line(footer)])
    return f"<b>{escape(title)}</b>\n<pre>{escape(text)}</pre>"


def today_table(user_id):
    t = now().date().isoformat()
    return table(rows_for(user_id, t, t), f"Today ({t})")


def month_table(user_id):
    today = now().date()
    rows = rows_for(user_id, today.replace(day=1).isoformat(), today.isoformat())
    return table(rows, f"This month ({today:%B %Y})", show_date=True)


# ---------- message handling ----------
def allowed(user_id):
    return not ALLOWED_USER_ID or str(user_id) == ALLOWED_USER_ID


def undo(user_id):
    with db() as conn:
        row = conn.execute(
            "SELECT id, name, amount FROM expenses WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,)
        ).fetchone()
        if not row:
            return "Nothing to undo."
        conn.execute("DELETE FROM expenses WHERE id = ?", (row[0],))
    return f"Removed: {escape(row[1])} - {fmt_amount(row[2])}\n\n" + today_table(user_id)


def add_lines(user_id, text):
    added, bad = 0, []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        m = LINE_RE.match(raw)
        if m:
            add_expense(user_id, m.group(1).strip(), float(m.group(2)))
            added += 1
        else:
            bad.append(raw.strip())

    msg = []
    if added:
        msg.append(f"✅ Added {added} item{'s' if added > 1 else ''}.")
    if bad:
        msg.append("⚠️ Couldn't read: " + ", ".join(f"<code>{escape(b)}</code>" for b in bad)
                   + "\nUse the format <code>name - amount</code>")
    msg.append(today_table(user_id))
    return "\n\n".join(msg)


def handle(user_id, text):
    """Return the HTML reply for one incoming text message."""
    cmd = text.strip().split()[0].split("@")[0].lower() if text.strip().startswith("/") else None
    if cmd in ("/start", "/help"):
        return f"Hi! Your Telegram user ID is <code>{user_id}</code>.\n\n{HELP}"
    if not allowed(user_id):
        return "Sorry, this bot is private."
    if cmd == "/today":
        return today_table(user_id)
    if cmd == "/month":
        return month_table(user_id)
    if cmd == "/undo":
        return undo(user_id)
    if cmd:
        return "Unknown command.\n\n" + HELP
    return add_lines(user_id, text)
