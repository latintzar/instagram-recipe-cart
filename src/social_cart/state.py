"""Private, durable webhook inbox. Unknown external outcomes are never retried automatically."""
import json
import os
import sqlite3
from pathlib import Path

def connect(path: str):
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(target, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.fchmod(fd, 0o600)
    os.close(fd)
    db = sqlite3.connect(target, timeout=30)
    db.execute("CREATE TABLE IF NOT EXISTS jobs (mid TEXT PRIMARY KEY, sender TEXT NOT NULL, event TEXT NOT NULL, status TEXT NOT NULL, result TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS carts (owner TEXT NOT NULL, shop TEXT NOT NULL, cart_id TEXT NOT NULL, PRIMARY KEY(owner,shop))")
    db.commit()
    return db

def enqueue(db, event):
    mid = event.get("message", {}).get("mid")
    sender = event.get("sender", {}).get("id")
    message = event.get("message", {})
    if not isinstance(mid, str) or not mid or len(mid) > 256 or not isinstance(sender, str) or not sender.isdigit() or message.get("is_echo"):
        return False
    body = json.dumps({"entry":[{"messaging":[event]}]}, separators=(",", ":"))
    inserted = db.execute("INSERT OR IGNORE INTO jobs(mid,sender,event,status) VALUES(?,?,?,'pending')", (mid, sender, body)).rowcount == 1
    db.commit()
    return inserted

def claim(db):
    db.execute("BEGIN IMMEDIATE")
    row = db.execute("""SELECT j.mid,j.event FROM jobs j WHERE j.status='pending'
                     AND NOT EXISTS (SELECT 1 FROM jobs active WHERE active.sender=j.sender
                                     AND active.status IN ('running','uncertain'))
                     ORDER BY j.rowid LIMIT 1""").fetchone()
    if row:
        db.execute("UPDATE jobs SET status='running' WHERE mid=?", (row[0],))
    db.commit()
    return (row[0], json.loads(row[1])) if row else None

def mark(db, mid, status, result=None):
    if status not in {"failed", "uncertain", "done"}:
        raise ValueError("invalid status")
    if result is None:
        db.execute("UPDATE jobs SET status=? WHERE mid=?", (status, mid))
    else:
        db.execute("UPDATE jobs SET status=?,result=? WHERE mid=?", (status, json.dumps(result), mid))
    db.commit()

def cart_for(db, owner, shop):
    row = db.execute("SELECT cart_id FROM carts WHERE owner=? AND shop=?", (owner, shop)).fetchone()
    return row[0] if row else None

def remember_cart(db, owner, shop, cart_id):
    db.execute("INSERT OR REPLACE INTO carts(owner,shop,cart_id) VALUES(?,?,?)", (owner, shop, cart_id))
    db.commit()
