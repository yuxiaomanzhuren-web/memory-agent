"""鱼小满控制台 v2 —— 一页看现状，不碰主服务。

为什么不改主服务里的 console：那个是 8780 那套旧的，改它要先重启，重启还得杀人家的进程；
这里单独起一个只读+少量动作的小服务，主服务挂了它也能告诉你「挂了」。
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB = os.environ.get("MEMORY_DB", r"$MEMORY_ROOT\data\brain.db")
PORT = int(os.environ.get("CONSOLE_PORT", "8782"))
SYN = r"$MEMORY_ROOT\memory-core\memory_core\synonyms.py"

MEMBAT = r"$MEMORY_ROOT\start-memory-core.bat"
PANEL = r"E:\阿鲤的工作台\panel_xmy.py"
SWITCH = r"E:\阿鲤的工作台\switch_panel.py"
PYEXE = sys.executable

# 名字、端口、说明、能不能开关、怎么起
SERVICES = [
    ("记忆服务", 8781, "记忆 · 召回 · 本页数据源", True, {"bat": MEMBAT}),
    ("记忆面板", 8791, "鱼小满的库，只读", True, {"script": PANEL}),
    ("换号面板", 8792, "当前号 · 重启雪光与桥接", True, {"script": SWITCH}),
    ("桥接", 3100, "QQ ↔ DSH，人格卡这里切", False, None),
    ("雪光", 3000, "QQ 账号的推送通道", False, None),
]


def probe(port: int) -> bool:
    s = socket.socket()
    s.settimeout(0.35)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def lines() -> list[dict]:
    """每条归属线各多少条 —— 这就是「两个她」的账。"""
    con = sqlite3.connect(DB)
    try:
        rows = con.execute(
            "select source, count(*) from memories where status='active' group by source order by 2 desc"
        ).fetchall()
    finally:
        con.close()
    out = []
    for src, n in rows:
        s = str(src or "")
        if s.startswith("qq:"):
            s = s[3:]
        bot, _, conv = s.partition("|")
        out.append({"bot": bot or "(未标)", "conv": conv or "-", "n": n})
    return out


def synonyms() -> list[dict]:
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(SYN)))
        from memory_core.synonyms import TABLE  # type: ignore

        return [{"k": k, "v": " / ".join(v)} for k, v in TABLE.items()]
    except Exception as exc:
        return [{"k": "读不到词表", "v": str(exc)[:80]}]


def stats() -> dict:
    con = sqlite3.connect(DB)
    try:
        active = con.execute("select count(*) from memories where status='active'").fetchone()[0]
        arch = con.execute("select count(*) from memories where status<>'active'").fetchone()[0]
        ents = con.execute("select count(*) from entities").fetchone()[0]
    except Exception:
        active = arch = ents = -1
    finally:
        con.close()
    return {"active": active, "archived": arch, "entities": ents}


def page() -> str:
    svc = [{"name": n, "port": p, "role": r, "up": probe(p), "ctl": bool(c)} for n, p, r, c, _ in SERVICES]
    data = {"services": svc, "lines": lines(), "syn": synonyms(), "stats": stats(),
            "now": time.strftime("%H:%M:%S")}
    blob = json.dumps(data, ensure_ascii=False)
    return TEMPLATE.replace("__DATA__", blob)


TEMPLATE = r"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>小满控制台</title><style>
:root{--bg:#0f1216;--card:#171c23;--line:#232b35;--txt:#e6edf3;--dim:#8b98a5;--ok:#3ecf8e;--bad:#e5534b;--acc:#4a9eff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:960px;margin:0 auto;padding:24px 18px 60px}
h1{font-size:19px;margin:0 0 4px;font-weight:600}
.sub{color:var(--dim);font-size:12px;margin-bottom:20px}
h2{font-size:13px;color:var(--dim);font-weight:600;letter-spacing:.08em;margin:26px 0 10px;text-transform:uppercase}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.row{display:flex;justify-content:space-between;align-items:center;gap:10px}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:7px}
.up{background:var(--ok);box-shadow:0 0 8px #3ecf8e88}.down{background:var(--bad)}
.name{font-weight:600}.role{color:var(--dim);font-size:12px;margin-top:2px}
.port{color:var(--dim);font-size:12px;font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line)}
th{color:var(--dim);font-weight:500;font-size:12px}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
.kpi{display:flex;gap:22px;flex-wrap:wrap}
.kpi div{font-size:12px;color:var(--dim)}.kpi b{display:block;font-size:22px;color:var(--txt);font-weight:600}
button{background:#1e2733;color:var(--txt);border:1px solid var(--line);border-radius:7px;padding:6px 12px;font-size:12px;cursor:pointer}
button:hover{border-color:var(--acc)}
input{background:#0d1117;border:1px solid var(--line);color:var(--txt);border-radius:7px;padding:6px 9px;font-size:13px;width:180px}
.foot{color:var(--dim);font-size:11px;margin-top:28px}
</style></head><body><div class="wrap">
<h1>小满控制台</h1>
<div class="sub">一页看现状 · 数据源是本机记忆库与端口探活 · <span id="ts"></span></div>

<h2>服务</h2><div class="grid" id="svc"></div>

<h2>记忆两条线</h2>
<div class="grid"><div class="card">
<div class="kpi" id="kpi"></div></div></div>
<div class="card" style="margin-top:10px"><table><thead><tr><th>机器人号</th><th>会话</th><th>条数</th></tr></thead><tbody id="lines"></tbody></table></div>

<h2>小词表</h2>
<div class="card"><table><thead><tr><th>原词</th><th>替身</th></tr></thead><tbody id="syn"></tbody></table>
<div style="margin-top:10px"><input id="nk" placeholder="原词"><input id="nv" placeholder="替身" style="margin-left:6px">
<button onclick="addSyn()" style="margin-left:6px">加一条</button></div>
<div class="role" style="margin-top:8px">只收「亲自遇到的漏检词」，一条最多两个替身。</div></div>

<h2>面板</h2>
<div class="card">
<div><a href="http://127.0.0.1:8791/" target="_blank">记忆面板 8791</a> · 只看她的库</div>
<div><a href="http://127.0.0.1:8792/" target="_blank">换号面板 8792</a> · 当前号 / 重启雪光与桥接</div>
<div><a href="http://127.0.0.1:8781/console?token=local-token" target="_blank">记忆服务自带的控制台 8781</a> · 认的是新代码这一套</div>
</div>

<div class="foot">本页只读为主；写词表会落盘到 synonyms.py，主服务下一次请求即生效。</div>
</div>
<script>
const D=__DATA__;
document.getElementById('ts').textContent='取数 '+D.now;
document.getElementById('svc').innerHTML=D.services.map(s=>`
 <div class="card"><div class="row"><div><span class="dot ${s.up?'up':'down'}"></span>
 <span class="name">${s.name}</span></div><span class="port">:${s.port}</span></div>
 <div class="role">${s.role}</div>
 ${s.ctl?`<div style="margin-top:9px">
   <button onclick="ctl(${s.port},'start')">启动</button>
   <button onclick="ctl(${s.port},'stop')" style="margin-left:6px">停止</button></div>`
  :`<div class="role" style="margin-top:9px">这个不在这儿开关（关了我就断了）</div>`}
 </div>`).join('');
async function ctl(port,act){
 if(act==='stop' && !confirm('确定停掉 :'+port+' ？')) return;
 const r=await fetch('/api/svc',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({port,act})});
 const j=await r.json();
 if(!j.ok) alert(j.error||'失败'); 
 setTimeout(()=>location.reload(), j.ok?1200:0);
}
document.getElementById('kpi').innerHTML=`
 <div><b>${D.stats.active}</b>活跃记忆</div>
 <div><b>${D.stats.archived}</b>已归档</div>
 <div><b>${D.stats.entities}</b>实体</div>
 <div><b>${D.lines.length}</b>条归属线</div>`;
document.getElementById('lines').innerHTML=D.lines.map(l=>{
 const legacy = !/^\d{6,}$/.test(l.bot) || l.bot==='life';
 return `<tr${legacy?' style="opacity:.45"':''}><td>${l.bot}${legacy?' <span class="role">旧·未标线</span>':''}</td><td>${l.conv}</td><td>${l.n}</td></tr>`;
}).join('');
document.getElementById('syn').innerHTML=D.syn.map(s=>`
 <tr><td>${s.k}</td><td>${s.v}</td></tr>`).join('');
async function addSyn(){
 const k=document.getElementById('nk').value.trim(), v=document.getElementById('nv').value.trim();
 if(!k||!v) return;
 const r=await fetch('/api/syn',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({k,v})});
 const j=await r.json(); alert(j.ok?'加好了，刷新看':'失败：'+j.error); if(j.ok) location.reload();
}
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 安静点
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/api/data"):
            return self._send(200, json.dumps({"services": [
                {"name": n, "port": p, "role": r, "up": probe(p), "ctl": bool(c)} for n, p, r, c, _ in SERVICES],
                "lines": lines(), "syn": synonyms(), "stats": stats()},
                ensure_ascii=False), "application/json; charset=utf-8")
        return self._send(200, page())

    def _svc(self, port: int):
        for name, p, role, ctl, how in SERVICES:
            if p == port:
                return name, ctl, how
        return None, False, None

    def _kill_port(self, port: int) -> bool:
        try:
            out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=10).stdout
            pids = set()
            for ln in out.splitlines():
                if f":{port}" in ln and "LISTENING" in ln:
                    pids.add(ln.split()[-1])
            for pid in pids:
                subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True, timeout=10)
            return bool(pids)
        except Exception:
            return False

    def do_POST(self):
        if self.path.startswith("/api/svc"):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            port = int(body.get("port") or 0)
            act = str(body.get("act") or "")
            name, ctl, how = self._svc(port)
            if not ctl:
                return self._send(200, json.dumps({"ok": False, "error": f"{name} 不在本页管，别在这儿关它"},
                                                  ensure_ascii=False), "application/json; charset=utf-8")
            if act == "stop":
                self._kill_port(port)
                return self._send(200, json.dumps({"ok": True}, ensure_ascii=False), "application/json; charset=utf-8")
            if act == "start":
                if probe(port):
                    return self._send(200, json.dumps({"ok": True, "note": "本来就在跑"}, ensure_ascii=False),
                                      "application/json; charset=utf-8")
                try:
                    if how.get("bat"):
                        subprocess.Popen(["cmd", "/c", "start", "", how["bat"]], close_fds=True)
                    else:
                        subprocess.Popen([PYEXE, how["script"]],
                                         cwd=os.path.dirname(how["script"]),
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    time.sleep(1.2)
                    return self._send(200, json.dumps({"ok": True, "up": probe(port)}, ensure_ascii=False),
                                      "application/json; charset=utf-8")
                except Exception as exc:
                    return self._send(200, json.dumps({"ok": False, "error": str(exc)[:120]}, ensure_ascii=False),
                                      "application/json; charset=utf-8")
            return self._send(200, json.dumps({"ok": False, "error": "act 只认 start/stop"}, ensure_ascii=False),
                              "application/json; charset=utf-8")
        if not self.path.startswith("/api/syn"):
            return self._send(404, "{}", "application/json")
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
            k = str(body.get("k") or "").strip()
            v = [x.strip() for x in str(body.get("v") or "").split("/") if x.strip()][:2]
            if not k or not v:
                raise ValueError("原词和替身都要有")
            src = open(SYN, encoding="utf-8").read()
            line = f'    "{k}": {json.dumps(v, ensure_ascii=False)},\n'
            if f'"{k}"' in src:
                raise ValueError("这个词已经有了")
            src = src.replace("TABLE: dict[str, list[str]] = {\n",
                               "TABLE: dict[str, list[str]] = {\n" + line, 1)
            open(SYN, "w", encoding="utf-8").write(src)
            return self._send(200, json.dumps({"ok": True}, ensure_ascii=False),
                              "application/json; charset=utf-8")
        except Exception as exc:
            return self._send(200, json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False),
                              "application/json; charset=utf-8")


if __name__ == "__main__":
    print(f"[console-v2] listen http://127.0.0.1:{PORT} db={DB}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
