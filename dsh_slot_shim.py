"""本机 dsh 文本小壳：把 DSH 的 node 脚本包成 loopback 的 /v1/chat/completions。

只监听 127.0.0.1，只认 POST /v1/chat/completions。
用来给记忆两槽提供「本地模型」，因为那边只接受 loopback 的 OpenAI 兼容接口。

启动： py dsh_slot_shim.py   （默认 127.0.0.1:8799）
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "127.0.0.1"
PORT = int(os.environ.get("SLOT_SHIM_PORT", "8799"))
MODEL = os.environ.get("SLOT_SHIM_MODEL", "deepseek-flash")
SCRIPT = os.environ.get("SLOT_SHIM_SCRIPT", r"$MEMORY_ROOT\tools\dsh-text.mjs")


def _node() -> str:
    found = shutil.which("node")
    if found:
        return found
    fallback = Path("E:/dsh/DSH NEXT/resources/runtime/x64/primary-runtime/dependencies/node/bin/node.exe")
    return str(fallback) if fallback.exists() else ""


def _script() -> str:
    if SCRIPT and Path(SCRIPT).exists():
        return SCRIPT
    base = Path("E:/dsh/DSH NEXT/resources")
    for cand in base.rglob("dsh-text*.js"):
        return str(cand)
    return ""


def ask(system: str, prompt: str, timeout: int = 60) -> str:
    node, script = _node(), _script()
    if not node or not script:
        return ""
    env = dict(os.environ)
    env.setdefault("DSH_HOME", str(Path.home() / ".dsh"))
    try:
        proc = subprocess.run(
            [node, script, "--system", system, "--timeout", str(max(20, timeout - 10) * 1000),
             "--model", MODEL],
            input=prompt[:16000].encode("utf-8"), capture_output=True, timeout=timeout, env=env,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[shim] 调用失败：{exc}", flush=True)
        return ""
    raw = (proc.stdout or b"").decode("utf-8", "replace").strip()
    line = ""
    for cand in reversed(raw.splitlines()):
        if cand.strip().startswith("{"):
            line = cand.strip()
            break
    if not line:
        print(f"[shim] 无输出：{(proc.stderr or b'')[:160]!r}", flush=True)
        return ""
    try:
        out = json.loads(line)
    except Exception:  # noqa: BLE001
        return ""
    return str(out.get("text") or "").strip() if out.get("ok") else ""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 不记正文，只记路径
        sys.stderr.write("[shim] " + fmt % args + "\n")

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/v1/models":
            self._send(200, {"object": "list", "data": [{"id": MODEL, "object": "model"}]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path != "/v1/chat/completions":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
        except Exception:  # noqa: BLE001
            self._send(400, {"error": "bad json"})
            return
        msgs = data.get("messages") or []
        system = "".join(str(m.get("content") or "") for m in msgs if m.get("role") == "system")
        user = "".join(str(m.get("content") or "") for m in msgs if m.get("role") == "user")
        text = ask(system, user)
        if not text:
            self._send(502, {"error": "dsh text failed"})
            return
        self._send(200, {
            "id": f"shim-{int(time.time()*1000)}",
            "object": "chat.completion",
            "model": str(data.get("model") or MODEL),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
        })


if __name__ == "__main__":
    print(f"[shim] http://{HOST}:{PORT}/v1/chat/completions model={MODEL}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
