"""Selectable model routing, history migration, and regeneration checks with fake APIs."""

import itertools
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
from note_store import NoteStore
from test_app import Speech, Summarizer


class ModelSelectionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "notes.sqlite3"
        self.store = NoteStore(self.path)
        patcher = patch.object(main, "NOTE_STORE", self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_all_four_live_final_combinations_reach_provider_and_sqlite(self):
        for live, final in itertools.product(main.SUMMARY_MODELS, repeat=2):
            retry_live = (live, final) == ("gpt-6-sol", "gpt-6-luna")
            model = Summarizer(fail_live_once=retry_live)
            with self.subTest(live=live, final=final), patch.dict("os.environ", {"SONIOX_API_KEY": "fake", "OPENAI_API_KEY": "fake"}), patch.object(main, "connect", return_value=Speech()), patch.object(main, "AsyncOpenAI", return_value=model), patch.object(main, "LIVE_RETRY_DELAY", .01):
                with TestClient(main.app, base_url="http://localhost") as client:
                    with client.websocket_connect("ws://localhost/ws/session") as ws:
                        ws.send_json({"type": "start", "sample_rate": 16000, "live_model": live, "final_model": final})
                        ready = ws.receive_json()
                        self.assertEqual((ready["live_model"], ready["final_model"]), (live, final))
                        ws.send_bytes(b"\0\0")
                        while True:
                            event = ws.receive_json()
                            if event["type"] == "summary_done" and event["kind"] == "live":
                                self.assertEqual(event["model"], live)
                                break
                        ws.send_json({"type": "stop"})
                        while True:
                            event = ws.receive_json()
                            if event["type"] == "complete":
                                self.assertTrue(event["final_ok"])
                                break
                    saved = client.get("/api/notes/" + ready["note_id"]).json()
                    self.assertEqual((saved["live_model"], saved["final_model"]), (live, final))
                    self.assertTrue(saved["chunks"] and saved["final"])
                    self.assertEqual([call["model"] for call in model.calls], [live] * (2 if retry_live else 1) + [final])
        self.assertEqual(len(self.store.list()), 4)

    def test_existing_model_settings_select_supported_successors(self):
        self.assertEqual(main.SUMMARY_MODELS, ("gpt-6-sol", "gpt-6-luna"))
        for old, new in {"gpt-5.6-sol": "gpt-6-sol", "gpt-5.6-terra": "gpt-6-sol", "gpt-5.6-luna": "gpt-6-luna"}.items():
            self.assertEqual(main.configured_summary_model(old, "gpt-6-luna"), new)
            self.assertEqual(main.configured_summary_model(new, "gpt-6-sol"), new)
        for invalid in (None, "", "unapproved-model"):
            for default in main.SUMMARY_MODELS:
                self.assertEqual(main.configured_summary_model(invalid, default), default)

    def test_invalid_models_are_rejected_before_api_calls(self):
        with patch.dict("os.environ", {"SONIOX_API_KEY": "fake", "OPENAI_API_KEY": "fake"}), patch.object(main, "connect") as connect, patch.object(main, "AsyncOpenAI") as model:
            with TestClient(main.app, base_url="http://localhost") as client:
                self.assertEqual(client.get("/api/config").json()["summary_models"], list(main.SUMMARY_MODELS))
                for invalid in ("unapproved-model", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-6-terra"):
                    for field in ("live_model", "final_model"):
                        with client.websocket_connect("ws://localhost/ws/session") as ws:
                            ws.send_json({"type": "start", "sample_rate": 16000, field: invalid})
                            self.assertTrue(ws.receive_json()["fatal"])
                    response = client.post("/api/summary/final", json={"transcript": "원문", "model": invalid})
                    self.assertEqual(response.status_code, 422)
                    response = client.post("/api/subjects/00000000-0000-4000-8000-000000000001/summary", json={"model": invalid})
                    self.assertEqual(response.status_code, 422)
                model.assert_not_called()
                connect.assert_not_called()

    def test_regeneration_replaces_model_atomically_and_survives_late_checkpoint(self):
        record = {"id": "00000000-0000-4000-8000-000000000001", "createdAt": "2026-09-23T00:00:00Z",
                  "course": {"name": "물리", "context": "", "terms": []}, "transcript": "확정 원문",
                  "chunks": [{"id": 1, "text": "실시간 요약", "status": "done", "source_text": "확정 원문"}], "final": "이전 노트", "duration": 1, "live_model": "gpt-5.6-luna", "final_model": "gpt-5.6-terra"}
        legacy_chunks = [{"id": 1, "text": "실시간 요약", "status": "done"}]
        self.store.save(record)
        for selected in main.SUMMARY_MODELS:
            model = Summarizer()
            with patch.dict("os.environ", {"OPENAI_API_KEY": "fake"}), patch.object(main, "AsyncOpenAI", return_value=model):
                with TestClient(main.app, base_url="http://localhost") as client:
                    response = client.post("/api/summary/final", json={"note_id": record["id"], "transcript": record["transcript"], "model": selected, "live_model": record["live_model"], "chunks": legacy_chunks})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["model"], selected)
                    self.assertEqual(response.json()["note"]["final_model"], selected)
                    self.assertEqual(model.calls[-1]["model"], selected)
            self.store.save({**record, "chunks": legacy_chunks})
            saved = self.store.get(record["id"])
            self.assertEqual(saved["final"], "- 화요일 회의")
            self.assertEqual(saved["final_model"], selected)
            self.assertEqual(saved["live_model"], "gpt-5.6-luna")
            self.assertEqual(saved["chunks"][0]["source_text"], "확정 원문", "old clients and late checkpoints cannot erase chunk sources")
        with patch.dict("os.environ", {"OPENAI_API_KEY": "fake"}), patch.object(main, "AsyncOpenAI", return_value=Summarizer(fail_final=True)):
            with TestClient(main.app, base_url="http://localhost") as client:
                response = client.post("/api/summary/final", json={"note_id": record["id"], "transcript": record["transcript"], "model": "gpt-6-sol"})
                self.assertEqual(response.status_code, 502)
                self.assertEqual(self.store.get(record["id"])["final_model"], "gpt-6-luna")

    def test_existing_database_is_migrated_without_relabeling_old_notes(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("""CREATE TABLE notes (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, title TEXT NOT NULL,
                course TEXT NOT NULL, transcript TEXT NOT NULL, chunks TEXT NOT NULL, chunk_count INTEGER NOT NULL,
                final TEXT NOT NULL, duration INTEGER NOT NULL)""")
            connection.execute("INSERT INTO notes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (
                "old-note", "2026-09-23T00:00:00Z", "기존 강의", json.dumps({"name": "기존 강의", "context": "", "terms": []}),
                "기존 원문", "[]", 0, "기존 최종 노트", 20,
            ))
        saved = self.store.get("old-note")
        self.assertEqual(saved["transcript"], "기존 원문")
        self.assertEqual(saved["final"], "기존 최종 노트")
        self.assertNotIn("final_model", saved)
        self.assertEqual(NoteStore(self.path).get("old-note"), saved, "migration is repeatable")
