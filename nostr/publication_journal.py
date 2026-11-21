# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Durable receipts for approved public article events.

Only already signed public events enter this journal. Opening it never
signs or sends anything. A deliberate retry can recover the exact event,
including its timestamp and source pin, after a window close or restart.
SQLite transactions keep independent windows/processes from losing receipts.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import time

from .events import verify_event


def publication_key(event: dict) -> str:
    """Identity of a payload, excluding retry-generated publication times."""
    tags = [tag for tag in event.get("tags", []) if tag[0] != "published_at"]
    payload = [event.get("pubkey"), event.get("kind"), tags, event.get("content")]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def coordinate(event: dict) -> str:
    identifiers = [tag[1] for tag in event.get("tags", [])
                   if len(tag) > 1 and tag[0] == "d"]
    if event.get("kind") != 30023 or len(identifiers) != 1 or not identifiers[0]:
        raise ValueError("publication journal requires an article address")
    return f"30023:{event['pubkey']}:{identifiers[0]}"


class PublicationJournal:
    """One receipt per signed event, scoped to its signing account."""

    def __init__(self, path: Path | None = None):
        if path is None:
            import user_dirs
            path = user_dirs.config_dir() / "article_publications.sqlite3"
        self.path = Path(path)

    @contextmanager
    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Unlike SQLite's default creation mode, this holds public-but-not-
        # yet-dispatched text readable only by this OS account.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("""CREATE TABLE IF NOT EXISTS publications (
            event_id TEXT PRIMARY KEY, address TEXT NOT NULL,
            payload_key TEXT NOT NULL, event TEXT NOT NULL,
            status TEXT NOT NULL, receipts TEXT NOT NULL DEFAULT '[]',
            added INTEGER NOT NULL, updated INTEGER NOT NULL)""")
        connection.execute("CREATE INDEX IF NOT EXISTS publication_address ON publications(address, added)")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def find(self, unsigned: dict) -> dict | None:
        """Newest attempt at this address, only if its payload still matches.

        A later different revision prevents an old completed event from
        intercepting an intentional revert to earlier text.
        """
        with self._connect() as db:
            row = db.execute("SELECT payload_key, event, status, receipts FROM publications "
                             "WHERE address=? ORDER BY added DESC, rowid DESC LIMIT 1",
                             (coordinate(unsigned),)).fetchone()
        if not row or row[0] != publication_key(unsigned):
            return None
        event = json.loads(row[1])
        if not verify_event(event):
            raise ValueError("damaged publication receipt")
        return {"event": event, "status": row[2], "receipts": json.loads(row[3])}

    def remember(self, event: dict) -> None:
        if not verify_event(event):
            raise ValueError("only valid signed events enter the publication journal")
        now = time.time_ns()
        with self._connect() as db:
            db.execute("INSERT OR IGNORE INTO publications "
                       "(event_id,address,payload_key,event,status,added,updated) VALUES (?,?,?,?,?,?,?)",
                       (event["id"], coordinate(event), publication_key(event),
                        json.dumps(event, ensure_ascii=False), "signed", now, now))

    def dispatching(self, event_id: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE publications SET status='unknown',updated=? "
                       "WHERE event_id=? AND status!='accepted'", (time.time_ns(), event_id))

    def accepted(self, event_id: str, relay: str) -> None:
        self.answered(event_id, [(relay, True, "")])

    def answered(self, event_id: str, results) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,receipts FROM publications WHERE event_id=?",
                             (event_id,)).fetchone()
            if row is None:
                raise ValueError("publication was not journaled before dispatch")
            receipts = {item[0]: item for item in json.loads(row[1])}
            for url, ok, message in results:
                # A later timeout never erases a positive acknowledgement.
                if url not in receipts or not receipts[url][1]:
                    receipts[url] = [url, bool(ok), message]
            status = "accepted" if row[0] == "accepted" or any(x[1] for x in receipts.values()) else "unknown"
            db.execute("UPDATE publications SET status=?,receipts=?,updated=? WHERE event_id=?",
                       (status, json.dumps(list(receipts.values())), time.time_ns(), event_id))

    def pending(self, owner: str) -> list[dict]:
        """Read recovery state for one account; do not resume dispatch."""
        with self._connect() as db:
            rows = db.execute("SELECT event,status,receipts FROM publications "
                              "WHERE address LIKE ? AND status!='accepted' ORDER BY added",
                              (f"30023:{owner}:%",)).fetchall()
        return [{"event": json.loads(row[0]), "status": row[1], "receipts": json.loads(row[2])}
                for row in rows]
