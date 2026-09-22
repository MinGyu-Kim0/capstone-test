"""Offline integration checks: no microphone, network, or API charges."""

import asyncio
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import main


class Browser:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()

    async def receive(self):
        return await self.incoming.get()

    async def send_json(self, event):
        await self.outgoing.put(event)

    async def until(self, event_type, kind=None):
        while True:
            event = await asyncio.wait_for(self.outgoing.get(), 3)
            if event["type"] == event_type and (kind is None or event.get("kind") == kind):
                return event


class Speech:
    def __init__(self, finish=True, speech=True):
        self.events = asyncio.Queue()
        self.finish = finish
        self.speech = speech
        self.frames = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def send(self, frame):
        self.frames.append(frame)
        if isinstance(frame, str) and frame:  # Nonempty text is the initial configuration.
            return
        if frame == b"":  # Observed Soniox behavior: empty binary audio is not EOF.
            return
        if frame:
            for tokens in [
                [
                    {"text": "월요일", "is_final": False, "translation_status": "original"},
                ],
                [
                    {"text": "회의는 ", "is_final": True, "translation_status": "original"},
                    {"text": "화요일", "is_final": False, "translation_status": "original"},
                ],
                [
                    {"text": "화요일입니다.", "is_final": True, "translation_status": "original"},
                    {"text": "<end>", "is_final": True},
                ],
            ]:
                await self.events.put({"tokens": tokens})
        elif self.finish:
            if self.speech:
                await self.events.put({"tokens": [
                    {"text": "담당자는 민수입니다.", "is_final": True},
                    {"text": "<fin>", "is_final": True},
                ]})
            await self.events.put({"tokens": [], "finished": True})
        else:
            await self.events.put(None)  # Provider disconnects without finished=true.

    def __aiter__(self):
        return self

    async def __anext__(self):
        event = await self.events.get()
        if event is None:
            raise StopAsyncIteration
        return json.dumps(event)


class Stream:
    def __init__(self, events, block=None):
        self.events = events
        self.block = block
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True

    async def __aiter__(self):
        for event in self.events:
            await asyncio.sleep(0)
            yield SimpleNamespace(**event)
        if self.block:
            self.block.set()
            await asyncio.Event().wait()


