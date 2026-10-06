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
    "Commands:\n/today – today's items\n/month – this month's items and total\n"
    "/total – every item and total till today\n"
    "/undo – delete the last entry\n"
    "/del 3 – delete row #3 of the table you last looked at (/del 2 5 deletes several)\n"
    "/clear – empty today's table\n/clear month – empty this month's table"
)

# Telegram allows 4096 characters per message; longer tables are split into parts
MAX_MESSAGE_CHARS = 4000

# Which rows each summary shows. `date_chars` is how much of YYYY-MM-DD the Date column shows.
VIEWS = {
    "today": {"label": "today's", "date_chars": 0},
    "month": {"label": "this month's", "date_chars": 5},
    "all": {"label": "the all-time", "date_chars": 10},
}


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
            # The table each user saw last, so /del numbers match what's on their screen
            conn.execute(
                "CREATE TABLE IF NOT EXISTS last_view (user_id INTEGER PRIMARY KEY, view TEXT NOT NULL)"
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
            "WHERE user_id = ? AND spent_on BETWEEN ? AND ? ORDER BY spent_on, id",
            (user_id, start, end),
        ).fetchall()


def view_rows(user_id, view):
    today = now().date()
    start = {"today": today.isoformat(), "month": today.replace(day=1).isoformat(), "all": ""}[view]
    return rows_for(user_id, start, today.isoformat())


def set_last_view(user_id, view):
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO last_view (user_id, view) VALUES (?, ?)", (user_id, view))


def get_last_view(user_id):
    with db() as conn:
        row = conn.execute("SELECT view FROM last_view WHERE user_id = ?", (user_id,)).fetchone()
    return row[0] if row else "today"


# ---------- formatting ----------
def fmt_amount(x):
    return f"{x:,.2f}".rstrip("0").rstrip(".")


def view_title(view):
    today = now().date()
    return {
        "today": f"Today ({today.isoformat()})",
        "month": f"This month ({today:%B %Y})",
        "all": f"All time (till {today.isoformat()})",
    }[view]


def table(rows, title, date_chars=0):
    """Return the table as a list of messages (more than one only when it's too long for Telegram)."""
    if not rows:
        return [f"<b>{escape(title)}</b>\nNo expenses yet."]
    header = ["#", "Date", "Cost", "Amount"] if date_chars else ["#", "Cost", "Amount"]
    body = []
    for i, (_id, name, amount, spent_on) in enumerate(rows, 1):
        date = [spent_on[-date_chars:]] if date_chars else []
        body.append([str(i), *date, name[:18], fmt_amount(amount)])
    total = sum(r[2] for r in rows)
    footer = ([""] * (len(header) - 2)) + ["TOTAL", fmt_amount(total)]

    n = len(header)
    widths = [max(len(row[c]) for row in [header, footer, *body]) for c in range(n)]

    def line(row):
        return " | ".join(row[c].rjust(widths[c]) if c in (0, n - 1) else row[c].ljust(widths[c])
                          for c in range(n))

    sep = "-+-".join("-" * w for w in widths)
    line_len = len(sep) + 1
    per_part = max(1, (MAX_MESSAGE_CHARS - len(title) - 20 - 4 * line_len) // line_len)
    parts = [body[i:i + per_part] for i in range(0, len(body), per_part)]

    messages = []
    for k, part in enumerate(parts, 1):
        lines = [line(header), sep, *map(line, part)]
        if k == len(parts):
            lines += [sep, line(footer)]
        t = title if len(parts) == 1 else f"{title} ({k}/{len(parts)})"
        messages.append(f"<b>{escape(t)}</b>\n<pre>{escape(chr(10).join(lines))}</pre>")
    return messages


def show(user_id, view, note=None):
    """Messages showing one summary table, optionally after a note. Remembers it for /del."""
    set_last_view(user_id, view)
    messages = table(view_rows(user_id, view), view_title(view), VIEWS[view]["date_chars"])
    if note:
        if len(note) + len(messages[0]) < MAX_MESSAGE_CHARS:
            messages[0] = f"{note}\n\n{messages[0]}"
        else:
            messages.insert(0, note)
    return messages


# ---------- message handling ----------
def allowed(user_id):
    return not ALLOWED_USER_ID or str(user_id) == ALLOWED_USER_ID


def undo(user_id):
    with db() as conn:
        row = conn.execute(
            "SELECT id, name, amount FROM expenses WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,)
        ).fetchone()
        if not row:
            return ["Nothing to undo."]
        conn.execute("DELETE FROM expenses WHERE id = ?", (row[0],))
    return show(user_id, "today", f"Removed: {escape(row[1])} - {fmt_amount(row[2])}")


def delete_rows(user_id, args):
    view = get_last_view(user_id)
    rows = view_rows(user_id, view)
    label = VIEWS[view]["label"]
    nums = sorted({int(a) for a in args if a.isdigit()})
    if not nums or len(nums) != len(args) or not all(1 <= x <= len(rows) for x in nums):
        return [f"Send row numbers from {label} table (it has {len(rows)} rows), "
                "e.g. <code>/del 2</code> or <code>/del 2 5</code>.\n"
                "To delete from another table, open it first with /today, /month or /total."]
    picked = [rows[x - 1] for x in nums]
    with db() as conn:
        conn.executemany("DELETE FROM expenses WHERE id = ?", [(r[0],) for r in picked])
    removed = "\n".join(f"#{x} {escape(r[1])} - {fmt_amount(r[2])} ({r[3]})" for x, r in zip(nums, picked))
    return show(user_id, view, f"🗑 Removed:\n{removed}")


def clear(user_id, args):
    today = now().date()
    month = args[:1] == ["month"]
    start = today.replace(day=1) if month else today
    label = "this month's" if month else "today's"
    rows = rows_for(user_id, start.isoformat(), today.isoformat())
    if not rows:
        return [f"Nothing to clear, {label} table is already empty."]
    if args[-1:] != ["yes"]:
        cmd = "/clear month yes" if month else "/clear yes"
        return [f"This deletes all {len(rows)} of {label} entries.\nSend <code>{cmd}</code> to confirm."]
    with db() as conn:
        conn.execute("DELETE FROM expenses WHERE user_id = ? AND spent_on BETWEEN ? AND ?",
                     (user_id, start.isoformat(), today.isoformat()))
    return [f"🗑 Cleared {len(rows)} of {label} entries."]


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
    return show(user_id, "today", "\n\n".join(msg))


def handle(user_id, text):
    """Return the HTML reply for one incoming text message, as a list of messages to send in order."""
    cmd = text.strip().split()[0].split("@")[0].lower() if text.strip().startswith("/") else None
    if cmd in ("/start", "/help"):
        return [f"Hi! Your Telegram user ID is <code>{user_id}</code>.\n\n{HELP}"]
    if not allowed(user_id):
        return ["Sorry, this bot is private."]
    if cmd == "/today":
        return show(user_id, "today")
    if cmd == "/month":
        return show(user_id, "month")
    if cmd in ("/total", "/all"):
        return show(user_id, "all")
    if cmd == "/undo":
        return undo(user_id)
    args = text.split()[1:]
    if cmd in ("/del", "/delete"):
        return delete_rows(user_id, args)
    if cmd == "/clear":
        return clear(user_id, [a.lower() for a in args])
    if cmd:
        return ["Unknown command.\n\n" + HELP]
    return add_lines(user_id, text)
