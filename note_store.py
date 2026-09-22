"""SQLite recording archive. Each operation uses and closes its own connection."""

import json
import hashlib
import sqlite3
import threading
from datetime import datetime, timezone
from uuid import uuid4
from contextlib import closing, contextmanager
from pathlib import Path


class DeletedNoteError(LookupError):
    pass


class NoteStore:
    def __init__(self, path):
        self.path = Path(path)
        self._schema_lock = threading.Lock()
        self._schema_ready = False

    def ensure_schema(self, connection):
        with self._schema_lock:
            if self._schema_ready:
                return
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""CREATE TABLE IF NOT EXISTS notes (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL, title TEXT NOT NULL,
                course TEXT NOT NULL, transcript TEXT NOT NULL, chunks TEXT NOT NULL,
                chunk_count INTEGER NOT NULL, final TEXT NOT NULL, duration INTEGER NOT NULL
            )""")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(notes)")}
            for name in ("live_model", "final_model"):
                if name not in columns:
                    connection.execute(f"ALTER TABLE notes ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
            connection.execute("""CREATE TABLE IF NOT EXISTS subjects (
                id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '', summary_model TEXT NOT NULL DEFAULT '',
                revision INTEGER NOT NULL DEFAULT 0, summary_revision INTEGER NOT NULL DEFAULT -1,
                summary_updated_at TEXT
            )""")
            subject_columns = {row[1] for row in connection.execute("PRAGMA table_info(subjects)")}
            if "summary_document" not in subject_columns:
                connection.execute("ALTER TABLE subjects ADD COLUMN summary_document TEXT NOT NULL DEFAULT '{}'")
            if "summary_version" not in subject_columns:
                connection.execute("ALTER TABLE subjects ADD COLUMN summary_version INTEGER NOT NULL DEFAULT 0")
            connection.execute("CREATE TABLE IF NOT EXISTS deleted_notes (id TEXT PRIMARY KEY)")
            if "subject_id" not in columns:
                connection.execute("ALTER TABLE notes ADD COLUMN subject_id TEXT REFERENCES subjects(id)")
                for row in connection.execute("SELECT id, course, final FROM notes").fetchall():
                    name = json.loads(row["course"]).get("name", "").strip()
                    if name:
                        subject_id = self.ensure_subject(connection, name)
                        connection.execute("UPDATE notes SET subject_id = ? WHERE id = ?", (subject_id, row["id"]))
                connection.execute("UPDATE subjects SET revision = (SELECT COUNT(*) FROM notes WHERE subject_id = subjects.id AND final != '')")
            connection.execute("CREATE INDEX IF NOT EXISTS notes_subject ON notes(subject_id, created_at)")
            connection.execute("""CREATE TRIGGER IF NOT EXISTS subject_note_added AFTER INSERT ON notes
                WHEN NEW.final != '' BEGIN UPDATE subjects SET revision = revision + 1 WHERE id = NEW.subject_id; END""")
            connection.execute("""CREATE TRIGGER IF NOT EXISTS subject_note_changed AFTER UPDATE OF final, subject_id ON notes
                WHEN OLD.final != NEW.final OR OLD.subject_id IS NOT NEW.subject_id BEGIN
                UPDATE subjects SET revision = revision + 1 WHERE id = OLD.subject_id AND OLD.final != '';
                UPDATE subjects SET revision = revision + 1 WHERE id = NEW.subject_id AND NEW.final != ''
                    AND (OLD.final = '' OR OLD.subject_id IS NOT NEW.subject_id);
                END""")
            connection.execute("""CREATE TRIGGER IF NOT EXISTS subject_note_deleted AFTER DELETE ON notes
                WHEN OLD.final != '' BEGIN UPDATE subjects SET revision = revision + 1 WHERE id = OLD.subject_id; END""")
            connection.execute("CREATE INDEX IF NOT EXISTS notes_created_at ON notes(created_at DESC)")
            connection.commit()
            self._schema_ready = True

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=5)) as connection, connection:
            connection.row_factory = sqlite3.Row
            self.ensure_schema(connection)
            yield connection

    @staticmethod
    def metadata(record):
        return {
            "id": record["id"], "createdAt": record["createdAt"],
            "title": record["course"]["name"] or "강의 노트",
            "has_final": bool(record["final"]), "chunk_count": len(record["chunks"]),
            "subject_id": record.get("subject_id"),
        }

    @staticmethod
    def ensure_subject(connection, name):
        row = connection.execute("SELECT id FROM subjects WHERE name = ?", (name,)).fetchone()
        if row:
            return row["id"]
        subject_id = str(uuid4())
        connection.execute("INSERT INTO subjects (id, name, created_at) VALUES (?, ?, ?)",
                           (subject_id, name, datetime.now(timezone.utc).isoformat()))
        return subject_id

    def save(self, record):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM deleted_notes WHERE id = ?", (record["id"],)).fetchone():
                raise DeletedNoteError("Note deleted")
            record = dict(record)
            existing = connection.execute("SELECT transcript, chunks, final, duration, live_model, final_model, subject_id FROM notes WHERE id = ?", (record["id"],)).fetchone()
            if existing:
                # Disconnect cleanup and HTTP retry can overlap. Never regress a checkpoint.
                record = dict(record)
                record["live_model"] = existing["live_model"] or record.get("live_model", "")
                if len(existing["transcript"]) > len(record["transcript"]):
                    record["transcript"] = existing["transcript"]
                    record["final"] = existing["final"]
                    record["final_model"] = existing["final_model"]
                elif record["transcript"] == existing["transcript"]:
                    if existing["final"]:
                        # Final regeneration owns replacements; late session checkpoints cannot undo it.
                        record["final"] = existing["final"]
                        record["final_model"] = existing["final_model"]
                    elif not record["final"]:
                        record["final_model"] = existing["final_model"] or record.get("final_model", "")
                chunks = {chunk["id"]: chunk for chunk in json.loads(existing["chunks"])}
                for chunk in record["chunks"]:
                    previous = chunks.get(chunk["id"], {})
                    if chunk["id"] not in chunks or chunk["status"] == "done":
                        chunks[chunk["id"]] = {**previous, **chunk}
                    source = previous.get("source_text") or chunk.get("source_text")
                    if source:
                        chunks[chunk["id"]]["source_text"] = source
                record["chunks"] = sorted(chunks.values(), key=lambda chunk: chunk["id"])
                record["duration"] = max(record["duration"], existing["duration"])
                record["subject_id"] = existing["subject_id"]
            else:
                name = record["course"]["name"].strip()
                record["subject_id"] = self.ensure_subject(connection, name) if name else None
            connection.execute("""INSERT INTO notes (id, created_at, title, course, transcript, chunks,
                chunk_count, final, duration, live_model, final_model, subject_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET transcript=excluded.transcript,
                chunks=excluded.chunks, chunk_count=excluded.chunk_count,
                final=excluded.final,
                duration=excluded.duration, live_model=excluded.live_model, final_model=excluded.final_model""", (
                record["id"], record["createdAt"], record["course"]["name"] or "강의 노트",
                json.dumps(record["course"], ensure_ascii=False), record["transcript"],
                json.dumps(record["chunks"], ensure_ascii=False), len(record["chunks"]),
                record["final"], record["duration"],
                record.get("live_model", ""), record.get("final_model", ""),
                record["subject_id"],
            ))
        return self.metadata(record)

    def list(self):
        with self.connection() as connection:
            return [dict(row) for row in connection.execute("""SELECT id, created_at AS createdAt,
                title, (final != '') AS has_final, chunk_count, subject_id FROM notes ORDER BY created_at DESC, id DESC""")]

    def get(self, note_id):
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"], "createdAt": row["created_at"], "course": json.loads(row["course"]),
            "transcript": row["transcript"], "chunks": json.loads(row["chunks"]),
            "final": row["final"], "duration": row["duration"],
            **{key: row[key] for key in ("live_model", "final_model") if row[key]},
            **({"subject_id": row["subject_id"]} if row["subject_id"] else {}),
        }

    def save_final(self, note_id, text, expected_source, model=""):
        with self.connection() as connection:
            result = connection.execute("UPDATE notes SET final = ?, final_model = ? WHERE id = ? AND transcript = ?", (text, model, note_id, expected_source))
            if result.rowcount != 1:
                raise LookupError("Note source changed")

    def delete_note(self, note_id):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not connection.execute("SELECT 1 FROM notes WHERE id = ?", (note_id,)).fetchone():
                return False
            connection.execute("INSERT OR IGNORE INTO deleted_notes VALUES (?)", (note_id,))
            connection.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        return True

    def move_note(self, note_id, subject_id):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if subject_id and not connection.execute("SELECT 1 FROM subjects WHERE id = ?", (subject_id,)).fetchone():
                raise LookupError("Subject not found")
            if connection.execute("UPDATE notes SET subject_id = ? WHERE id = ?", (subject_id, note_id)).rowcount != 1:
                raise LookupError("Note not found")

    def create_subject(self, name):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            subject_id = self.ensure_subject(connection, name)
        return subject_id

    def list_subjects(self):
        with self.connection() as connection:
            return [dict(row) for row in connection.execute("""SELECT s.id, s.name,
                COUNT(n.id) AS note_count, COALESCE(SUM(n.final != ''), 0) AS completed_count,
                (s.summary != '') AS has_summary, s.revision, s.summary_revision,
                s.summary_model, s.summary_updated_at FROM subjects s LEFT JOIN notes n ON n.subject_id = s.id
                GROUP BY s.id ORDER BY s.name, s.id""")]

    def get_subject(self, subject_id, *, include_sources=False):
        with self.connection() as connection:
            connection.execute("BEGIN")
            row = connection.execute("SELECT * FROM subjects WHERE id = ?", (subject_id,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["summary_document"] = json.loads(result["summary_document"])
            result["notes"] = [dict(note) for note in connection.execute("""SELECT id, created_at AS createdAt,
                title, (final != '') AS has_final, final, chunk_count, subject_id FROM notes WHERE subject_id = ?
                ORDER BY created_at, id""", (subject_id,))]
            for note in result["notes"]:
                note["final_hash"] = hashlib.sha256(note.pop("final").encode()).hexdigest()
            # Resolve historical references in this same snapshot, including moved/deleted lectures.
            referenced = sorted({source["note_id"] for item in result["summary_document"].get("items", [])
                                 for event in item["history"] for source in event["sources"]})
            result["source_notes"] = []
            for offset in range(0, len(referenced), 400):
                ids = referenced[offset:offset + 400]
                placeholders = ",".join("?" for _ in ids)
                rows = connection.execute(f"""SELECT id, created_at AS createdAt, title,
                    (final != '') AS has_final, final, chunk_count, subject_id FROM notes WHERE id IN ({placeholders})""", ids)
                for source in rows:
                    source = dict(source)
                    source["final_hash"] = hashlib.sha256(source.pop("final").encode()).hexdigest()
                    result["source_notes"].append(source)
            if include_sources:
                result["sources"] = [dict(note) for note in connection.execute("""SELECT id, created_at AS createdAt,
                    title, final AS summary FROM notes WHERE subject_id = ? AND final != '' ORDER BY created_at, id""", (subject_id,))]
        return result

    def save_subject_summary(self, subject_id, text, model, revision, document, expected_version):
        with self.connection() as connection:
            result = connection.execute("""UPDATE subjects SET summary = ?, summary_model = ?,
                summary_revision = ?, summary_updated_at = ?, summary_document = ?, summary_version = summary_version + 1
                WHERE id = ? AND revision = ? AND summary_version = ?""",
                (text, model, revision, datetime.now(timezone.utc).isoformat(), json.dumps(document, ensure_ascii=False),
                 subject_id, revision, expected_version))
            if result.rowcount != 1:
                raise LookupError("Subject sources changed")

    def clear_subject_summary(self, subject_id):
        with self.connection() as connection:
            # Increase the revision so an in-flight generation cannot resurrect a deleted summary.
            result = connection.execute("""UPDATE subjects SET summary = '', summary_model = '', summary_revision = -1,
                summary_updated_at = NULL, summary_document = '{}', summary_version = summary_version + 1,
                revision = revision + 1 WHERE id = ?""", (subject_id,))
            return result.rowcount == 1

    def delete_subject(self, subject_id):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM notes WHERE subject_id = ? LIMIT 1", (subject_id,)).fetchone():
                raise ValueError("Subject is not empty")
            return connection.execute("DELETE FROM subjects WHERE id = ?", (subject_id,)).rowcount == 1
