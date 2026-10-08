"""Only approved local context. No WhatsApp client or network calls."""
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

MESSAGES_DB_PATH = Path(os.environ.get("ARC_CONTEXT_DB", str(Path(__file__).resolve().parent.parent / "whatsapp-bridge/store/context.db"))).resolve()


def bounded(value, maximum=100):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"Expected integer between 0 and {maximum}")


def term(value):
    if value is not None and (not isinstance(value, str) or len(value) > 500):
        raise ValueError("Expected string of at most 500 characters")


@contextmanager
def database():
    conn = sqlite3.connect(Path(MESSAGES_DB_PATH).as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        policy = conn.execute("SELECT value FROM arc_meta WHERE key='policy'").fetchone()
        if policy is None or policy[0] != "read-history-v1":
            raise PermissionError("Unverified store; access denied")
        heartbeat = conn.execute("SELECT value FROM arc_meta WHERE key='heartbeat'").fetchone()
        age = time.time() - int(heartbeat[0]) if heartbeat else float("inf")
        if not 0 <= age <= 15:
            raise PermissionError("Connector not current; access denied")
        yield conn
    finally:
        conn.close()


def rows(conn, sql, values=()):
    return [dict(row) for row in conn.execute(sql, values)]


MESSAGE_SELECT = """SELECT m.id,m.chat_jid,m.sender,m.content,m.timestamp,m.is_from_me,
 c.name AS chat_name FROM arc_messages m JOIN arc_chats c ON c.jid=m.chat_jid
 WHERE c.eligible=1"""


def list_messages(after=None, before=None, sender_phone_number=None, chat_jid=None,
                  query=None, limit=20, page=0, include_context=True,
                  context_before=1, context_after=1):
    bounded(limit); bounded(page, 10000); bounded(context_before, 20); bounded(context_after, 20)
    sql = MESSAGE_SELECT
    values = []
    for value, predicate in ((chat_jid, "m.chat_jid=?"),
                              (sender_phone_number, "m.sender LIKE ?"), (query, "m.content LIKE ?")):
        term(value)
        if value is not None:
            sql += " AND " + predicate
            values.append(f"%{value}%" if "LIKE" in predicate else value)
    for value, operator in ((after, ">"), (before, "<")):
        term(value)
        if value:
            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if date.tzinfo is None:
                raise ValueError("Date must include a timezone")
            sql += f" AND m.timestamp {operator} ?"
            values.append(int(date.timestamp()))
    sql += " ORDER BY m.timestamp DESC,m.id DESC LIMIT ? OFFSET ?"
    values.extend((limit, limit * page))
    with database() as conn:
        matches = rows(conn, sql, values)
        if include_context:
            for message in matches:
                message["context"] = _context(conn, message["id"], message["chat_jid"], context_before, context_after)
        return matches


def _context(conn, message_id, chat_jid, before, after):
    target = rows(conn, MESSAGE_SELECT + " AND m.id=? AND m.chat_jid=?", (message_id, chat_jid))
    if not target:
        return None
    message = target[0]
    result = {"message": message}
    for label, operator, order, limit in (("before", "<", "DESC", before), ("after", ">", "ASC", after)):
        result[label] = rows(conn, MESSAGE_SELECT +
            f" AND m.chat_jid=? AND (m.timestamp,m.id) {operator} (?,?)"
            f" ORDER BY m.timestamp {order},m.id {order} LIMIT ?",
            (chat_jid, message["timestamp"], message_id, limit))
    result["before"].reverse()
    return result


def get_message_context(message_id, before=5, after=5):
    term(message_id); bounded(before, 20); bounded(after, 20)
    with database() as conn:
        targets = rows(conn, MESSAGE_SELECT + " AND m.id=?", (message_id,))
        if not targets:
            return None
        if len(targets) != 1:
            raise ValueError("Ambiguous ID; use list_messages with chat_jid")
        return _context(conn, message_id, targets[0]["chat_jid"], before, after)


def list_chats(query=None, limit=20, page=0, include_last_message=True, sort_by="last_active"):
    term(query); bounded(limit); bounded(page, 10000)
    if sort_by not in ("last_active", "name"):
        raise ValueError("Unsupported sort order")
    sql = """SELECT c.jid,c.name,c.eligible,
      CASE WHEN c.eligible=1 THEN (SELECT MAX(timestamp) FROM arc_messages WHERE chat_jid=c.jid)
      END AS last_message_time FROM arc_chats c"""
    values = []
    if query is not None:
        sql += " WHERE c.name LIKE ? OR c.jid LIKE ?"
        values += [f"%{query}%"] * 2
    sql += " ORDER BY " + ("name,jid" if sort_by == "name" else "last_message_time DESC,jid")
    sql += " LIMIT ? OFFSET ?"
    values += [limit, limit * page]
    with database() as conn:
        chats = rows(conn, sql, values)
        for chat in chats:
            _decorate(conn, chat, include_last_message)
        return chats


def _decorate(conn, chat, include_last_message):
    chat["access"] = "read_history" if chat.pop("eligible") else "blocked_unread_or_unknown"
    if include_last_message and chat["access"] == "read_history":
        messages = rows(conn, MESSAGE_SELECT + " AND m.chat_jid=? ORDER BY m.timestamp DESC,m.id DESC LIMIT 1", (chat["jid"],))
        chat["last_message"] = messages[0] if messages else None


def get_chat(chat_jid, include_last_message=True):
    term(chat_jid)
    with database() as conn:
        found = rows(conn, "SELECT jid,name,eligible FROM arc_chats WHERE jid=?", (chat_jid,))
        if not found:
            return None
        chat = found[0]
        _decorate(conn, chat, include_last_message)
        return chat


def search_contacts(query):
    return list_chats(query=query, include_last_message=False)


def get_direct_chat_by_contact(sender_phone_number):
    term(sender_phone_number)
    if not sender_phone_number or not sender_phone_number.isdecimal():
        raise ValueError("Expected phone number digits")
    return get_chat(sender_phone_number + "@s.whatsapp.net")


def get_contact_chats(jid, limit=20, page=0):
    term(jid); bounded(limit); bounded(page, 10000)
    with database() as conn:
        return rows(conn, """SELECT DISTINCT c.jid,c.name FROM arc_chats c
         JOIN arc_messages m ON m.chat_jid=c.jid WHERE c.eligible=1 AND
         (m.sender=? OR c.jid=?) ORDER BY c.jid LIMIT ? OFFSET ?""", (jid, jid, limit, limit*page))


def get_last_interaction(jid):
    term(jid)
    with database() as conn:
        result = rows(conn, MESSAGE_SELECT +
            " AND (m.sender=? OR m.chat_jid=?) ORDER BY m.timestamp DESC,m.id DESC LIMIT 1", (jid, jid))
        return result[0] if result else None
