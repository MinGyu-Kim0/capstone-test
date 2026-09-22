"""SQLite persistence and recording integration; all provider responses are fake."""

import asyncio
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
from note_store import NoteStore
from test_app import Browser, Speech, Summarizer


class NoteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = NoteStore(Path(directory.name) / "notes.sqlite3")

    async def record(self, *, fail_final=False, disconnect=False):
        browser, speech, model = Browser(), Speech(), Summarizer(fail_final=fail_final)
        session = main.Session(browser, speech, model, 16000, note_store=self.store,
                               course=main.CourseSettings(name="일반물리학", context="뉴턴 역학", terms=["momentum"]))
        task = asyncio.create_task(session.run())
        try:
            await browser.incoming.put({"type": "websocket.receive", "bytes": b"\0\0"})
            await browser.until("summary_done", "live")
            if disconnect:
                await browser.incoming.put({"type": "websocket.disconnect", "code": 1000})
                with self.assertRaises(main.WebSocketDisconnect):
                    await asyncio.wait_for(task, 5)
            else:
                await browser.incoming.put({"type": "websocket.receive", "text": '{"type":"stop"}'})
                complete = await browser.until("complete")
                self.assertEqual(complete["final_ok"], not fail_final)
                await asyncio.wait_for(task, 5)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return session

    async def test_all_three_outputs_survive_new_process_and_recordings_are_separate(self):
        first = await self.record()
        second = await self.record()
        self.assertNotEqual(first.note_id, second.note_id)
        expected = self.store.get(first.note_id)
        self.assertEqual(expected["transcript"], "회의는 화요일입니다.\n담당자는 민수입니다.")
        self.assertEqual(expected["chunks"], [{"id": 1, "text": "- 화요일 회의", "status": "done", "source_text": "회의는 화요일입니다."}])
        self.assertEqual(expected["final"], "- 화요일 회의")
        self.assertEqual(expected["course"]["terms"], ["momentum"])
        output = await asyncio.to_thread(subprocess.check_output, [
            sys.executable, "-B", "-c",
            "import json, sys; from note_store import NoteStore; print(json.dumps(NoteStore(sys.argv[1]).get(sys.argv[2])))",
            str(self.store.path), first.note_id,
        ], cwd=main.ROOT)
        self.assertEqual(json.loads(output), expected, "a new process reads all results from the file")
        items = self.store.list()
        self.assertEqual([item["id"] for item in items], [second.note_id, first.note_id])
        self.assertTrue(all("transcript" not in item and "chunks" not in item for item in items))

    async def test_failed_final_and_disconnect_keep_source_and_live_notes(self):
        failed = await self.record(fail_final=True)
        disconnected = await self.record(disconnect=True)
        for session in (failed, disconnected):
            record = self.store.get(session.note_id)
            self.assertTrue(record["transcript"])
            self.assertEqual(record["chunks"][0]["text"], "- 화요일 회의")
            self.assertEqual(record["final"], "")

    async def test_database_failure_is_reported_and_later_save_recovers(self):
        browser = Browser()
        session = main.Session(browser, Speech(), Summarizer(), 16000, note_store=self.store)
        session.transcript = "확정 원문"
        with patch.object(self.store, "save", side_effect=sqlite3.OperationalError("disk full")):
            await session.send({"type": "transcript", "delta": "확정 원문", "partial": ""})
        self.assertEqual((await browser.outgoing.get())["type"], "storage_error")
        self.assertEqual((await browser.outgoing.get())["type"], "transcript")
        await session.send({"type": "summary_done", "kind": "final", "text": "최종 노트"})
        self.assertEqual((await browser.outgoing.get())["type"], "note_saved")
        self.assertEqual(self.store.get(session.note_id)["final"], "최종 노트")

    async def test_cancelled_write_finishes_before_newer_checkpoint(self):
        session = main.Session(Browser(), Speech(), Summarizer(), 16000, note_store=self.store)
        session.transcript = "앞부분"
        entered, release = threading.Event(), threading.Event()
        save = self.store.save

        def slow_save(record):
            entered.set()
            release.wait(3)
            save(record)

        with patch.object(self.store, "save", side_effect=slow_save):
            task = asyncio.create_task(session.persist(notify=False))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                task.cancel()
                await asyncio.sleep(.02)
                self.assertFalse(task.done())
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        session.transcript += " 뒷부분"
        session.final_note = "전체 최종 노트"
        await session.persist(notify=False)
        self.assertEqual(self.store.get(session.note_id)["transcript"], "앞부분 뒷부분")


class ArchiveRouteTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = NoteStore(Path(directory.name) / "notes.sqlite3")
        self.record = {
            "id": "00000000-0000-4000-8000-000000000001", "createdAt": "2026-09-23T00:00:00+00:00",
            "course": {"name": "물리", "context": "힘", "terms": ["force"]}, "transcript": "DB에 저장된 원문",
            "chunks": [{"id": 1, "text": "실시간 요약", "status": "done"}], "final": "", "duration": 90,
        }
        self.store.save(self.record)
        patcher = patch.object(main, "NOTE_STORE", self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_list_detail_and_retry_update_same_note_using_database_source(self):
        model = Summarizer()
        with patch.dict("os.environ", {"OPENAI_API_KEY": "fake"}), patch.object(main, "AsyncOpenAI", return_value=model):
            with TestClient(main.app, base_url="http://localhost") as client:
                self.assertEqual(len(client.get("/api/notes").json()), 1)
                loaded = client.get("/api/notes/" + self.record["id"]).json()
                self.assertEqual({key: loaded[key] for key in self.record}, self.record)
                self.assertTrue(loaded["subject_id"])
                response = client.post("/api/summary/final", json={"note_id": self.record["id"], "transcript": "DB에 저장된"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(json.loads(model.calls[-1]["input"])["transcript"], "DB에 저장된 원문")
                self.assertEqual(response.json()["note"]["transcript"], "DB에 저장된 원문")
                self.assertEqual(response.json()["note"]["chunks"], self.record["chunks"])
            with TestClient(main.app, base_url="http://localhost") as client:
                saved = client.get("/api/notes/" + self.record["id"]).json()
                self.assertEqual(saved["final"], "- 화요일 회의")
                self.assertEqual(saved["chunks"], self.record["chunks"])
                self.assertEqual(len(client.get("/api/notes").json()), 1)
        # A late checkpoint containing no final result cannot erase a successful retry.
        self.store.save(self.record)
        self.assertEqual(self.store.get(self.record["id"])["final"], "- 화요일 회의")

    def test_retry_saves_newer_browser_source_and_chunks_before_summarizing(self):
        model = Summarizer()
        latest = self.record["transcript"] + " 마지막 정정 내용"
        chunk = {"id": 2, "text": "정정 요약", "status": "done"}
        with patch.dict("os.environ", {"OPENAI_API_KEY": "fake"}), patch.object(main, "AsyncOpenAI", return_value=model):
            with TestClient(main.app, base_url="http://localhost") as client:
                response = client.post("/api/summary/final", json={"note_id": self.record["id"], "transcript": latest, "chunks": [chunk]})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(json.loads(model.calls[-1]["input"])["transcript"], latest)
                conflict = client.post("/api/summary/final", json={"note_id": self.record["id"], "transcript": "다른 노트의 내용"})
                self.assertEqual(conflict.status_code, 409)
        self.store.save(self.record)  # Late cleanup must not erase the retry's newer source or chunks.
        saved = self.store.get(self.record["id"])
        self.assertEqual(saved["transcript"], latest)
        self.assertEqual(saved["chunks"], self.record["chunks"] + [chunk])
        self.assertEqual(saved["final"], "- 화요일 회의")

    def test_retry_recovers_a_record_whose_initial_database_write_failed(self):
        note_id = "00000000-0000-4000-8000-000000000002"
        with patch.dict("os.environ", {"OPENAI_API_KEY": "fake"}), patch.object(main, "AsyncOpenAI", return_value=Summarizer()):
            with TestClient(main.app, base_url="http://localhost") as client:
                response = client.post("/api/summary/final", json={
                    "note_id": note_id, "transcript": self.record["transcript"], "chunks": self.record["chunks"],
                    "created_at": self.record["createdAt"], "course": self.record["course"], "duration": 90,
                })
                self.assertEqual(response.status_code, 200)
                self.assertEqual(client.get("/api/notes/" + note_id).json()["chunks"], self.record["chunks"])
                self.assertTrue(self.store.get(note_id)["final"])

    def test_late_incomplete_chunks_add_missing_sources_without_regressing_summaries(self):
        cases = [(status, path) for status in ("done", "incomplete") for path in ("checkpoint", "retry")]
        with patch.dict("os.environ", {"OPENAI_API_KEY": "fake"}), patch.object(main, "AsyncOpenAI", side_effect=lambda **_: Summarizer()):
            with TestClient(main.app, base_url="http://localhost") as client:
                for index, (status, path) in enumerate(cases, start=2):
                    with self.subTest(status=status, path=path):
                        original = {"id": 1, "text": "완료된 요약" if status == "done" else "", "status": status}
                        record = {**self.record, "id": f"00000000-0000-4000-8000-{index:012d}", "chunks": [original]}
                        self.store.save(record)
                        late_chunk = {"id": 1, "text": "", "status": "incomplete", "source_text": record["transcript"]}
                        if path == "checkpoint":
                            self.store.save({**record, "chunks": [late_chunk]})
                        else:
                            response = client.post("/api/summary/final", json={"note_id": record["id"], "transcript": record["transcript"], "chunks": [late_chunk]})
                            self.assertEqual(response.status_code, 200)
                        expected = [{**original, "source_text": record["transcript"]}]
                        self.assertEqual(self.store.get(record["id"])["chunks"], expected)
                        self.store.save({**record, "chunks": [{**late_chunk, "source_text": ""}]})
                        self.assertEqual(self.store.get(record["id"])["chunks"], expected)

    def test_source_change_during_retry_cannot_attach_an_outdated_final(self):
        model = Summarizer()
        create = model.create

        async def append_source(**kwargs):
            latest = {**self.record, "transcript": self.record["transcript"] + " 늦게 확정된 문장"}
            self.store.save(latest)
            return await create(**kwargs)

        model.create = append_source
        with patch.dict("os.environ", {"OPENAI_API_KEY": "fake"}), patch.object(main, "AsyncOpenAI", return_value=model):
            with TestClient(main.app, base_url="http://localhost") as client:
                response = client.post("/api/summary/final", json={"note_id": self.record["id"], "transcript": self.record["transcript"]})
                self.assertEqual(response.status_code, 409)
                saved = self.store.get(self.record["id"])
                self.assertTrue(saved["transcript"].endswith("늦게 확정된 문장"))
                self.assertEqual(saved["final"], "")

    def test_new_transcript_invalidates_a_final_from_an_older_checkpoint(self):
        self.store.save_final(self.record["id"], "이전 최종 노트", self.record["transcript"])
        self.store.save({**self.record, "transcript": self.record["transcript"] + " 새 문장"})
        self.assertEqual(self.store.get(self.record["id"])["final"], "")

    def test_failed_retry_of_longer_source_does_not_keep_the_old_final(self):
        self.store.save_final(self.record["id"], "이전 최종 노트", self.record["transcript"])
        with patch.dict("os.environ", {"OPENAI_API_KEY": "fake"}), patch.object(main, "AsyncOpenAI", return_value=Summarizer(fail_final=True)):
            with TestClient(main.app, base_url="http://localhost") as client:
                response = client.post("/api/summary/final", json={"note_id": self.record["id"], "transcript": self.record["transcript"] + " 추가 발언"})
                self.assertEqual(response.status_code, 502)
                saved = self.store.get(self.record["id"])
                self.assertTrue(saved["transcript"].endswith("추가 발언"))
                self.assertEqual(saved["final"], "")

    def test_missing_note_and_database_failure_are_explicit_errors(self):
        with TestClient(main.app, base_url="http://localhost") as client:
            self.assertEqual(client.get("/api/notes/00000000-0000-4000-8000-000000000099").status_code, 404)
            self.assertEqual(client.get("/api/notes/invalid").status_code, 422)
            with patch.object(self.store, "list", side_effect=sqlite3.OperationalError("private path")):
                response = client.get("/api/notes")
                self.assertEqual(response.status_code, 503)
                self.assertNotIn("private path", response.text)
