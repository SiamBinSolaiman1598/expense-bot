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

COMMANDS = (
    "<b>Commands</b>\n\n"
    "<b>Add</b>\n"
    "<code>tea - 20</code> – add an expense (send one or many lines at once)\n\n"
    "<b>See</b>\n"
    "/month – this month's items and total (shown after every change)\n"
    "/day – today's items and total\n"
    "/total – every item and total till today\n\n"
    "<b>Edit</b>\n"
    "/edit 3 – change row #3 of the table you last looked at (then send e.g. <code>home - 100</code>)\n"
    "/edit 3 home - 100 – same, in one message\n"
    "/edit 3 100 – change only the amount\n"
    "/edit 3 Home to office – change only the name\n"
    "(several /edit lines in one message change several rows)\n\n"
    "<b>Delete</b>\n"
    "/undo – delete the last entry\n"
    "/del 3 – delete row #3 of the table you last looked at\n"
    "/del 2 5 – delete several rows at once\n"
    "/clear – empty today's table\n"
    "/clear month – empty this month's table\n\n"
    "<b>Help</b>\n"
    "/commands – this list\n"
    "/help – how to use the bot"
)

HELP = (
    "Send expenses as:\n<code>tea - 20</code>\n<code>bus fare - 45</code>\n"
    "(one or many lines per message)\n\n" + COMMANDS
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
            # An /edit waiting for the new "name - amount" message
            conn.execute(
                "CREATE TABLE IF NOT EXISTS pending_edit ("
                "user_id INTEGER PRIMARY KEY, expense_id INTEGER NOT NULL, created_at TEXT NOT NULL)"
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
    return row[0] if row else "month"


def set_pending_edit(user_id, expense_id):
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO pending_edit (user_id, expense_id, created_at) VALUES (?, ?, ?)",
                     (user_id, expense_id, now().isoformat(timespec="seconds")))


def pop_pending_edit(user_id):
    """Return the expense id waiting to be edited (if asked in the last 10 minutes) and forget it."""
    with db() as conn:
        row = conn.execute("SELECT expense_id, created_at FROM pending_edit WHERE user_id = ?",
                           (user_id,)).fetchone()
        conn.execute("DELETE FROM pending_edit WHERE user_id = ?", (user_id,))
    if row and (now() - datetime.fromisoformat(row[1])).total_seconds() < 600:
        return row[0]
    return None


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
    return show(user_id, "month", f"Removed: {escape(row[1])} - {fmt_amount(row[2])}")


def delete_rows(user_id, args):
    view = get_last_view(user_id)
    rows = view_rows(user_id, view)
    label = VIEWS[view]["label"]
    nums = sorted({int(a) for a in args if a.isdigit()})
    if not nums or len(nums) != len(args) or not all(1 <= x <= len(rows) for x in nums):
        return [f"Send row numbers from {label} table (it has {len(rows)} rows), "
                "e.g. <code>/del 2</code> or <code>/del 2 5</code>.\n"
                "To delete from another table, open it first with /day, /month or /total."]
    picked = [rows[x - 1] for x in nums]
    with db() as conn:
        conn.executemany("DELETE FROM expenses WHERE id = ?", [(r[0],) for r in picked])
    removed = "\n".join(f"#{x} {escape(r[1])} - {fmt_amount(r[2])} ({r[3]})" for x, r in zip(nums, picked))
    return show(user_id, view, f"🗑 Removed:\n{removed}")


EDIT_RE = re.compile(r"\s*/edit(?:@\S+)?\s+(\d+)\s*(.*?)\s*$", re.I)


def start_edit(user_id, text):
    """Each line "/edit 3 home - 100" changes row #3 of the last table; "/edit 3" alone asks for the value."""
    view = get_last_view(user_id)
    rows = view_rows(user_id, view)
    lines = [l for l in text.splitlines() if l.strip()]
    usage = (f"Send a row number from {VIEWS[view]['label']} table (it has {len(rows)} rows), "
             "e.g. <code>/edit 2</code> or <code>/edit 2 tea - 40</code>.\n"
             "To edit another day, open its table first with /month or /total.")

    if len(lines) == 1:
        m = EDIT_RE.match(lines[0])
        if not m or not 1 <= int(m.group(1)) <= len(rows):
            return [usage]
        num, new = int(m.group(1)), m.group(2)
        if not new:
            expense_id, name, amount, spent_on = rows[num - 1]
            set_pending_edit(user_id, expense_id)
            return [f"✏️ Editing #{num}: {escape(name)} - {fmt_amount(amount)} ({spent_on})\n\n"
                    "Send the new value, e.g. <code>tea - 40</code>\n"
                    "or just a name or just a number to change only that.\n/cancel to keep it as it is."]

    # Row numbers refer to the table as it was before this message, so they don't shift between lines
    notes, edited = [], 0
    for line in lines:
        m = EDIT_RE.match(line)
        if not m or not m.group(2) or not 1 <= int(m.group(1)) <= len(rows):
            notes.append(f"⚠️ Skipped <code>{escape(line.strip())}</code>")
            continue
        notes.append(f"#{m.group(1)} " + update_expense(user_id, rows[int(m.group(1)) - 1][0], m.group(2)))
        edited += 1
    if not edited:
        return ["\n".join(notes) + "\n\n" + usage]
    return show(user_id, view, "✏️ Edited:\n" + "\n".join(notes))


def apply_edit(user_id, expense_id, text):
    return show(user_id, get_last_view(user_id), "✏️ Edited: " + update_expense(user_id, expense_id, text))


def update_expense(user_id, expense_id, text):
    """Change one entry. Text can be "name - amount", just an amount, or just a name. Returns "old → new"."""
    with db() as conn:
        old = conn.execute("SELECT name, amount FROM expenses WHERE id = ? AND user_id = ?",
                           (expense_id, user_id)).fetchone()
        if not old:
            return "that entry no longer exists"
        text = text.strip()
        if re.fullmatch(r"\d+(?:\.\d+)?", text):
            name, amount = old[0], float(text)
        elif m := LINE_RE.match(text):
            name, amount = m.group(1).strip(), float(m.group(2))
        else:
            name, amount = text, old[1]
        conn.execute("UPDATE expenses SET name = ?, amount = ? WHERE id = ?", (name, amount, expense_id))
    return f"{escape(old[0])} - {fmt_amount(old[1])} → {escape(name)} - {fmt_amount(amount)}"


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
    return show(user_id, "month", "\n\n".join(msg))


def handle(user_id, text):
    """Return the HTML reply for one incoming text message, as a list of messages to send in order."""
    cmd = text.strip().split()[0].split("@")[0].lower() if text.strip().startswith("/") else None
    if cmd in ("/start", "/help"):
        return [f"Hi! Your Telegram user ID is <code>{user_id}</code>.\n\n{HELP}"]
    if cmd == "/commands":
        return [COMMANDS]
    if not allowed(user_id):
        return ["Sorry, this bot is private."]
    # Any message after "/edit 3" is the new value; a command cancels the edit instead
    editing = pop_pending_edit(user_id)
    if editing and not cmd:
        return apply_edit(user_id, editing, text)
    if cmd == "/cancel":
        return ["Edit cancelled." if editing else "Nothing to cancel."]
    if cmd == "/edit":
        return start_edit(user_id, text)
    if cmd in ("/day", "/today"):
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
