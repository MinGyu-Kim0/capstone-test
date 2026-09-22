"""Run the local app behind an authenticated ngrok HTTPS endpoint."""

import argparse
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
import uvicorn

ROOT = Path(__file__).resolve().parent
LOCAL_HOSTS = "localhost,127.0.0.1,[::1]"


class MobileError(Exception):
    """A message safe to print without revealing agent credentials."""


def https_url(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.port not in (None, 443)
                or parsed.username or parsed.password or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment or not re.fullmatch(r"[a-zA-Z0-9.-]+", parsed.hostname)):
            raise ValueError
    except ValueError:
        raise MobileError("NGROK_URL에는 경로가 없는 HTTPS 주소를 입력해 주세요. 예: https://example.ngrok-free.app") from None
    return f"https://{parsed.netloc.lower()}"


def access_password(value):
    if not value:
        return secrets.token_urlsafe(12)
    # Basic Auth uses printable ASCII; '$' would be interpreted by ngrok's policy expressions.
    if not 8 <= len(value) <= 128 or any(ord(c) < 33 or ord(c) > 126 or c == "$" for c in value):
        raise MobileError("MOBILE_PASSWORD는 공백·$를 제외한 영문/숫자/기호 8~128자로 입력해 주세요.")
    return value


def traffic_policy(password):
    return {"on_http_request": [{"actions": [{
        "type": "basic-auth",
        "config": {"realm": "Voice Notes", "credentials": [f"voice:{password}"], "enforce": True},
    }]}]}


class Tunnel:
    def __init__(self, command):
        self.command = command
        self.process = None
        self.reader = None
        self.ready = threading.Event()
        self.url = ""
        self.error_code = ""

    def read_logs(self):
        try:
            for line in self.process.stdout:
                # Agent errors may echo a malformed authtoken. Never relay raw logs.
                code = re.search(r"ERR_NGROK_\d+", line)
                if code:
                    self.error_code = code.group()
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                url, message = event.get("url"), event.get("msg")
                if isinstance(url, str) and url.startswith("https://") and isinstance(message, str) and message.startswith("started"):
                    self.url = https_url(url)
                    self.ready.set()
                elif event.get("lvl") in ("eror", "error", "crit"):
                    self.ready.set()
        except (OSError, MobileError):
            pass
        finally:
            self.ready.set()

    def start(self, timeout=30):
        try:
            self.process = subprocess.Popen(
                self.command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
            )
            self.reader = threading.Thread(target=self.read_logs, daemon=True)
            self.reader.start()
            if not self.ready.wait(timeout) or not self.url or self.process.poll() is not None:
                code = f" ({self.error_code})" if self.error_code else ""
                raise MobileError(f"ngrok HTTPS 연결에 실패했습니다{code}. NGROK_AUTHTOKEN, ngrok 버전과 네트워크를 확인해 주세요.")
            return self.url
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            if self.reader is not None:
                self.reader.join(timeout=2)
            self.process.stdout.close()


def watch_tunnel(tunnel, server, finished):
    while not finished.wait(.5):
        if tunnel.process.poll() is not None:
            print("ngrok 연결이 종료되어 앱 서버도 종료합니다.", flush=True)
            server.should_exit = True
            return


def run(port, public_url="", executable="ngrok"):
    if not 1 <= port <= 65535:
        raise MobileError("포트는 1~65535 범위로 입력해 주세요.")
    binary = shutil.which(executable)
    if binary is None:
        raise MobileError("ngrok을 찾을 수 없습니다. https://ngrok.com/download 에서 설치하거나 NGROK_BIN에 실행 파일 경로를 지정해 주세요.")
    public_url = https_url(public_url) if public_url else ""
    password = access_password(os.getenv("MOBILE_PASSWORD", ""))
    # Reserve the port before opening the tunnel so an existing local service is never exposed.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(("127.0.0.1", port))
        except OSError:
            raise MobileError(f"127.0.0.1:{port}를 사용할 수 없습니다. 기존 서버를 종료하거나 --port로 다른 포트를 선택해 주세요.") from None
        # A project-local file also works with ngrok installations that have an isolated /tmp.
        (ROOT / "data").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="mobile-", dir=ROOT / "data") as directory:
            policy_file = Path(directory) / "policy.json"
            policy_file.write_text(json.dumps(traffic_policy(password)), encoding="utf-8")
            command = [binary, "http", f"http://127.0.0.1:{port}", "--log=stdout", "--log-format=json", "--log-level=info",
                       "--inspect=false", "--traffic-policy-file", str(policy_file)]
            if public_url:
                command.extend(["--url", public_url])
            tunnel = Tunnel(command)
            finished = threading.Event()
            watcher = None
            previous_hosts = os.environ.get("ALLOWED_HOSTS")
            try:
                print("ngrok HTTPS 주소를 준비하고 있습니다…", flush=True)
                url = tunnel.start()
                os.environ["ALLOWED_HOSTS"] = f"{LOCAL_HOSTS},{urlsplit(url).hostname}"
                # Import main only after its exact public host is known. Keep Origin checks intact.
                server = uvicorn.Server(uvicorn.Config(
                    "main:app", host="127.0.0.1", port=port, proxy_headers=True,
                    forwarded_allow_ips="127.0.0.1", timeout_graceful_shutdown=30,
                ))
                watcher = threading.Thread(target=watch_tunnel, args=(tunnel, server, finished), daemon=True)
                watcher.start()
                print(f"\n휴대폰 접속: {url}\n아이디: voice\n접속 비밀번호: {password}\n"
                      f"PC 접속: http://127.0.0.1:{port}\n종료: Ctrl+C\n", flush=True)
                server.run(sockets=[listener])
            finally:
                finished.set()
                tunnel.close()
                if watcher is not None:
                    watcher.join(timeout=2)
                if previous_hosts is None:
                    os.environ.pop("ALLOWED_HOSTS", None)
                else:
                    os.environ["ALLOWED_HOSTS"] = previous_hosts


def main():
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="앱과 ngrok을 함께 실행해 휴대폰으로 접속합니다.")
    parser.add_argument("--port", type=int, default=8000, help="로컬 포트 (기본 8000)")
    parser.add_argument("--url", default=os.getenv("NGROK_URL", ""), help="ngrok 계정의 지정 HTTPS 주소 (선택)")
    parser.add_argument("--ngrok", default=os.getenv("NGROK_BIN") or "ngrok", help="ngrok 실행 파일 경로")
    args = parser.parse_args()
    try:
        run(args.port, args.url, args.ngrok)
    except KeyboardInterrupt:
        print("\n휴대폰 접속과 앱 서버를 종료했습니다.")
    except (MobileError, OSError) as exc:
        print(f"실행 실패: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
