"""ngrok launcher lifecycle and HTTPS proxy HTTP/WebSocket checks, without public tunnels."""

import copy
import io
import json
import os
import socket
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.websockets import WebSocketDisconnect
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

import main
import run_mobile
from note_store import NoteStore
from test_app import Speech, Summarizer


class LauncherTests(unittest.TestCase):
    def test_startup_uses_authenticated_policy_exact_host_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tunnel = Mock()
            tunnel.start.return_value = "https://phone.ngrok-free.app"
            tunnel.process.poll.return_value = None
            server = Mock()
            policy_path = None

            def serve(*, sockets):
                nonlocal policy_path
                self.assertEqual(os.environ["ALLOWED_HOSTS"], "localhost,127.0.0.1,[::1],phone.ngrok-free.app")
                self.assertEqual(len(sockets), 1)
                command = tunnel_class.call_args.args[0]
                self.assertIn("--log-level=info", command)
                policy_path = Path(command[command.index("--traffic-policy-file") + 1])
                policy = json.loads(policy_path.read_text())
                self.assertEqual(policy["on_http_request"][0]["actions"][0]["config"], {
                    "realm": "Voice Notes", "credentials": ["voice:test-password"], "enforce": True,
                })
                self.assertNotIn("test-password", " ".join(command))
                self.assertNotIn("test-authtoken", " ".join(command))
                config = server_class.call_args.args[0]
                self.assertEqual(config.forwarded_allow_ips, "127.0.0.1")
                self.assertTrue(config.proxy_headers)
                raise KeyboardInterrupt

            server.run.side_effect = serve
            with patch.object(run_mobile, "ROOT", root), patch.object(run_mobile.shutil, "which", return_value="ngrok"), \
                    patch.object(run_mobile.socket, "socket"), patch.object(run_mobile, "Tunnel", return_value=tunnel) as tunnel_class, \
                    patch.object(run_mobile.uvicorn, "Server", return_value=server) as server_class, \
                    patch.dict(os.environ, {"ALLOWED_HOSTS": "localhost", "MOBILE_PASSWORD": "test-password", "NGROK_AUTHTOKEN": "test-authtoken"}), \
                    redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(KeyboardInterrupt):
                    run_mobile.run(8000)
                self.assertEqual(os.environ["ALLOWED_HOSTS"], "localhost")
                self.assertIn("https://phone.ngrok-free.app", output.getvalue())
                self.assertNotIn("test-authtoken", output.getvalue())
            tunnel.close.assert_called_once()
            self.assertFalse(policy_path.exists())

    def test_busy_port_never_opens_a_tunnel(self):
        with socket.socket() as occupied, patch.object(run_mobile.shutil, "which", return_value="ngrok"), patch.object(run_mobile, "Tunnel") as tunnel:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            with self.assertRaisesRegex(run_mobile.MobileError, "기존 서버"):
                run_mobile.run(occupied.getsockname()[1])
            tunnel.assert_not_called()

    def test_agent_logs_are_parsed_and_token_errors_are_redacted(self):
        process = Mock()
        process.poll.return_value = None
        process.stdout = io.StringIO('not json\n{"url":null}\n{"msg":"started tunnel","url":"https://phone.ngrok-free.app"}\n')
        with patch.object(run_mobile.subprocess, "Popen", return_value=process):
            tunnel = run_mobile.Tunnel(["ngrok"])
            self.assertEqual(tunnel.start(timeout=1), "https://phone.ngrok-free.app")
            tunnel.close()
            process.terminate.assert_called_once()
        process.stdout = io.StringIO('{"lvl":"eror","msg":"bad authtoken SECRET ERR_NGROK_105"}\n')
        with patch.object(run_mobile.subprocess, "Popen", return_value=process):
            with self.assertRaises(run_mobile.MobileError) as error:
                run_mobile.Tunnel(["ngrok"]).start(timeout=1)
            self.assertIn("ERR_NGROK_105", str(error.exception))
            self.assertNotIn("SECRET", str(error.exception))

    def test_startup_timeout_kills_an_unresponsive_agent(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("ngrok", 5), 0]
        with patch.object(run_mobile.subprocess, "Popen", return_value=process), patch.object(run_mobile.Tunnel, "read_logs"):
            with self.assertRaises(run_mobile.MobileError):
                run_mobile.Tunnel(["ngrok"]).start(timeout=0)
            process.terminate.assert_called_once()
            process.kill.assert_called_once()

    def test_tunnel_exit_stops_the_app(self):
        tunnel, server = Mock(), Mock()
        tunnel.process.poll.return_value = 1
        with redirect_stdout(io.StringIO()):
            run_mobile.watch_tunnel(tunnel, server, threading.Event())
        self.assertTrue(server.should_exit)

    def test_invalid_urls_and_policy_interpolation_are_rejected(self):
        for url in ("http://example.com", "https://user:secret@example.com", "https://example.com/path", "https://*.example.com", "https://example.com:8443"):
            with self.subTest(url=url), self.assertRaises(run_mobile.MobileError):
                run_mobile.https_url(url)
        for password in ("short", "has spaces", "${secrets.get('x','y')}"):
            with self.assertRaises(run_mobile.MobileError):
                run_mobile.access_password(password)
        self.assertGreaterEqual(len(run_mobile.access_password("")), 8)