class Summarizer:
    def __init__(self, fail_live_once=False, fail_final=False, block_final=False):
        self.responses = self
        self.calls = []
        self.streams = []
        self.fail_live_once = fail_live_once
        self.fail_final = fail_final
        self.block_final = asyncio.Event() if block_final else None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        payload = json.loads(kwargs["input"])
        final = "transcript" in payload or "lecture_notes" in payload
        fail = (not final and self.fail_live_once) or (final and self.fail_final)
        if not final:
            self.fail_live_once = False
        text = "- 화요일 회의"
        if "lecture_notes" in payload:
            from trace_fakes import subject_response
            text = subject_response(payload)
        events = [{"type": "response.output_text.delta", "delta": text}]
        if not (final and self.block_final is not None):
            events.append({"type": "response.incomplete" if fail else "response.completed"})
        stream = Stream(events, self.block_final if final else None)
        self.streams.append(stream)
        return stream


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, model=None, speech=None):
        browser = Browser()
        model = model or Summarizer()
        speech = speech or Speech()
        session = main.Session(browser, speech, model, 16000)
        task = asyncio.create_task(session.run())
        self.addAsyncCleanup(self.cancel, task)
        return browser, speech, model, session, task

    async def cancel(self, task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_live_then_final_includes_last_tokens_and_requested_models(self):
        with patch.object(main, "LIVE_RETRY_DELAY", .02):
            browser, speech, model, session, task = await self.exercise()
            await browser.incoming.put({"type": "websocket.receive", "bytes": b"\x00\x00"})
            first = await browser.until("transcript")
            self.assertEqual(first["partial"], "월요일")
            second = await browser.until("transcript")
            self.assertEqual(second["partial"], "화요일")
            await browser.until("summary_done", "live")
            await browser.incoming.put({"type": "websocket.receive", "text": '{"type":"stop"}'})
            done = await browser.until("complete")
            await asyncio.wait_for(task, 3)
        self.assertTrue(done["final_ok"])
        self.assertEqual(session.transcript, "회의는 화요일입니다.\n담당자는 민수입니다.")
        self.assertEqual(speech.frames[-1], "")
        self.assertEqual([c["model"] for c in model.calls], ["gpt-6-luna", "gpt-6-sol"])
        self.assertEqual(json.loads(model.calls[-1]["input"])["transcript"], session.transcript)
        self.assertTrue(all(c["store"] is False for c in model.calls))
        self.assertTrue(all(s.closed for s in model.streams))

    async def test_translation_only_does_not_trigger_summary(self):
        with patch.object(main, "LIVE_RETRY_DELAY", .01):
            browser, speech, model, session, task = await self.exercise(speech=Speech(speech=False))
            await speech.events.put({"tokens": [
                {"text": "unexpected translated text", "is_final": True, "translation_status": "translation"},
            ]})
            event = await browser.until("transcript")
            self.assertEqual(event["delta"], "")
            self.assertNotIn("translation_delta", event)
            await asyncio.sleep(.04)
            self.assertEqual(model.calls, [])
            await browser.incoming.put({"type": "websocket.receive", "text": '{"type":"stop"}'})
            self.assertFalse((await browser.until("complete"))["final_ok"])
            await asyncio.wait_for(task, 3)
            self.assertEqual(session.transcript, "")

    async def test_source_length_limit(self):
        speech = Speech()
        session = main.Session(Browser(), speech, Summarizer(), 16000)
        await speech.events.put({"tokens": [
            {"text": "원문이 너무 깁니다", "is_final": True},
        ]})
        with patch.object(main, "MAX_TRANSCRIPT_CHARS", 5):
            with self.assertRaises(main.SessionError):
                await session.receive_tokens()
        self.assertEqual(session.transcript, "")

    async def test_soniox_timeout_cancels_session_without_summary(self):
        _, speech, model, _, task = await self.exercise()
        await speech.events.put({"error_code": 408, "error_type": "request_timeout"})
        with self.assertRaisesRegex(main.SessionError, "시간이 초과"):
            await asyncio.wait_for(task, 3)
        self.assertEqual(model.calls, [])

    async def test_failed_live_summary_retries_without_losing_new_text(self):
        with patch.object(main, "LIVE_RETRY_DELAY", .02):
            browser, _, model, session, task = await self.exercise(Summarizer(fail_live_once=True))
            await browser.incoming.put({"type": "websocket.receive", "bytes": b"\0\0"})
            error = await browser.until("error", "live")
            self.assertFalse(error["fatal"])
            self.assertEqual(session.completed_chunks, 0)
            await browser.until("summary_done", "live")
            await browser.incoming.put({"type": "websocket.receive", "text": '{"type":"stop"}'})
            await browser.until("complete")
            await asyncio.wait_for(task, 3)
        self.assertEqual(model.calls[0]["input"], model.calls[1]["input"])

    async def test_stop_cancels_pending_live_summary_and_still_finishes(self):
        model = Summarizer()
        blocked = asyncio.Event()
        real_create = model.create

        async def create(**kwargs):
            stream = await real_create(**kwargs)
            if kwargs["model"] == main.LIVE_MODEL:
                stream.events = stream.events[:1]
                stream.block = blocked
            return stream

        model.create = create
        with patch.object(main, "LIVE_RETRY_DELAY", .02):
            browser, _, _, _, task = await self.exercise(model)
            await browser.incoming.put({"type": "websocket.receive", "bytes": b"\0\0"})
            await asyncio.wait_for(blocked.wait(), 3)
            await browser.incoming.put({"type": "websocket.receive", "text": '{"type":"stop"}'})
            self.assertTrue((await browser.until("complete"))["final_ok"])
            await asyncio.wait_for(task, 3)
        self.assertTrue(all(s.closed for s in model.streams))

    async def test_disconnect_cancels_inflight_final_summary(self):
        model = Summarizer(block_final=True)
        browser, _, _, _, task = await self.exercise(model)
        await browser.incoming.put({"type": "websocket.receive", "text": '{"type":"stop"}'})
        await asyncio.wait_for(model.block_final.wait(), 3)
        await browser.incoming.put({"type": "websocket.disconnect", "code": 1000})
        with self.assertRaises(WebSocketDisconnect):
            await asyncio.wait_for(task, 3)
        self.assertTrue(model.streams[-1].closed)

    async def test_no_speech_skips_summary(self):
        browser, _, model, _, task = await self.exercise(speech=Speech(speech=False))
        await browser.incoming.put({"type": "websocket.receive", "text": '{"type":"stop"}'})
        self.assertFalse((await browser.until("complete"))["final_ok"])
        await asyncio.wait_for(task, 3)
        self.assertEqual(model.calls, [])

    async def test_stop_uses_text_eof_and_automatically_completes_final_note(self):
        browser, speech, model, session, task = await self.exercise()
        # No endpoint was emitted: the last confirmed speech arrives only on EOF.
        await browser.incoming.put({"type": "websocket.receive", "text": '{"type":"stop"}'})
        final = await browser.until("summary_done", "final")
        complete = await browser.until("complete")
        await asyncio.wait_for(task, 3)
        self.assertEqual(speech.frames, [""])
        self.assertTrue(session.transcription_done)
        self.assertTrue(final["text"])
        self.assertTrue(complete["final_ok"])
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(json.loads(model.calls[0]["input"])["transcript"], "담당자는 민수입니다.")

    async def test_binary_audio_eof_does_not_finalize_provider_stream(self):
        speech = Speech()
        await speech.send(b"")
        self.assertTrue(speech.events.empty())
        await speech.send("")
        events = []
        while not speech.events.empty():
            events.append(speech.events.get_nowait())
        self.assertTrue(events[-1]["finished"])

    async def test_unfinished_transcription_never_produces_final_summary(self):
        browser, _, model, _, task = await self.exercise(speech=Speech(finish=False))
        await browser.incoming.put({"type": "websocket.receive", "text": '{"type":"stop"}'})
        with self.assertRaises(main.SessionError):
            await asyncio.wait_for(task, 3)
        self.assertEqual(model.calls, [])

    async def test_incomplete_final_summary_is_reported_as_failure(self):
        browser, _, _, session, task = await self.exercise(Summarizer(fail_final=True))
        await browser.incoming.put({"type": "websocket.receive", "text": '{"type":"stop"}'})
        await browser.until("error", "final")
        self.assertFalse((await browser.until("complete"))["final_ok"])
        await asyncio.wait_for(task, 3)
        self.assertIn("민수", session.transcript)

    async def test_only_final_source_endpoint_completes_chunk(self):
        browser, speech, model, _, task = await self.exercise(speech=Speech(speech=False))
        await speech.events.put({"tokens": [
            {"text": "Newton의 ", "is_final": True},
            {"text": "임시 내용", "is_final": False},
            {"text": "<end>", "is_final": False},
            {"text": "unexpected translation", "is_final": True, "translation_status": "translation"},
            {"text": "<end>", "is_final": True, "translation_status": "translation"},
        ]})
        await browser.until("transcript")
        await asyncio.sleep(.02)
        self.assertEqual(model.calls, [])
        await speech.events.put({"tokens": [
            {"text": "운동 법칙입니다.", "is_final": True},
            {"text": "<end>", "is_final": True},
            {"text": "<end>", "is_final": True},
        ]})
        done = await browser.until("summary_done", "live")
        self.assertEqual(done["chunk_id"], 1)
        self.assertEqual(json.loads(model.calls[0]["input"]), {
            "previous_chunks": [], "current_chunk": {"id": 1, "text": "Newton의 운동 법칙입니다."},
        })
        await browser.incoming.put({"type": "websocket.receive", "text": '{"type":"stop"}'})
        await browser.until("complete")
        await asyncio.wait_for(task, 3)
        self.assertEqual(sum(c["model"] == main.LIVE_MODEL for c in model.calls), 1)

    async def test_multiple_endpoints_use_only_previous_two_chunks_in_order(self):
        browser, speech, model, session, task = await self.exercise(speech=Speech(speech=False))
        tokens = []
        texts = ["힘은 질량 곱하기 가속도.", "즉 F = ma.", "이 식은 뉴턴의 제2법칙.", "가속도는 힘에 비례."]
        for text in texts:
            tokens.extend([{"text": text, "is_final": True}, {"text": "<end>", "is_final": True}])
        await speech.events.put({"tokens": tokens})
        for expected in range(1, 5):
            start = await browser.until("summary_start", "live")
            self.assertEqual(start["chunk_id"], expected)
            self.assertEqual(start["source_text"], texts[expected - 1])
            self.assertEqual((await browser.until("summary_done", "live"))["chunk_id"], expected)
            self.assertEqual(session.note_chunks[expected]["source_text"], texts[expected - 1])
        self.assertEqual(session.completed_chunks, 4)
        for index, call in enumerate(model.calls):
            payload = json.loads(call["input"])
            self.assertEqual(payload["current_chunk"], {"id": index + 1, "text": texts[index]})
            self.assertEqual(payload["previous_chunks"], [
                {"id": i + 1, "text": texts[i]} for i in range(max(0, index - 2), index)
            ])
            self.assertIn("반복된 내용", call["instructions"])
            self.assertIn("보완·정정", call["instructions"])
            self.assertIn("주제:", call["instructions"])
            self.assertIn("새 내용", call["instructions"])
            self.assertIn("한국어로 요약", call["instructions"])
            self.assertIn("원문 철자와 표기를 유지", call["instructions"])
        await browser.incoming.put({"type": "websocket.receive", "text": '{"type":"stop"}'})
        await browser.until("complete")
        await asyncio.wait_for(task, 3)

    async def test_failed_chunk_retries_same_payload_before_next_chunk(self):
        with patch.object(main, "LIVE_RETRY_DELAY", .01):
            browser, speech, model, _, task = await self.exercise(Summarizer(fail_live_once=True), Speech(speech=False))
            await speech.events.put({"tokens": [
                {"text": "첫 발화", "is_final": True}, {"text": "<end>", "is_final": True},
                {"text": "다음 발화", "is_final": True}, {"text": "<end>", "is_final": True},
            ]})
            self.assertTrue((await browser.until("error", "live"))["retrying"])
            self.assertEqual((await browser.until("summary_done", "live"))["chunk_id"], 1)
            self.assertEqual((await browser.until("summary_done", "live"))["chunk_id"], 2)
            self.assertEqual(model.calls[0]["input"], model.calls[1]["input"])
            self.assertEqual(json.loads(model.calls[2]["input"])["current_chunk"]["text"], "다음 발화")
            await browser.incoming.put({"type": "websocket.receive", "text": '{"type":"stop"}'})
            await browser.until("complete")
            await asyncio.wait_for(task, 3)

    async def test_disconnect_cancels_live_stream_and_does_not_process_queue(self):
        model = Summarizer()
        blocked = asyncio.Event()
        real_create = model.create

        async def create(**kwargs):
            stream = await real_create(**kwargs)
            stream.events = stream.events[:1]
            stream.block = blocked
            return stream

        model.create = create
        browser, speech, _, _, task = await self.exercise(model, Speech(speech=False))
        await speech.events.put({"tokens": [
            {"text": "첫 발화", "is_final": True}, {"text": "<end>", "is_final": True},
            {"text": "대기 발화", "is_final": True}, {"text": "<end>", "is_final": True},
        ]})
        await asyncio.wait_for(blocked.wait(), 3)
        await browser.incoming.put({"type": "websocket.disconnect", "code": 1000})
        with self.assertRaises(WebSocketDisconnect):
            await asyncio.wait_for(task, 3)
        self.assertEqual(len(model.calls), 1)
        self.assertTrue(model.streams[0].closed)


class RouteTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = patch.object(main, "NOTE_STORE", main.NoteStore(directory.name + "/notes.sqlite3"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_invalid_course_is_rejected_before_provider_connection(self):
        invalid_courses = [
            {"name": "x" * 81}, {"context": "x" * 2001},
            {"terms": ["x"] * 101}, {"terms": ["x" * 81]}, {"terms": [" "]},
            {"terms": [None]}, {"terms": "term"},
            {"context": "가" * 2000, "terms": [f"{i}영어" * 15 for i in range(20)]},
            {"language_hints": ["ja"]},
        ]
        with (
            patch.dict("os.environ", {"SONIOX_API_KEY": "fake-soniox", "OPENAI_API_KEY": "fake-openai"}),
            patch.object(main, "connect") as connect,
            TestClient(main.app, base_url="http://localhost") as client,
        ):
            for course in invalid_courses:
                with self.subTest(course=course):
                    with client.websocket_connect("ws://localhost/ws/session") as ws:
                        ws.send_json({"type": "start", "sample_rate": 16000, "course": course})
                        self.assertTrue(ws.receive_json()["fatal"])
            connect.assert_not_called()

    def test_default_course_is_optional(self):
        self.assertEqual(main.StartMessage(type="start", sample_rate=16000).course.soniox_context(), {})

    def test_websocket_route_and_final_retry(self):
        speech = Speech()
        model = Summarizer()
        with (
            patch.dict("os.environ", {"SONIOX_API_KEY": "fake-soniox", "OPENAI_API_KEY": "fake-openai"}),
            patch.object(main, "connect", return_value=speech),
            patch.object(main, "AsyncOpenAI", return_value=model),
            TestClient(main.app, base_url="http://localhost") as client,
        ):
            events = []
            with client.websocket_connect("ws://localhost/ws/session", headers={"origin": "http://localhost"}) as ws:
                ws.send_json({"type": "start", "sample_rate": 16000, "course": {
                    "name": " 일반물리학 ", "context": " 운동량과 힘을 다루는 강의 ",
                    "terms": [" momentum ", "각운동량", "momentum"],
                }})
                self.assertEqual(ws.receive_json()["type"], "ready")
                ws.send_bytes(b"\0\0")
                ws.send_json({"type": "stop"})
                while True:
                    event = ws.receive_json()
                    events.append(event)
                    if event["type"] == "complete":
                        self.assertTrue(event["final_ok"])
                        break
            provider_config = json.loads(speech.frames[0])
            self.assertEqual(provider_config["api_key"], "fake-soniox")
            self.assertEqual(provider_config["audio_format"], "pcm_s16le")
            self.assertEqual(provider_config["sample_rate"], 16000)
            self.assertEqual(provider_config["language_hints"], ["ko", "en"])
            self.assertTrue(provider_config["language_hints_strict"])
            self.assertTrue(provider_config["enable_endpoint_detection"])
            self.assertEqual(provider_config["context"], {
                "general": [{"key": "topic", "value": "일반물리학"}],
                "text": "운동량과 힘을 다루는 강의", "terms": ["momentum", "각운동량"],
            })
            self.assertNotIn("translation", provider_config)
            self.assertNotIn("fake-", json.dumps(events))
            last_source = max(i for i, e in enumerate(events) if e.get("delta") and e["type"] == "transcript")
            source_done = next(i for i, e in enumerate(events) if e["type"] == "transcript_done")
            self.assertLess(last_source, source_done)
            final_call = next(call for call in model.calls if call["model"] == main.FINAL_MODEL)
            final_input = json.loads(final_call["input"])["transcript"]
            self.assertEqual(final_input, "회의는 화요일입니다.\n담당자는 민수입니다.")
            self.assertIn("강의 개요", final_call["instructions"])
            self.assertIn("핵심 개념·정의", final_call["instructions"])
            response = client.post("/api/summary/final", json={"transcript": final_input})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["model"], "gpt-6-sol")
            self.assertEqual(json.loads(model.calls[-1]["input"])["transcript"], final_input)

    def test_static_config_and_missing_keys(self):
        with patch.dict("os.environ", {"SONIOX_API_KEY": "", "OPENAI_API_KEY": ""}):
            with TestClient(main.app, base_url="http://localhost") as client:
                self.assertEqual(client.get("/").status_code, 200)
                self.assertEqual(client.get("/static/pcm-worklet.js").status_code, 200)
                self.assertFalse(client.get("/api/config").json()["soniox_configured"])
                with client.websocket_connect("ws://localhost/ws/session") as ws:
                    error = ws.receive_json()
                    self.assertTrue(error["fatal"])
                    self.assertIn("SONIOX_API_KEY", error["message"])
                self.assertEqual(client.post("/api/summary/final", json={"transcript": "테스트"}).status_code, 503)

    def test_validation_and_cross_origin_rejection(self):
        with patch.dict("os.environ", {"SONIOX_API_KEY": "fake-soniox", "OPENAI_API_KEY": "fake-openai"}):
            with TestClient(main.app, base_url="http://localhost") as client:
                self.assertNotIn("fake-", client.get("/api/config").text)
                with client.websocket_connect("ws://localhost/ws/session") as ws:
                    ws.send_json({"type": "start", "sample_rate": 192000})
                    self.assertTrue(ws.receive_json()["fatal"])
                with self.assertRaises(WebSocketDisconnect):
                    with client.websocket_connect("ws://localhost/ws/session", headers={"origin": "https://example.com"}):
                        pass
                self.assertEqual(client.post("/api/summary/final", json={"transcript": "테스트"}, headers={"origin": "https://example.com"}).status_code, 403)
                self.assertEqual(client.post("/api/summary/final", json={"transcript": " "}).status_code, 422)
                self.assertEqual(client.post("/api/summary/final", json={"transcript": "a" * 200001}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
