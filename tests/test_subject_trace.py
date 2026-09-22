"""The prose must remain unchanged when provenance is attached in a separate pass."""

import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import main
import test_subjects
from note_store import NoteStore
from subject_trace import previous_note
from test_app import Stream
from trace_fakes import subject_draft, subject_response, subject_text


class TraceTests(unittest.TestCase):
    setUp = test_subjects.SubjectTests.setUp
    note = test_subjects.SubjectTests.note
    aggregate = test_subjects.SubjectTests.aggregate

    def respond(self, annotation=None, body=None):
        async def create(**kwargs):
            self.model.calls.append(kwargs)
            payload = json.loads(kwargs['input'])
            if 'annotation_layout' in payload and annotation is not None:
                result = annotation(payload) if callable(annotation) else annotation
                text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
            elif 'annotation_layout' not in payload and body is not None:
                text = body
            else:
                text = subject_response(payload)
            return Stream([{'type': 'response.output_text.delta', 'delta': text}, {'type': 'response.completed'}])
        self.model.create = create

    def test_prose_is_generated_without_trace_constraints_and_stored_verbatim(self):
        first = self.note(final='속도는 위치의 변화율이다.')
        subject_id = first['subject_id']
        result = self.aggregate(subject_id).json()
        self.assertEqual(len(self.model.calls), 2)
        prose, annotation = self.model.calls
        self.assertNotIn('text', prose)
        self.assertEqual(prose['max_output_tokens'], 6000)
        self.assertEqual(prose['reasoning'], {'effort': 'low'})
        prose_input = json.loads(prose['input'])
        self.assertNotIn('previous_note', prose_input)
        self.assertNotIn('annotation_layout', prose_input)
        body = subject_text(prose_input)
        self.assertEqual(result['summary'], body)
        self.assertEqual(result['summary_document']['body'], body)
        self.assertEqual(self.store.get_subject(subject_id)['summary'], body)
        self.assertTrue(annotation['text']['format']['strict'])
        self.assertNotIn('text', annotation['text']['format']['schema']['$defs']['AnnotationItem']['properties'])
        item = result['summary_document']['items'][0]
        self.assertEqual(body[item['start']:item['end']], item['text'])
        self.assertEqual(item['id'], 'N0001')
        self.assertEqual(item['sources'][0]['note_id'], first['id'])
        self.assertEqual(item['sources'][0]['created_at'], first['createdAt'])
        self.assertEqual(len(item['sources'][0]['final_hash']), 64)
        self.assertTrue(result['summary_export'].startswith(body))
        self.assertIn('U0001', result['summary_export'])

    def test_additions_and_unchanged_regeneration_keep_ids_times_and_restart_trace(self):
        first = self.note(final='속도는 위치의 변화율이다.')
        subject_id = first['subject_id']
        original = self.aggregate(subject_id).json()['summary_document']['items'][0]
        second = self.note(final='가속도는 속도의 변화율이다.', day=2)
        result = self.aggregate(subject_id).json()
        document = result['summary_document']
        self.assertEqual(document['items'][0], original)
        self.assertEqual(document['items'][1]['id'], 'N0002')
        self.assertEqual(document['items'][1]['sources'][0]['note_id'], second['id'])
        self.assertEqual(document['items'][1]['history'][0]['id'], 'U0002')
        unchanged = self.aggregate(subject_id).json()
        self.assertEqual(unchanged['summary_document'], document)
        self.assertEqual(NoteStore(self.store.path).get_subject(subject_id)['summary_document'], document)
        payload = json.loads(self.model.calls[-1]['input'])
        self.assertNotIn('history', json.dumps(payload['previous_note']))
        self.assertNotIn('previous_note', json.loads(self.model.calls[-2]['input']))

    def test_changed_prose_keeps_first_added_time_and_before_after_snapshots(self):
        note = self.note(final='시험은 월요일이다.')
        subject_id = note['subject_id']
        old = self.aggregate(subject_id).json()['summary_document']['items'][0]
        self.store.save_final(note['id'], '정정: 시험은 화요일이다.', note['transcript'])
        result = self.aggregate(subject_id).json()
        item = result['summary_document']['items'][0]
        self.assertEqual(item['id'], old['id'])
        self.assertEqual(item['created_at'], old['created_at'])
        self.assertEqual(item['history'][0], old['history'][0])
        self.assertEqual(item['history'][1]['kind'], 'updated')
        self.assertIn('월요일', item['history'][0]['text'])
        self.assertIn('화요일', item['history'][1]['text'])
        self.assertNotEqual(item['history'][0]['sources'][0]['final_hash'], item['sources'][0]['final_hash'])

    def test_missing_identity_is_recovered_once_per_repeated_paragraph(self):
        note = self.note(final='같은 설명이다.\n\n같은 설명이다.')
        before = self.aggregate(note['subject_id']).json()['summary_document']
        self.assertEqual(len(before['items']), 2)
        def null_ids(payload):
            result = subject_draft(payload)
            for part in result['parts']:
                part['id'] = None
                for item in part['items']:
                    item['id'] = None
            result['removed_ids'] = [item['id'] for item in before['items']]
            return result
        self.respond(annotation=null_ids)
        result = self.aggregate(note['subject_id'])
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['summary_document'], before)

    def test_repeated_headings_recover_part_ids_one_to_one(self):
        body = '핵심 개념\n\n같은 문장이다.\n\n핵심 개념\n\n같은 문장이다.'
        note = self.note(final=body)
        self.respond(body=body)
        before = self.aggregate(note['subject_id']).json()['summary_document']
        self.assertEqual(len(before['parts']), 2)
        def null_ids(payload):
            result = subject_draft(payload)
            for part in result['parts']:
                part['id'] = None
                for item in part['items']:
                    item['id'] = None
            result['removed_ids'] = [item['id'] for item in before['items']]
            return result
        self.respond(annotation=null_ids, body=body)
        result = self.aggregate(note['subject_id'])
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['summary_document'], before)

    def test_annotator_cannot_rewrite_prose_or_invent_sources_ids_and_positions(self):
        note = self.note(final='허용된 근거 문장이다.')
        other = self.note(name='다른 과목', final='다른 과목 근거이다.')
        subject_id = note['subject_id']
        before = self.aggregate(subject_id).json()
        payload = json.loads(self.model.calls[-1]['input'])
        payload['previous_note'] = previous_note(before['summary_document'])
        valid = subject_draft(payload)
        cases = []
        for citation in ({'note_id': other['id'], 'quote': other['final']}, {'note_id': note['id'], 'quote': '없는 문장'}, {'note_id': str(uuid4()), 'quote': note['final']}):
            bad = copy.deepcopy(valid)
            bad['parts'][0]['items'][0]['sources'] = [citation]
            cases.append(bad)
        for field, value in (('id', 'N9999'), ('key', 'block-does-not-exist'), ('text', '본문을 짧게 다시 썼다.'), ('created_at', '2000-01-01')):
            bad = copy.deepcopy(valid)
            bad['parts'][0]['items'][0][field] = value
            cases.append(bad)
        bad = copy.deepcopy(valid)
        bad['parts'][0]['items'].append(copy.deepcopy(bad['parts'][0]['items'][0]))
        cases.append(bad)
        bad = copy.deepcopy(valid)
        bad['parts'][0]['title'] = '임의로 바꾼 제목'
        cases.extend([bad, {'parts': [], 'removed_ids': []}, '{"parts":'])
        for bad in cases:
            with self.subTest(value=bad):
                self.respond(annotation=bad)
                self.assertEqual(self.aggregate(subject_id).status_code, 502)
                after = self.store.get_subject(subject_id)
                for key in ('summary', 'summary_document', 'summary_version', 'summary_updated_at'):
                    self.assertEqual(after[key], before[key])

    def test_unmatched_prose_is_preserved_and_marks_missing_direct_evidence(self):
        note = self.note(final='속도는 위치의 변화율이다.')
        body = '과목 개요\n\n이제 다음 주제를 살펴보겠습니다.\n\n속도는 위치의 변화율이다.'
        self.respond(body=body)
        response = self.aggregate(note['subject_id'])
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result['summary'], body)
        self.assertEqual(result['summary_document']['items'][0]['sources'], [])
        self.assertEqual(result['summary_document']['items'][1]['sources'][0]['note_id'], note['id'])

    def test_long_paragraph_and_unicode_offsets_survive_annotation(self):
        body = '𝑥 😀 ' + 'alpha ' * 2200 + '\n\n다음 문단이다.'
        note = self.note(final=body)
        self.respond(body=body)
        response = self.aggregate(note['subject_id'])
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result['summary'], body.strip())
        for item in result['summary_document']['items']:
            self.assertEqual(result['summary'][item['start']:item['end']], item['text'])
        self.assertGreater(len(result['summary_document']['items'][0]['text']), 12000)

    def test_move_out_and_back_preserves_old_trace_and_restores_id(self):
        first = self.note(final='첫 강의의 내용이다.')
        second = self.note(final='둘째 강의의 내용이다.', day=2)
        subject_id = first['subject_id']
        original = self.aggregate(subject_id).json()['summary_document']['items'][0]
        elsewhere = self.store.create_subject('이동 대상')
        self.store.move_note(first['id'], elsewhere)
        snapshot = self.store.get_subject(subject_id)
        self.assertEqual(next(note for note in snapshot['source_notes'] if note['id'] == first['id'])['subject_id'], elsewhere)
        removed = self.aggregate(subject_id).json()['summary_document']
        item = next(item for item in removed['items'] if item['id'] == original['id'])
        self.assertFalse(item['active'])
        self.assertEqual(item['history'][-1]['kind'], 'removed')
        self.store.move_note(first['id'], subject_id)
        restored = self.aggregate(subject_id).json()['summary_document']
        item = next(item for item in restored['items'] if item['id'] == original['id'])
        self.assertEqual(item['created_at'], original['created_at'])
        self.assertEqual([event['kind'] for event in item['history']], ['added', 'removed', 'restored'])
        self.store.delete_note(first['id'])
        result = self.aggregate(subject_id).json()
        self.assertNotIn(first['id'], [note['id'] for note in result['source_notes']])
        self.assertIn(first['id'], json.dumps(result['summary_document']))
        self.assertIn(second['final'], result['summary'])

    def test_concurrent_update_and_clear_cannot_overwrite_trace(self):
        note = self.note()
        subject_id = note['subject_id']
        initial = self.aggregate(subject_id).json()
        newer = self.aggregate(subject_id).json()
        with self.assertRaises(LookupError):
            self.store.save_subject_summary(subject_id, 'outdated', 'gpt-5.6-sol', initial['revision'], initial['summary_document'], initial['summary_version'])
        self.assertEqual(self.store.get_subject(subject_id)['summary'], newer['summary'])
        self.store.clear_subject_summary(subject_id)
        self.assertEqual(self.store.get_subject(subject_id)['summary_document'], {})
        self.assertEqual(self.store.get(note['id']), note)
        with self.assertRaises(LookupError):
            self.store.save_subject_summary(subject_id, 'outdated', 'gpt-5.6-sol', newer['revision'], newer['summary_document'], newer['summary_version'])

    def test_legacy_schema_and_existing_summary_are_not_relabelled(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'old.sqlite3'
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("""CREATE TABLE subjects (id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL,
                    created_at TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '', summary_model TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL DEFAULT 0, summary_revision INTEGER NOT NULL DEFAULT -1, summary_updated_at TEXT)""")
                connection.execute('INSERT INTO subjects (id,name,created_at,summary,summary_updated_at) VALUES (?,?,?,?,?)', ('old', '물리', '2026-01-01', '기존 본문', '2026-01-02'))
            migrated = NoteStore(path).get_subject('old')
            self.assertEqual(migrated['summary'], '기존 본문')
            self.assertEqual(migrated['summary_document'], {})
        note = self.note(final='현재 강의에서 확인되는 내용이다.')
        with self.store.connection() as connection:
            connection.execute('UPDATE subjects SET summary=?, summary_updated_at=? WHERE id=?', ('기존 본문', '2026-01-02', note['subject_id']))
        result = self.aggregate(note['subject_id']).json()['summary_document']
        self.assertEqual(result['legacy']['text'], '기존 본문')
        self.assertEqual(result['legacy']['generated_at'], '2026-01-02')
        self.assertNotEqual(result['items'][0]['created_at'], '2026-01-02')

    def test_multistage_prose_gets_sources_from_every_annotation_batch(self):
        first = self.note(final='첫 근거' + 'A' * 80000)
        second = self.note(final='두 번째 근거' + 'B' * 80000, day=2)
        response = self.aggregate(first['subject_id'])
        self.assertEqual(response.status_code, 200, response.text)
        calls = [(json.loads(call['input']), call) for call in self.model.calls]
        composition = [(payload, call) for payload, call in calls if 'annotation_layout' not in payload]
        annotations = [(payload, call) for payload, call in calls if 'annotation_layout' in payload]
        self.assertEqual(len(composition), 3)
        self.assertEqual(len(annotations), 2)
        self.assertFalse(annotations[0][0]['grounding_only'])
        self.assertTrue(annotations[1][0]['grounding_only'])
        self.assertNotIn('previous_note', annotations[1][0])
        result = response.json()
        self.assertEqual(result['summary'], subject_text(composition[-1][0]))
        sources = [source for item in result['summary_document']['items'] if item['active'] for source in item['sources']]
        self.assertEqual({source['note_id'] for source in sources}, {first['id'], second['id']})
        for source in sources:
            self.assertIn(source['quote'], self.store.get(source['note_id'])['final'])


if __name__ == '__main__':
    unittest.main()
