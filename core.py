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
    "/total – all-time summary by month\n"
    "/undo – delete the last entry\n"
    "/del 3 – delete row #3 of today's table\n"
    "/clear – empty today's table\n/clear month – empty this month's table"
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


# Telegram allows 4096 characters per message; leave room for text around the table
MAX_TABLE_CHARS = 3500


def render(title, header, body, footer, right=(0, -1)):
    """Draw a text table. Columns listed in `right` are right-aligned."""
    n = len(header)
    right = {c % n for c in right}
    widths = [max(len(row[c]) for row in [header, footer, *body]) for c in range(n)]

    def line(row):
        return " | ".join(row[c].rjust(widths[c]) if c in right else row[c].ljust(widths[c])
                          for c in range(n))

    sep = "-+-".join("-" * w for w in widths)
    text = "\n".join([line(header), sep, *map(line, body), sep, line(footer)])
    return f"<b>{escape(title)}</b>\n<pre>{escape(text)}</pre>", len(title) + len(text)


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

    # Too long for one message: fold the oldest rows into a single "earlier" row.
    # Row numbers and the total stay correct.
    hidden = 0
    while True:
        shown = body[hidden:]
        if hidden:
            earlier = [""] * len(header)
            earlier[-2] = f"…{hidden} earlier"
            earlier[-1] = fmt_amount(sum(r[2] for r in rows[:hidden]))
            shown = [earlier, *shown]
        html, size = render(title, header, shown, footer)
        if size <= MAX_TABLE_CHARS or hidden >= len(body) - 1:
            return html
        hidden += 1


def today_table(user_id):
    t = now().date().isoformat()
    return table(rows_for(user_id, t, t), f"Today ({t})")


def month_table(user_id):
    today = now().date()
    rows = rows_for(user_id, today.replace(day=1).isoformat(), today.isoformat())
    return table(rows, f"This month ({today:%B %Y})", show_date=True)


def total_table(user_id):
    with db() as conn:
        months = conn.execute(
            "SELECT substr(spent_on, 1, 7) AS ym, SUM(amount), COUNT(*) FROM expenses "
            "WHERE user_id = ? GROUP BY ym ORDER BY ym",
            (user_id,),
        ).fetchall()
    if not months:
        return "<b>All time</b>\nNo expenses yet."
    body = [[datetime.strptime(ym, "%Y-%m").strftime("%b %Y"), str(n), fmt_amount(s)] for ym, s, n in months]
    footer = ["TOTAL", str(sum(m[2] for m in months)), fmt_amount(sum(m[1] for m in months))]
    return render("All time", ["Month", "Items", "Amount"], body, footer, right=(1, 2))[0]


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


def delete_row(user_id, arg):
    rows = rows_for(user_id, now().date().isoformat(), now().date().isoformat())
    if not arg.isdigit() or not 1 <= int(arg) <= len(rows):
        return "Send the row number from today's table, e.g. <code>/del 2</code>\n\n" + today_table(user_id)
    row_id, name, amount, _ = rows[int(arg) - 1]
    with db() as conn:
        conn.execute("DELETE FROM expenses WHERE id = ?", (row_id,))
    return f"Removed: {escape(name)} - {fmt_amount(amount)}\n\n" + today_table(user_id)


def clear(user_id, args):
    today = now().date()
    month = args[:1] == ["month"]
    start = today.replace(day=1) if month else today
    label = "this month's" if month else "today's"
    rows = rows_for(user_id, start.isoformat(), today.isoformat())
    if not rows:
        return f"Nothing to clear, {label} table is already empty."
    if args[-1:] != ["yes"]:
        cmd = "/clear month yes" if month else "/clear yes"
        return f"This deletes all {len(rows)} of {label} entries.\nSend <code>{cmd}</code> to confirm."
    with db() as conn:
        conn.execute("DELETE FROM expenses WHERE user_id = ? AND spent_on BETWEEN ? AND ?",
                     (user_id, start.isoformat(), today.isoformat()))
    return f"🗑 Cleared {len(rows)} of {label} entries."


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
    if cmd == "/total":
        return total_table(user_id)
    if cmd == "/undo":
        return undo(user_id)
    args = text.split()[1:]
    if cmd in ("/del", "/delete"):
        return delete_row(user_id, args[0] if args else "")
    if cmd == "/clear":
        return clear(user_id, [a.lower() for a in args])
    if cmd:
        return "Unknown command.\n\n" + HELP
    return add_lines(user_id, text)