class ProxyTests(unittest.TestCase):
    def test_ngrok_https_host_and_origin_allow_http_and_websocket(self):
        # Give the same app/router a separate middleware stack, as a fresh mobile process does.
        app = copy.copy(main.app)
        app.user_middleware = [Middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "phone.ngrok-free.app"])]
        app.middleware_stack = None
        proxied = ProxyHeadersMiddleware(app, trusted_hosts="127.0.0.1")
        headers = {"x-forwarded-proto": "https", "origin": "https://phone.ngrok-free.app"}
        with tempfile.TemporaryDirectory() as directory, patch.object(main, "NOTE_STORE", NoteStore(Path(directory) / "notes.sqlite3")), \
                patch.dict(os.environ, {"SONIOX_API_KEY": "fake", "OPENAI_API_KEY": "fake"}), \
                patch.object(main, "connect", return_value=Speech(speech=False)), patch.object(main, "AsyncOpenAI", return_value=Summarizer()):
            with TestClient(proxied, base_url="http://phone.ngrok-free.app", client=("127.0.0.1", 50000), headers=headers) as client:
                self.assertEqual(client.get("/").status_code, 200)
                self.assertEqual(client.get("/static/pcm-worklet.js").status_code, 200)
                self.assertEqual(client.post("/api/subjects", json={"name": "모바일 강의"}).status_code, 200)
                self.assertEqual(client.post("/api/subjects", json={"name": "거부"}, headers={"origin": "https://other.example"}).status_code, 403)
                self.assertEqual(client.get("/api/config", headers={"host": "other.example"}).status_code, 400)
                with client.websocket_connect("ws://phone.ngrok-free.app/ws/session") as ws:
                    ws.send_json({"type": "start", "sample_rate": 16000})
                    self.assertEqual(ws.receive_json()["type"], "ready")
                    ws.send_json({"type": "stop"})
                    while ws.receive_json()["type"] != "complete":
                        pass
                with self.assertRaises(WebSocketDisconnect):
                    with client.websocket_connect("ws://phone.ngrok-free.app/ws/session", headers={"origin": "https://other.example"}):
                        pass
            # An external client cannot spoof HTTPS using forwarded headers.
            with TestClient(proxied, base_url="http://phone.ngrok-free.app", client=("192.0.2.1", 50000), headers=headers) as client:
                self.assertEqual(client.post("/api/subjects", json={"name": "거부"}).status_code, 403)
