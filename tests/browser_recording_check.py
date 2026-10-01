"""Optional browser regression: pip install playwright; python -m playwright install chromium.

Run from the project root: python tests/browser_recording_check.py
Uses a fake microphone and provider responses; no external API requests.
"""
import asyncio
import json
import os
import socket
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import main
import uvicorn
from playwright.async_api import async_playwright


class Speech:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.sent = False
    async def __aenter__(self): return self
    async def __aexit__(self, *_): pass
    async def send(self, data):
        if isinstance(data, str) and data:
            config = json.loads(data)
            self.topic = config.get('context', {}).get('general', [{'value': ''}])[0]['value']
            return
        if data == b'': return
        if data and not self.sent:
            self.sent = True
            await self.queue.put({'tokens': [{'text': self.topic + ': acceleration은 속도의 변화율입니다.', 'is_final': True}, {'text': '<end>', 'is_final': True}]})
        elif not data:
            await asyncio.sleep(.05)
            await self.queue.put({'tokens': [{'text': '단위는 m/s²입니다.', 'is_final': True}, {'text': '<fin>', 'is_final': True}]})
            await self.queue.put({'tokens': [], 'finished': True})
    def __aiter__(self): return self
    async def __anext__(self): return json.dumps(await self.queue.get())


class Stream:
    def __init__(self, final, model, payload): self.final, self.model, self.payload, self.closed = final, model, payload, False
    async def __aenter__(self): return self
    async def __aexit__(self, *_): self.closed = True
    async def __aiter__(self):
        if not self.final and self.model.block_live:
            yield SimpleNamespace(type='response.output_text.delta', delta='작성 중')
            await asyncio.Event().wait()
        parts = ['강의 개요\n', '- acceleration은 속도의 변화율입니다.\n', '- 단위는 m/s²입니다.']
        if not self.final:
            parts = ['주제: acceleration의 정의\n', '새 내용\n- acceleration은 속도의 변화율입니다.\n', '반복된 내용\n- 없음\n보완·정정\n- 없음']
        if 'lecture_notes' in self.payload:
            from trace_fakes import subject_response
            parts = [subject_response(self.payload)]
        for part in parts:
            await asyncio.sleep(self.model.delay)
            yield SimpleNamespace(type='response.output_text.delta', delta=part)
        yield SimpleNamespace(type='response.incomplete' if self.final and self.model.fail_final else 'response.completed')


class Model:
    def __init__(self): self.responses, self.calls, self.delay, self.block_live, self.fail_final = self, [], .05, False, False
    async def __aenter__(self): return self
    async def __aexit__(self, *_): pass
    async def create(self, **kwargs):
        self.calls.append(kwargs)
        payload = json.loads(kwargs['input'])
        return Stream('transcript' in payload or 'lecture_notes' in payload, self, payload)


