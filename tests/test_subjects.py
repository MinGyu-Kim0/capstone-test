"""Subject folders, aggregate notes, deletion and source-version regression tests."""

import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
from note_store import DeletedNoteError, NoteStore
from test_app import Summarizer


class SubjectTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = NoteStore(Path(directory.name) / "notes.sqlite3")
        self.model = Summarizer()
        for patcher in [patch.object(main, "NOTE_STORE", self.store),
                        patch.object(main, "AsyncOpenAI", return_value=self.model),
                        patch.dict("os.environ", {"OPENAI_API_KEY": "fake"})]:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(main.app, base_url="http://localhost")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def note(self, name="물리", final="강의 최종 요약", day=1):
        record = {"id": str(uuid4()), "createdAt": f"2026-09-{day:02}T00:00:00Z",
                  "course": {"name": name, "context": "배경 설명", "terms": ["force"]},
                  "transcript": "PRIVATE RAW MUST NOT BE SENT", "chunks": [{"id": 1, "text": "PRIVATE LIVE", "status": "done"}],
                  "final": final, "duration": 60, "live_model": "gpt-5.6-luna", "final_model": "gpt-5.6-terra"}
        self.store.save(record)
        return self.store.get(record["id"])

    def aggregate(self, subject_id, model="gpt-6-sol"):
        return self.client.post(f"/api/subjects/{subject_id}/summary", json={"model": model})

    def test_folders_and_single_aggregate_preserve_lectures_and_only_use_final_summaries(self):
        first = self.note(final="FIRST FINAL", day=1)
        second = self.note(final="SECOND FINAL", day=2)
        self.note(final="", day=3)
        self.note("화학", "OTHER SUBJECT")
        subject_id = first["subject_id"]
        self.assertEqual(second["subject_id"], subject_id)
        folders = self.client.get("/api/subjects").json()
        self.assertEqual(len(folders), 2)
        subject = self.aggregate(subject_id).json()
        payload = json.loads(self.model.calls[-1]["input"])
        self.assertEqual([note["summary"] for note in payload["lecture_notes"]], ["FIRST FINAL", "SECOND FINAL"])
        self.assertNotIn("PRIVATE", self.model.calls[-1]["input"])
        self.assertNotIn("OTHER SUBJECT", self.model.calls[-1]["input"])
        self.assertEqual(subject["summary_model"], "gpt-6-sol")
        self.assertEqual(subject["revision"], subject["summary_revision"])
        self.assertEqual(len(subject["notes"]), 3)
        self.assertEqual(self.store.get(first["id"]), first)
        self.assertEqual(self.store.get(second["id"]), second)
        response = self.aggregate(subject_id, "gpt-6-luna")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["summary_model"], "gpt-6-luna")
        self.assertEqual(len(self.store.list_subjects()), 2)
        self.assertEqual(NoteStore(self.store.path).get_subject(subject_id)["summary"], response.json()["summary"])

    def test_create_empty_folder_and_reject_empty_summary_and_nonempty_folder_deletion(self):
        folder = self.client.post("/api/subjects", json={"name": "  새 과목  "}).json()
        duplicate = self.client.post("/api/subjects", json={"name": "새 과목"}).json()
        self.assertEqual(duplicate["id"], folder["id"])
        self.assertEqual(folder["notes"], [])
        self.assertEqual(self.aggregate(folder["id"]).status_code, 422)
        self.assertEqual(self.model.calls, [])
        self.assertEqual(self.client.delete("/api/subjects/" + folder["id"]).status_code, 200)
        note = self.note()
        self.assertEqual(self.client.delete("/api/subjects/" + note["subject_id"]).status_code, 409)
        self.assertIsNotNone(self.store.get(note["id"]))

    def test_note_delete_invalidates_aggregate_and_blocks_late_save_and_retry_resurrection(self):
        note = self.note()
        subject_id = note["subject_id"]
        self.assertEqual(self.aggregate(subject_id).status_code, 200)
        self.assertEqual(self.client.delete("/api/notes/" + note["id"]).status_code, 200)
        self.assertEqual(self.client.get("/api/notes/" + note["id"]).status_code, 404)
        with self.assertRaises(DeletedNoteError):
            self.store.save(note)
        response = self.client.post("/api/summary/final", json={"note_id": note["id"], "transcript": note["transcript"],
            "course": note["course"], "created_at": note["createdAt"], "chunks": note["chunks"]})
        self.assertEqual(response.status_code, 404)
        subject = self.store.get_subject(subject_id)
        self.assertNotEqual(subject["revision"], subject["summary_revision"])
        self.assertTrue(subject["summary"])
        self.assertEqual(subject["notes"], [])

    def test_move_invalidates_both_folders_and_late_checkpoint_cannot_move_back(self):
        original = self.note()
        target_note = self.note("화학")
        old, target = original["subject_id"], target_note["subject_id"]
        self.aggregate(old)
        self.aggregate(target)
        response = self.client.patch(f"/api/notes/{original['id']}/subject", json={"subject_id": target})
        self.assertEqual(response.status_code, 200)
        self.store.save(original)
        moved = self.store.get(original["id"])
        self.assertEqual(moved["subject_id"], target)
        self.assertEqual(moved["course"], original["course"])
        for key in (old, target):
            subject = self.store.get_subject(key)
            self.assertNotEqual(subject["revision"], subject["summary_revision"])
        self.assertEqual(self.client.patch(f"/api/notes/{original['id']}/subject", json={"subject_id": None}).status_code, 200)
        self.store.save(original)
        self.assertNotIn("subject_id", self.store.get(original["id"]))

    def test_changes_and_deletion_during_generation_cannot_commit_outdated_summary(self):
        for operation in ("change", "delete", "clear_summary", "delete_folder"):
            with self.subTest(operation=operation):
                note = self.note(name=operation)
                subject_id = note["subject_id"]
                create = Summarizer().create

                async def mutate(**kwargs):
                    if operation == "change":
                        self.store.save_final(note["id"], "CHANGED FINAL", note["transcript"], "gpt-6-sol")
                    elif operation == "clear_summary":
                        self.store.clear_subject_summary(subject_id)
                    else:
                        self.store.delete_note(note["id"])
                        if operation == "delete_folder":
                            self.store.delete_subject(subject_id)
                    return await create(**kwargs)

                self.model.create = mutate
                self.assertEqual(self.aggregate(subject_id).status_code, 409)
                subject = self.store.get_subject(subject_id)
                self.assertTrue(subject is None or subject["summary"] == "")

    def test_summary_failure_keeps_previous_result_and_summary_only_delete_keeps_lectures(self):
        note = self.note()
        subject_id = note["subject_id"]
        previous = self.aggregate(subject_id).json()
        self.model.fail_final = True
        self.assertEqual(self.aggregate(subject_id).status_code, 502)
        self.assertEqual(self.store.get_subject(subject_id)["summary"], previous["summary"])
        response = self.client.delete(f"/api/subjects/{subject_id}/summary")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["summary"], "")
        self.assertEqual(self.store.get(note["id"]), note)

    def test_large_subject_batches_include_every_final_summary_without_raw_transcripts(self):
        first_text, second_text = "A" * 80_000 + "END_ONE", "B" * 80_000 + "END_TWO"
        first = self.note(final=first_text)
        second = self.note(final=second_text, day=2)
        self.assertEqual(self.aggregate(first["subject_id"]).status_code, 200)
        generation = [call for call in self.model.calls if "annotation_layout" not in json.loads(call["input"])]
        self.assertEqual(len(generation), 3)
        pieces = {first["id"]: [], second["id"]: []}
        for call in generation[:-1]:
            payload = json.loads(call["input"])
            self.assertNotIn("PRIVATE", call["input"])
            for item in payload["lecture_notes"]:
                pieces[item["id"]].append(item["summary"])
        self.assertEqual("".join(pieces[first["id"]]), first_text)
        self.assertEqual("".join(pieces[second["id"]]), second_text)

    def test_active_recording_and_cross_origin_mutations_are_rejected(self):
        note = self.note()
        main.ACTIVE_RECORDINGS.add(note["id"])
        self.addCleanup(main.ACTIVE_RECORDINGS.discard, note["id"])
        self.assertEqual(self.client.delete("/api/notes/" + note["id"]).status_code, 409)
        self.assertEqual(self.client.patch(f"/api/notes/{note['id']}/subject", json={"subject_id": None}).status_code, 409)
        response = self.client.delete("/api/notes/" + note["id"], headers={"origin": "https://outside.example"})
        self.assertEqual(response.status_code, 403)
        main.ACTIVE_SUBJECT_SUMMARIES.add(note["subject_id"])
        self.addCleanup(main.ACTIVE_SUBJECT_SUMMARIES.discard, note["subject_id"])
        self.assertEqual(self.aggregate(note["subject_id"]).status_code, 409)