async def run():
    model = Model()
    server = uvicorn.Server(uvicorn.Config(main.app, log_level='warning', lifespan='off'))
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    task = None
    with tempfile.TemporaryDirectory() as directory, patch.object(main, 'NOTE_STORE', main.NoteStore(Path(directory) / 'notes.sqlite3')), patch.dict(os.environ, {'SONIOX_API_KEY': 'test', 'OPENAI_API_KEY': 'test'}), patch.object(main, 'connect', side_effect=lambda *_a, **_k: Speech()), patch.object(main, 'AsyncOpenAI', side_effect=lambda **_k: model):
        try:
            task = asyncio.create_task(server.serve(sockets=[sock]))
            while not server.started: await asyncio.sleep(.01)
            async with async_playwright() as p:
                browser = await p.chromium.launch(args=['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream', '--no-sandbox'])
                context = await browser.new_context(permissions=['microphone'])
                page = await context.new_page()
                errors = []
                snapshots = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                for index, (block_live, delay) in enumerate([(False, .03), (True, .1), (True, 1), (False, .03)]):
                    live_model, final_model = [
                        ('gpt-6.1-sol', 'gpt-6.1-sol'), ('gpt-6-luna', 'gpt-6-luna'),
                        ('gpt-6-luna', 'gpt-6.1-sol'), ('gpt-6.1-sol', 'gpt-6-luna'),
                    ][index]
                    model.block_live, model.delay = block_live, delay
                    model.fail_final = index == 3
                    model.calls.clear()
                    await page.goto(f'http://127.0.0.1:{port}')
                    await page.locator('#new-note:enabled').click()
                    for selector in ('#live-model', '#final-model', '#subject-model'):
                        assert await page.locator(selector + ' option').evaluate_all('(options) => options.map(option => option.value)') == ['gpt-6.1-sol', 'gpt-6-luna']
                    await page.locator('#course-name').fill(f'테스트 강의 {index + 1}')
                    await page.locator('#live-model').select_option(live_model)
                    await page.locator('#final-model').select_option(final_model)
                    await page.locator('#start:enabled').click()
                    await page.wait_for_function("document.body.dataset.state === 'recording' && document.querySelector('#confirmed').textContent.length > 0")
                    assert await page.locator('#new-note').is_disabled()
                    assert await page.locator('#live-model').is_disabled()
                    assert await page.locator('#final-model').is_disabled()
                    assert await page.locator('.saved-note:enabled').count() == 0
                    if not block_live:
                        await page.wait_for_selector('.live-chunk[data-status="done"]')
                        assert await page.locator('#live-title').text_content() == '실시간 요약 노트'
                        assert await page.locator('.live-chunk h3').text_content() == 'acceleration의 정의'
                        await page.locator('.live-source summary').click()
                        assert await page.locator('.live-source p').is_visible()
                        assert await page.locator('.live-source p').text_content() == f'테스트 강의 {index + 1}: acceleration은 속도의 변화율입니다.'
                        for heading in ('새 내용', '반복된 내용', '보완·정정'):
                            assert heading in await page.locator('.live-chunk > p').text_content()
                    await page.locator('#stop').click()
                    try:
                        await page.wait_for_function("['done', 'error'].includes(document.body.dataset.state)", timeout=15000)
                    finally:
                        print(json.dumps({'block_live': block_live, 'delay': delay, 'state': await page.get_attribute('body', 'data-state'), 'notice': await page.locator('#notice').text_content(), 'final': await page.locator('#final-summary').text_content(), 'models': [call['model'] for call in model.calls], 'errors': errors}, ensure_ascii=False), flush=True)
                    assert model.calls[0]['model'] == live_model
                    assert model.calls[-1]['model'] == final_model
                    if model.fail_final:
                        assert await page.get_attribute('body', 'data-state') == 'error'
                        await page.reload()
                        await page.locator('#retry:visible').wait_for()
                        model.fail_final = False
                        await page.locator('#final-model').select_option('gpt-6.1-sol')
                        await page.locator('#retry').click()
                        await page.wait_for_function("document.body.dataset.state === 'done'")
                        assert model.calls[-1]['model'] == 'gpt-6.1-sol'
                    if index == 0:
                        await page.locator('#final-model').select_option('gpt-6-luna')
                        assert final_model in await page.locator('#final-used-model').text_content()
                        await page.locator('#retry').click()
                        await page.wait_for_function("document.body.dataset.state === 'done'")
                        assert model.calls[-1]['model'] == 'gpt-6-luna'
                    assert await page.get_attribute('body', 'data-state') == 'done'
                    assert await page.locator('#retry').is_hidden()
                    assert 'm/s²' in await page.locator('#final-summary').text_content()
                    snapshots.append({
                        'id': await page.locator('#saved-notes .saved-note[aria-current="true"]').get_attribute('data-note-id'),
                        'title': await page.locator('#note-title').text_content(),
                        'transcript': await page.locator('#confirmed').text_content(),
                        'live': await page.locator('#live-summary').text_content(),
                        'final': await page.locator('#final-summary').text_content(),
                        'live_model': await page.locator('#live-model').input_value(),
                        'final_model': await page.locator('#final-model').input_value(),
                        'live_used': await page.locator('#live-used-model').text_content(),
                        'final_used': await page.locator('#final-used-model').text_content(),
                    })
                    assert await page.locator('#saved-notes .saved-note').count() == index + 1
                # Restart the server with a new repository object and an empty browser profile.
                await context.close()
                server.should_exit = True
                await task
                sock.close()
                main.NOTE_STORE = main.NoteStore(Path(directory) / 'notes.sqlite3')
                sock = socket.socket()
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
                server = uvicorn.Server(uvicorn.Config(main.app, log_level='warning', lifespan='off'))
                task = asyncio.create_task(server.serve(sockets=[sock]))
                while not server.started: await asyncio.sleep(.01)
                context = await browser.new_context(viewport={'width': 1440, 'height': 1080})
                page = await context.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))
                await page.goto(f'http://127.0.0.1:{port}')
                await page.locator('#new-note:enabled').wait_for()
                assert await page.locator('#saved-notes .saved-note').count() == len(snapshots)
                for snapshot in snapshots:
                    await page.locator(f'#saved-notes .saved-note[data-note-id="{snapshot["id"]}"]').click()
                    await page.wait_for_function("document.body.dataset.state === 'done'")
                    for field, selector in [('title', '#note-title'), ('transcript', '#confirmed'), ('live', '#live-summary'), ('final', '#final-summary')]:
                        assert await page.locator(selector).text_content() == snapshot[field], field
                    for kind in ('live', 'final'):
                        assert await page.locator(f'#{kind}-model').input_value() == snapshot[kind + '_model']
                        assert await page.locator(f'#{kind}-used-model').text_content() == snapshot[kind + '_used']
                # Move a lecture into another course without changing its original three results.
                first_id, moved_id = snapshots[0]['id'], snapshots[1]['id']
                subject_id = main.NOTE_STORE.get(first_id)['subject_id']
                await page.locator(f'#saved-notes .saved-note[data-note-id="{moved_id}"]').click()
                await page.wait_for_function("document.body.dataset.state === 'done'")
                await page.locator('#note-folder').select_option(subject_id)
                await page.wait_for_function("document.body.dataset.state === 'done'")
                assert await page.locator('#confirmed').text_content() == snapshots[1]['transcript']
                assert main.NOTE_STORE.get(moved_id)['subject_id'] == subject_id
                folder = page.locator(f'#saved-notes .subject-note[data-subject-id="{subject_id}"]')
                await folder.click()
                await page.wait_for_function("document.body.dataset.state === 'subject'")
                assert await page.locator('#subject-lectures .saved-note:enabled').count() == 2
                await page.locator('#subject-lectures .saved-note').first.click()
                await page.wait_for_function("document.body.dataset.state === 'done'")
                await folder.click()
                await page.wait_for_function("document.body.dataset.state === 'subject'")
                await page.locator('#subject-model').select_option('gpt-6.1-sol')
                await page.locator('#subject-generate').click()
                await page.wait_for_function("document.body.dataset.state === 'subject' && !document.querySelector('#subject-summary').classList.contains('placeholder')")
                aggregate = await page.locator('#subject-summary').text_content()
                payload = json.loads(model.calls[-1]['input'])
                assert set(note['id'] for note in payload['lecture_notes']) == {first_id, moved_id}
                assert 'transcript' not in payload
                assert model.calls[-1]['model'] == 'gpt-6.1-sol'
                model.fail_final = True
                await page.locator('#subject-generate').click()
                await page.wait_for_function("document.body.dataset.state === 'subject' && !document.querySelector('#notice').hidden")
                assert await page.locator('#subject-summary').text_content() == aggregate
                model.fail_final = False
                await page.reload()
                await page.wait_for_function("document.body.dataset.state === 'subject'")
                assert await page.locator('#subject-summary').text_content() == aggregate
                original_doc = main.NOTE_STORE.get_subject(subject_id)['summary_document']
                original_id = original_doc['items'][0]['id']
                await page.evaluate('(id) => savedNotes.delete(id)', moved_id)
                await folder.click()
                await page.wait_for_function("document.body.dataset.state === 'subject'")
                assert await page.locator(f'.trace-item[data-item-id="{original_id}"]').count() == 1
                await page.locator('.trace-item .trace-details > summary').first.click()
                await page.locator(f'.trace-source[data-note-id="{moved_id}"]').first.click()
                await page.wait_for_function("document.body.dataset.state === 'done'")
                await folder.click()
                await page.wait_for_function("document.body.dataset.state === 'subject'")
                moved = main.NOTE_STORE.get(moved_id)
                main.NOTE_STORE.save_final(moved_id, '보충: force는 질량과 acceleration의 곱입니다.', moved['transcript'])
                await page.locator('#subject-generate').click()
                await page.wait_for_function("document.body.dataset.state === 'subject' && document.querySelectorAll('.trace-part .trace-item').length === 2")
                changed_doc = main.NOTE_STORE.get_subject(subject_id)['summary_document']
                assert changed_doc['items'][0]['id'] == original_id
                assert changed_doc['items'][0]['created_at'] == original_doc['items'][0]['created_at']
                assert changed_doc['items'][1]['id'] != original_id
                assert changed_doc['items'][1]['part_id'] == changed_doc['items'][0]['part_id']
                assert changed_doc['items'][1]['sources'][0]['note_id'] == moved_id
                await page.locator('#subject-generate').click()
                await page.wait_for_function("document.body.dataset.state === 'subject'")
                assert main.NOTE_STORE.get_subject(subject_id)['summary_document'] == changed_doc
                assert not await page.locator('.trace-part > .trace-item > .trace-details[open]').count()
                assert await page.locator('.trace-part > .trace-item > .trace-text').first.is_visible()
                await page.locator(f'.trace-item[data-item-id="{original_id}"] > .trace-details > summary').click()
                await page.locator(f'.trace-item[data-item-id="{original_id}"] .trace-history > summary').click()
                await page.wait_for_function("document.querySelectorAll('.trace-event').length === 2")
                assert 'U0001' in await page.locator('.trace-history').first.text_content()
                async with page.expect_download() as download_info:
                    await page.locator('#download').click()
                download = await download_info.value
                exported = Path(await download.path()).read_text(encoding='utf-8')
                assert original_id in exported and 'U0001' in exported and moved_id in exported
                await page.screenshot(path='/tmp/voice-notes-trace-desktop.png', full_page=True)
                await page.set_viewport_size({'width': 390, 'height': 844})
                assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                await page.screenshot(path='/tmp/voice-notes-trace-mobile.png', full_page=True)
                await page.set_viewport_size({'width': 1440, 'height': 1100})
                async def accept_dialog(dialog):
                    await dialog.accept()
                page.on('dialog', accept_dialog)
                await page.locator(f'#subject-lectures .saved-note[data-note-id="{first_id}"]').click()
                await page.wait_for_function("document.body.dataset.state === 'done'")
                await page.locator('#note-delete').click()
                await page.wait_for_function("document.body.dataset.state === 'subject'")
                assert main.NOTE_STORE.get(first_id) is None
                assert await page.locator('#subject-status').get_attribute('data-stale') == 'true'
                assert await page.locator('#subject-lectures .saved-note').count() == 1
                await page.locator('#subject-generate').click()
                await page.wait_for_function("document.body.dataset.state === 'subject' && document.querySelector('#subject-status').dataset.stale === 'false'")
                assert len(json.loads(model.calls[-1]['input'])['lecture_notes']) == 1
                await page.locator('.trace-archive > summary').click()
                excluded = page.locator(f'.trace-archive .trace-item[data-item-id="{original_id}"]')
                assert await excluded.count() == 1
                await excluded.locator('.trace-details > summary').first.click()
                assert await excluded.locator('.trace-source').first.is_disabled()
                assert '삭제된 강의' in await excluded.text_content()
                await page.locator('#subject-summary-delete').click()
                await page.wait_for_function("document.body.dataset.state === 'subject' && document.querySelector('#subject-summary').classList.contains('placeholder')")
                assert main.NOTE_STORE.get(moved_id) is not None
                assert main.NOTE_STORE.get_subject(subject_id)['summary_document'] == {}
                await page.locator('#subject-name').fill('빈 과목 확인')
                await page.locator('#subject-create').click()
                await page.wait_for_function("document.body.dataset.state === 'subject' && document.querySelector('#note-title').textContent === '빈 과목 확인'")
                await page.locator('#subject-delete').click()
                await page.wait_for_function("document.body.dataset.state === 'idle'")
                assert all(subject['name'] != '빈 과목 확인' for subject in main.NOTE_STORE.list_subjects())
                await folder.click()
                await page.wait_for_function("document.body.dataset.state === 'subject'")
                await page.locator('#subject-generate').click()
                await page.wait_for_function("document.body.dataset.state === 'subject' && !document.querySelector('#subject-summary').classList.contains('placeholder')")
                await page.screenshot(path='/tmp/voice-notes-subjects-desktop.png', full_page=True)
                await page.set_viewport_size({'width': 390, 'height': 844})
                assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                await page.screenshot(path='/tmp/voice-notes-subjects-mobile.png', full_page=True)
                assert not errors, errors
                print('PASS: recording/models/archive, subject folders, lecture move/delete, traced IDs/additions/history/source links/export/reload, aggregate retry/staleness, summary-only delete, desktop/mobile', flush=True)
                await browser.close()
        finally:
            server.should_exit = True
            if task: await task
            sock.close()


asyncio.run(run())
