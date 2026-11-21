"""宿主管理域控制台：本子、清单、人工账、配置与服务。

记忆调参写入同进程 settings；桥接配置先备份、原子写盘，再尝试热更新。
applied 表示已应用，pending 表示待生效；监听、进程、HTTP 响应与就绪分别报告。
会话域的记忆和日志由经过范围授权的独立接口提供。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BRIDGE_DIR = Path(os.environ.get("FISH_BRIDGE_DIR", r"C:\Users\34318\Apps\qq-bridge"))
# 桥接**真正读的那份** config 不在这里。
#
# node_modules/qq-bridge 是指向 .pnpm/qq-bridge@x.y.z_<hash>/node_modules/qq-bridge 的软链，
# 而 ESM 的 import.meta.url 会把软链解析成真实路径（实测过），于是 bridge.js 里的
# ROOT = 真实路径 —— 它读 config、写 state、找 audio 全在 .pnpm 那一层。
# 之前这里直接拼 BRIDGE_DIR/config.json，结果是：控制台改得欢，桥接一个字没看见。
# （2026-11-02 语音这轮才发现：改完 tts.enabled 重启，接口仍报「语音合成已关闭」。）
# 路径不能写死版本号（pnpm 升级就变），所以按软链解析。
BRIDGE_PKG_DIR = Path(os.environ.get(
    "FISH_BRIDGE_PKG_DIR",
    str((BRIDGE_DIR / "node_modules" / "qq-bridge").resolve()),
))
BRIDGE_CONFIG = BRIDGE_PKG_DIR / "config.json"


# 各侧的「热更新能力」边界。声明式写在这里，schema 里每个组的「即时生效 / 需重启」
# 徽标就是从这张表推出来的——不再靠前端硬编码一句「改完需重启」糊住所有旋钮。
BRIDGE_HOT_PREFIX = ("socialV2.", "slang.", "webSearch.")
BRIDGE_HOT_EXACT = ("role.maxInjectChars",)


def _bridge_hot(path: str) -> bool:
    return path.startswith(BRIDGE_HOT_PREFIX) or path in BRIDGE_HOT_EXACT


def _bridge_token() -> str:
    for cand in (BRIDGE_PKG_DIR / "state" / "console-token", BRIDGE_DIR / "state" / "console-token"):
        try:
            tok = cand.read_text(encoding="utf-8").strip()
            if tok:
                return tok
        except Exception:
            continue
    return ""


def _bridge_port() -> int:
    try:
        return int(_read_json(BRIDGE_CONFIG).get("consolePort") or 3100)
    except Exception:
        return 3100


def _bridge_base() -> str:
    return os.environ.get("FISH_BRIDGE_BASE") or f"http://127.0.0.1:{_bridge_port()}"


SECRET_HINTS = ("key", "token", "password", "credential", "secret", "authorization")


class _BadRequest(ValueError):
    """Only fixed, safe messages may cross the console boundary."""


def _require_target(target: Any, allowed: tuple[str, ...] = ("bridge", "memory")) -> str:
    if not isinstance(target, str) or target not in allowed:
        raise _BadRequest("缺少、未知或不支持的目标")
    return target


def _query_target(query: dict) -> str:
    values = query.get("target")
    if not isinstance(values, list) or len(values) != 1:
        raise _BadRequest("需要唯一的目标")
    return _require_target(values[0])


def _validate_patch(patch: Any) -> dict:
    if not isinstance(patch, dict) or any(
        not isinstance(p, str) or not p or any(not part for part in p.split(".")) for p in patch
    ):
        raise _BadRequest("patch 必须是有效点路径组成的对象")
    return patch


# ────────────────────────────────────────────────────────────── 配置读写

def _target_path(target: str) -> Path:
    _require_target(target, ("bridge",))
    return BRIDGE_CONFIG


def _read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _write_json(path: Path, data: dict, backup_tag: str = "console") -> str | None:
    """备份后原子写入，返回备份文件路径。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{backup_tag}-{stamp}")
    try:
        shutil.copy2(path, backup)
    except Exception:
        backup = None
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return str(backup) if backup else None


def _get_path(data: dict, dotted: str) -> Any:
    cur: Any = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _set_path(data: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur = data
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _mask(data: Any, key: str = "") -> Any:
    """Show that a secret is configured without returning any part of its value."""
    if key.lower().endswith(SECRET_HINTS) or key.lower() in ('tokens', 'credentials', 'secrets'):
        return "••••" if data else ""
    if isinstance(data, dict):
        return {k: _mask(v, k) for k, v in data.items()}
    if isinstance(data, list):
        return [_mask(v, key) for v in data]
    return data


# 记忆库里的调参（存在 settings 表，改完立刻生效，不用重启）
def _settings_tree() -> dict:
    from . import server as srv

    tree: dict = {}
    for key, value in srv.service().store.all_settings().items():
        _set_path(tree, key, value)
    return tree


def _settings_set(dotted: str, value: Any) -> Any:
    """dotted 形如 intent.tuning.threshold → settings['intent.tuning']['threshold']。"""
    from . import server as srv

    key, _, field = dotted.rpartition(".")
    bucket = srv.service().store.get_setting(key, {}) or {}
    if not isinstance(bucket, dict):
        bucket = {}
    old = bucket.get(field)
    bucket[field] = value
    srv.service().store.set_setting(key, bucket)
    return old


# ────────────────────────────────────────────────────────────── 服务定义
# 每个服务的启停方式都不一样，所以在这里显式写清楚，不要试图通用化。
#
# 关键事实（2026-11-02 实测）：
#   · bridge 有一个 .bat 自愈循环（进程退出后 bat 重新拉起）
#     → 只杀监听进程没用，必须连父 cmd 一起杀，否则「关闭」后自己又起来了
#   · 所有服务还被 C:\Users\34318\dsh-companion.ps1 每 4 秒轮询拉起
#     → 必须先写停机名单 dsh-companion.off，否则杀了 4 秒后又活
# memory 的旧启动入口不能证明属于当前实例，只保留观测，不提供启停。

SERVICES = [
    {
        "id": "memory", "name": "记忆大脑", "port": None,
        "role": "记忆 / 生活层 / 清单 / 意图",
        "off_key": None, "controllable": False, "restart_mode": None,
        "guard_bat": None, "start": None,
        "warn": "当前实例未核实受控启动入口，启停及重启不可用。",
    },
    {
        "id": "bridge", "name": "QQ 桥接", "port": 3100,
        "role": "私聊 · 群聊 · 人格 · 工具",
        "off_key": "bridge", "controllable": True, "restart_mode": "stopstart",
        "guard_bat": "start-bridge.bat",
        "start": {"exe": "cmd.exe", "args": ["/c", r"C:\Users\34318\Apps\qq-bridge\start-bridge.bat"], "cwd": r"C:\Users\34318\Apps\qq-bridge"},
        "warn": "关掉后 QQ 里的鱼小满会完全消失（不再回消息）。不会掉登录，重启后接上。",
    },
    {
        "id": "tts", "name": "语音合成", "port": 8001,
        "role": "文字 → 她的声音（BreezeBlue 云端克隆）",
        "off_key": "tts", "controllable": True, "restart_mode": "stopstart",
        "guard_bat": r"E:\kk-tts\run-api-hidden.bat",
        "start": {"exe": "cmd.exe", "args": ["/c", r"E:\kk-tts\run-api-hidden.bat"], "cwd": r"E:\kk-tts"},
        "warn": "关掉后她说不出话（qq_speak 会失败并退回打字）。纯云端，重启约 1 秒就 ready，不用等模型。",
    },
    {
        # 2026-11-06 补登记：dsh-companion.ps1 一直在管它（Test-Off 'img' + 8021），
        # 但控制台的子系统页面里根本没有这一条，图片生成挂了只能靠猜。
        "id": "imgapi", "name": "生图 API", "port": 8021,
        "role": "在线文生图 · /generate 和 OpenAI 兼容接口",
        "off_key": "img", "controllable": True, "restart_mode": "stopstart",
        "guard_bat": None,
        "start": {"exe": "cmd.exe", "args": ["/c", r"E:\img-api\run-api-hidden.bat"], "cwd": r"E:\img-api"},
        "warn": "关掉后她生成不了新图（已经生成的还在 outputs 里）。守护进程认的键是 img。",
    },
    {
        "id": "snowluma", "name": "SnowLuma", "port": 3000,
        "role": "QQ 协议端",
        "off_key": "snowluma", "controllable": True, "restart_mode": "stopstart",
        "guard_bat": None,
        # 2026-11-05：原来 start=None 只能等守护进程拉；companion 一死就彻底点不动
        # （点「启动」干等 25 秒必失败，按钮还是灰的）。这里补上直接启动命令，
        # 与 companion 的 Start-SnowLuma 完全一致（node index.mjs，cwd=SnowLuma）。
        "start": {"exe": r"E:\dsh\DSH NEXT\resources\runtime\x64\primary-runtime\dependencies\node\bin\node.exe",
                  "args": ["index.mjs"], "cwd": r"C:\Users\34318\SnowLuma"},
        "warn": "协议端一停，QQ 就掉线。建议只在排障时关，关完用「启动」恢复。",
    },
    {
        "id": "dsh", "name": "DSH 会话", "port": 19387,
        "role": "底层 agent 运行时（桥接依赖）",
        "off_key": None, "controllable": False, "restart_mode": None,
        "guard_bat": None, "start": None,
        "warn": "宿主程序，关掉会把整个桌面端带走，不提供开关。",
    },
    {
        # 2026-11-06 补登记：她读网页/截图用的常驻浏览器，开机由 start-hidden.vbs 拉起，
        # 守护进程不管它（companion 里没有这一项），所以停机名单对它无效。
        "id": "browser", "name": "浏览器服务", "port": 8030,
        "role": "常驻 Edge · 读网页 / 截图 / 捞图",
        "off_key": None, "controllable": True, "restart_mode": "stopstart",
        "guard_bat": None,
        "start": {"exe": "wscript.exe", "args": [r"E:\qq-browser\start-hidden.vbs"], "cwd": r"E:\qq-browser"},
        "warn": "守护进程不管这个；关掉后不会自己回来，得点「启动」。关掉后她读不了网页。",
    },
    {
        "id": "pet", "name": "桌面宠物", "port": None, "proc": "dsh-pet",
        "role": "桌面上的那只 · 跟着 DSH 一起起落",
        "off_key": None, "controllable": False, "restart_mode": None,
        "guard_bat": None, "start": None,
        "warn": "守护进程负责：DSH 在它才在，DSH 重启它会跟着换一个。",
    },
    {
        "id": "qq", "name": "QQ 客户端", "port": None, "proc": "qq",
        "role": "QQ 本体 · 协议端要靠它登着",
        "off_key": None, "controllable": False, "restart_mode": None,
        "guard_bat": None, "start": None,
        "warn": "它退了桥接就掉线；守护进程发现不在会拉起来。",
    },
    {
        "id": "companion", "name": "守护进程", "port": None,
        "role": "每 4 秒检查并拉起所有服务",
        "off_key": None, "controllable": True, "restart_mode": "companion",
        "guard_bat": None, "start": None,
        "warn": "守护进程本身。重启后 4 秒内会核对一遍所有服务状态。",
    },
]

COMPANION_SCRIPT = Path.home() / "dsh-companion.ps1"
COMPANION_OFF = Path.home() / "dsh-companion.off"

# 从服务进程里 spawn 子进程，必须彻底脱离父进程，否则 memory-core 重启会把它们带走
_DETACHED = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW


def _probe(port: int, timeout: float = 0.35, *, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def _listen_pids(port: int) -> list[int]:
    # 2026-11-06：netstat 被拦/没输出时 stdout 会是 None，原来是直接 .splitlines() 崩掉，
    # /api/console/services 整个 500，子系统页面白屏只剩一行 AttributeError。
    try:
        cp = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                            errors="replace", timeout=10, creationflags=0x08000000)
        out = cp.stdout or ""
    except Exception:
        return []
    pids = set()
    for line in out.splitlines():
        if "LISTENING" not in line or f":{port} " not in line.replace("\t", " "):
            continue
        tail = line.split()
        if tail and tail[-1].isdigit():
            pids.add(int(tail[-1]))
    return sorted(pids)


def _procs() -> dict[int, dict]:
    """一次拿全量进程表（含命令行），用来判断父进程是不是自愈用的 bat。"""
    ps = ("Get-CimInstance Win32_Process | "
          "Select-Object ProcessId,ParentProcessId,Name,CommandLine | ConvertTo-Json -Compress")
    try:
        out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                             capture_output=True, text=True, errors="replace", timeout=25,
                             creationflags=0x08000000).stdout
        data = json.loads(out or "[]")
        if isinstance(data, dict):
            data = [data]
        return {int(p["ProcessId"]): p for p in data if p.get("ProcessId")}
    except Exception:
        return {}


def _off_keys() -> set[str]:
    try:
        if not COMPANION_OFF.exists():
            return set()
        return {ln.strip() for ln in COMPANION_OFF.read_text(encoding="utf-8").splitlines() if ln.strip()}
    except Exception:
        return set()


def _off_set(key: str, on: bool) -> None:
    keys = _off_keys()
    if on:
        keys.add(key)
    else:
        keys.discard(key)
    if keys:
        COMPANION_OFF.write_text("\n".join(sorted(keys)) + "\n", encoding="utf-8")
    elif COMPANION_OFF.exists():
        COMPANION_OFF.unlink()


def _kill(pid: int) -> bool:
    try:
        r = subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True,
                           timeout=10, creationflags=0x08000000)
        return r.returncode == 0
    except Exception:
        return False


def _spawn(spec: dict) -> int | None:
    """普通服务用：单个 detached 进程，不等待。"""
    fh = None
    try:
        p = subprocess.Popen(
            [spec["exe"], *spec.get("args", [])],
            cwd=spec.get("cwd") or None,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x08000000 | 0x00000200, close_fds=True,
        )
        return p.pid
    except Exception:
        return None


def _spawn_variants(spec: dict, log_path: str) -> list[dict]:
    """按可靠性顺序试几种脱离方式，返回每次尝试的结果。

    踩过的坑：DETACHED_PROCESS 和 CREATE_NO_WINDOW 同时用，powershell 会
    静默退出（pid 存在、1 秒后没了、日志一个字都没有）。所以这里分开试，
    谁活着就用谁。
    """
    base = [spec["exe"], *spec.get("args", [])]
    plans = [
        ("no_window", base, 0x08000000 | 0x00000200),          # CREATE_NO_WINDOW | NEW_PROCESS_GROUP
        ("detached", base, 0x00000008),                        # DETACHED_PROCESS
        ("cmd_start", ["cmd.exe", "/c", "start", "", *base], 0x08000000),
    ]
    attempts: list[dict] = []
    for name, argv, flags in plans:
        try:
            fh = open(log_path, "ab")
            try:
                p = subprocess.Popen(argv, cwd=spec.get("cwd") or None,
                                     stdin=subprocess.DEVNULL, stdout=fh, stderr=fh,
                                     creationflags=flags, close_fds=True)
            finally:
                fh.close()
        except Exception as exc:
            attempts.append({"mode": name, "pid": None, "error": "启动失败"})
            continue
        time.sleep(1.5)
        code = p.poll()
        attempts.append({"mode": name, "pid": p.pid, "exit": code})
        if code is None:  # 活过 1.5 秒就认为站住了
            break
    return attempts


def _svc(svc_id: str) -> dict:
    for s in SERVICES:
        if s["id"] == svc_id:
            if svc_id == "memory":
                from . import server as srv
                return {**s, "port": srv.PORT, "host": srv.HOST}
            return s
    raise _BadRequest("缺少或未知服务")


def _allowed_actions(svc: dict) -> list[str]:
    # A current endpoint does not prove that any launcher controls this instance.
    if svc["id"] == "memory" or not svc.get("controllable"):
        return []
    if svc["id"] == "companion":
        return ["start", "restart"]
    return ["start", "stop", "restart"] if svc.get("start") else []


def _wait_port(port: int, want_up: bool, seconds: float = 18.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _probe(port) == want_up:
            return True
        time.sleep(0.7)
    return _probe(port) == want_up


def _svc_state(svc: dict, procs: dict[int, dict] | None = None) -> dict:
    current = svc["id"] == "memory"
    if current:
        from . import server as srv
        svc = {**svc, "port": srv.PORT, "host": srv.HOST}
    port = svc.get("port")
    host = svc.get("host") or "127.0.0.1"
    listening = None
    basis = "listener_pids"
    if port:
        listening = _probe(port, host=host)
        pids = _listen_pids(port)
    elif svc.get("proc"):
        # 2026-11-06：没有端口的服务（桌宠、QQ 客户端）按进程名认。
        # 之前只有「守护进程」这一种无端口形态，其余无端口条目会被误判成守护进程的 pid。
        table = procs if procs is not None else _procs()
        key = str(svc["proc"]).lower()
        pids = sorted(pid for pid, p in table.items()
                      if key in (p.get("Name") or "").lower())
        basis = "process_name"
    else:
        table = procs if procs is not None else _procs()
        pids = _companion_pids(table)
        basis = "companion_file"
    identified = os.getpid() in pids if current else bool(pids)
    actions = _allowed_actions(svc)
    url_host = f"[{host}]" if ":" in host else host
    notice = "受控入口已登记；应用就绪未核实。" if actions else (svc.get("warn") or "只读观测，不提供启停。")
    return {
        "id": svc["id"], "name": svc["name"], "port": port, "role": svc["role"],
        "address": f"http://{url_host}:{port}" if port else None,
        "listening": listening,
        "process_identified": identified,
        "process": {"identified": identified, "running": True if current else (bool(pids) if not port or pids else None),
                    "pids": [os.getpid()] if current else pids,
                    "basis": "current_instance" if current else basis},
        "http": {"responding": True if current else None,
                 "basis": "current_request" if current else "not_checked"},
        "readiness": "unknown", "readiness_checked": False,
        # Compatibility observation only: TCP/visible process is not application health.
        "up": bool(listening) if port else bool(pids), "pids": pids,
        "off": bool(svc.get("off_key") and svc["off_key"] in _off_keys()),
        "allowed_actions": actions, "controllable": bool(actions),
        "control_notice": notice,
        "control_reason": "registered_entry" if actions else ("unverified_current_entry" if current else "read_only"),
        "restart_mode": svc.get("restart_mode") if "restart" in actions else None,
        "warn": svc.get("warn") or "",
        "can_start": "start" in actions,
    }


def _companion_pids(procs: dict[int, dict] | None = None) -> list[int]:
    """只认 `-File ...dsh-companion.ps1` 的实例。

    注意：用 `-Command` 的查询脚本，命令行里也会出现这个文件名，
    早先按字符串包含判断，把自己也算成了守护进程，导致「明明有实例」的假象。
    """
    out = []
    table = procs if procs is not None else _procs()
    for pid, p in table.items():
        cmd = p.get("CommandLine") or ""
        if "-File" in cmd and "dsh-companion.ps1" in cmd:
            out.append(pid)
    return sorted(out)


def _companion_alive() -> bool:
    return bool(_companion_pids())


def _start_companion() -> dict:
    """重启守护进程。必须在服务进程里 spawn，工具沙箱会把子进程一起回收。"""
    old = _companion_pids()
    for pid in old:
        _kill(pid)
    time.sleep(1.5)
    boot_log = str(Path.home() / "dsh-companion-boot.log")
    attempts = _spawn_variants({"exe": "powershell.exe", "args": [
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
        "-File", str(COMPANION_SCRIPT)], "cwd": str(Path.home())}, boot_log)
    alive: list[int] = []
    for _ in range(10):  # 最多等 8 秒，守护脚本要连打几个端口探测，别催
        time.sleep(0.8)
        alive = _companion_pids()
        if alive:
            break
    return {"killed": old, "attempts": attempts, "alive": alive, "ok": bool(alive),
            "boot_log": boot_log}


def _stop_service(svc: dict) -> dict:
    log = []
    if svc.get("off_key"):
        _off_set(svc["off_key"], True)
        log.append(f"写入停机名单 {svc['off_key']}")
    procs = _procs()
    pids = _listen_pids(svc["port"])
    targets: list[int] = list(pids)
    guard = (svc.get("guard_bat") or "").lower()
    for pid in pids:
        parent = procs.get(int((procs.get(pid) or {}).get("ParentProcessId") or 0)) or {}
        cmd = (parent.get("CommandLine") or "").lower()
        if guard and guard in cmd:
            targets.append(int(parent["ProcessId"]))
            log.append(f"连自愈循环一起停（{svc['guard_bat']}）")
    for pid in dict.fromkeys(targets):
        log.append(f"结束进程 {pid} ({'成功' if _kill(pid) else '失败/已退出'})")
    up = _wait_port(svc["port"], False, 8)
    log.append("端口已释放" if up else "端口仍被占用")
    return {"log": log}


def _start_service(svc: dict) -> dict:
    log = []
    if svc.get("off_key"):
        _off_set(svc["off_key"], False)
        log.append(f"移出停机名单 {svc['off_key']}")
    if _probe(svc["port"]):
        log.append("已经在跑，不用启动")
        return {"log": log}
    spec = svc.get("start")
    if spec:
        pid = _spawn(spec)
        log.append(f"已拉起 {spec['exe']}（pid {pid}）" if pid else "启动失败")
    else:
        log.append("交给守护进程拉起（最多 5 秒）")
    ok = _wait_port(svc["port"], True, 25)
    log.append("端口已就绪" if ok else "等了 25 秒还没起来，去看日志")
    return {"log": log}


def _service_action(svc_id: str, action: str) -> dict:
    svc = _svc(svc_id)
    if action not in ("start", "stop", "restart"):
        raise _BadRequest("缺少或未知服务动作")
    if action not in _allowed_actions(svc):
        raise _BadRequest("该服务不支持此动作或受控入口未核实")

    if svc_id == "companion":
        return {"result": _start_companion(), "log": []}

    if action == "stop":
        out = _stop_service(svc)
        out["state"] = _svc_state(svc)
        return out

    if action == "start":
        out = _start_service(svc)
        out["state"] = _svc_state(svc)
        return out

    if action == "restart":
        out = _stop_service(svc)
        time.sleep(1.0)
        out["log"].extend(_start_service(svc)["log"])
        out["state"] = _svc_state(svc)
        return out

    raise _BadRequest("未知服务动作")


def _http(url: str, method: str = "GET", payload: dict | None = None, timeout: float = 6.0,
          headers: dict | None = None) -> Any:
    body = None
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except Exception:
        return raw


def _bridge_post(path: str, payload: dict, timeout: float = 12.0) -> Any:
    tok = _bridge_token()
    if not tok:
        raise RuntimeError("读不到桥接控制台令牌（state/console-token）")
    return _http(_bridge_base() + path, "POST", payload, timeout=timeout,
                 headers={"x-console-token": tok})


def _hot_apply_bridge(patch: dict) -> tuple[list[str], list[str], list[str]]:
    """把刚写进 config.json 的值推给正在跑的桥接。

    返回 (applied, pending, errors)。applied = 运行时已拿到新值；pending = 只能靠重启。
    """
    applied: list[str] = []
    pending: list[str] = []
    errors: list[str] = []
    paths = list(patch.keys())

    sv2 = [p for p in paths if p.startswith("socialV2.")]
    if sv2:
        try:
            # 空 body = 「只重读盘并把 socialV2 热合并进内存」，顺带按新参数重排唤醒定时器
            _bridge_post("/api/socialV2/config", {})
            applied += sv2
        except Exception as exc:  # noqa: BLE001
            pending += sv2
            errors.append("socialV2 热合并失败")

    if "role.maxInjectChars" in patch:
        try:
            _bridge_post("/api/roles/limit", {"maxInjectChars": int(patch["role.maxInjectChars"])})
            applied.append("role.maxInjectChars")
        except Exception as exc:  # noqa: BLE001
            pending.append("role.maxInjectChars")
            errors.append("人格注入上限热更新失败")

    slang = {p.split(".", 1)[1]: patch[p] for p in paths if p.startswith("slang.")}
    if slang:
        try:
            _bridge_post("/api/slang/config", slang)
            applied += [f"slang.{k}" for k in slang]
        except Exception as exc:  # noqa: BLE001
            pending += [f"slang.{k}" for k in slang]
            errors.append("黑话配置热更新失败")

    for p in paths:
        if p.startswith("webSearch."):
            applied.append(p)      # web-budget 每次调用都重读 config.json，写盘即生效
        elif p not in applied and p not in pending:
            pending.append(p)
    return applied, pending, errors


def _hot_apply(target: str, patch: dict, data: dict | None = None) -> tuple[list[str], list[str], list[str]]:
    _require_target(target, ("bridge",))
    return _hot_apply_bridge(patch)


# ────────────────────────────────────────────────────────────── 漂移检测
#
# 「文件里写着 A、跑着的还是 B」是这套系统最容易骗人的状态：面板读的是文件，
# 所以它永远显示你刚填的值，看上去很对。这个函数拿运行时的真值来对，对不上就报出来。

def _live_bridge_snapshot() -> dict:
    tok = _bridge_token()
    if not tok:
        raise RuntimeError("读不到桥接控制台令牌")
    d = _http(_bridge_base() + "/api/socialV2/config", "GET", None, timeout=6.0,
              headers={"x-console-token": tok})
    if not isinstance(d, dict) or not d.get("ok"):
        raise RuntimeError("桥接没返回配置")
    role = d.get("roleInject") or {}
    return {
        "socialV2": d.get("config") or {},
        "role": {"maxInjectChars": role.get("maxInjectChars")} if role else {},
    }


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 1e-9
    if a is None or b is None:
        return a == b
    return str(a) == str(b)


def _drift(target: str) -> dict:
    _require_target(target)
    if target == "memory":
        return {"checked": False, "error": "该目标没有独立运行时可比", "diffs": []}
    try:
        live = _live_bridge_snapshot()
        file_cfg = _read_json(BRIDGE_CONFIG)
    except Exception as exc:  # noqa: BLE001
        return {"checked": False, "error": "运行时配置核对失败", "diffs": []}

    diffs = []
    for g in SCHEMA:
        if g["target"] != target:
            continue
        for f in g["fields"]:
            p = f["path"]
            if target == "bridge" and not (p.startswith("socialV2.") or p == "role.maxInjectChars"):
                continue
            lv = _get_path(live, p)
            if lv is None:
                continue  # 运行时那边没这个字段（未部署的键），不算漂移
            fv = _get_path(file_cfg, p)
            if not _same(fv, lv):
                secret = f.get("type") == "secret"
                key = p.rsplit('.', 1)[-1]
                diffs.append({"path": p, "label": f.get("label") or p,
                              "file": _mask(fv, 'secret' if secret else key),
                              "live": _mask(lv, 'secret' if secret else key)})
    return {"checked": True, "diffs": diffs[:60], "count": len(diffs)}


def _apply_hint(group: dict) -> dict:
    """给每个参数组标一个「真的能生效吗」的结论，前端徽标直接照着渲染。"""
    target = group.get("target")
    _require_target(target)
    if target == "memory":
        return {"apply": "hot", "why": "存在记忆库 settings 表里，下一次判定直接读"}
    paths = [f["path"] for f in group.get("fields", [])]
    if target == "bridge":
        if not _probe(_bridge_port()):
            return {"apply": "offline", "why": "未观测到桥接监听，先存盘，启动后生效"}
        cold = [p for p in paths if not _bridge_hot(p)]
        if cold:
            return {"apply": "mixed", "cold": cold, "why": f"其中 {len(cold)} 项只能重启桥接生效"}
        return {"apply": "hot", "why": "走桥接的热更新入口，保存即生效"}


# ────────────────────────────────────────────────────────────── 可调项 schema
# 每一行就是一个前端控件。type: number | percent | bool | text | ms | select | textarea
# target 指向落在哪个配置文件；path 是点路径。

SCHEMA: list[dict] = [
    {
        "id": "proactive", "title": "主动性", "target": "bridge",
        "desc": "她会不会自己开口。影子模式下只记录判定、不真的发消息，用来观察几天再上线。",
        "fields": [
            {"path": "socialV2.proactive.enabled", "label": "允许主动开口", "type": "bool"},
            {"path": "socialV2.proactive.shadow", "label": "影子模式（只记录不发送）", "type": "bool",
             "hint": "观察 2-3 天，看 /intent/wakes 的判定是否合理，再关掉"},
            {"path": "socialV2.proactive.probability", "label": "骰子概率", "type": "percent",
             "hint": "意图引擎给出「该开口」之后，还按这个概率掷一次；1.0 = 完全听引擎的"},
            {"path": "socialV2.proactive.idleThresholdMs", "label": "沉默多久算「可以找她」", "type": "ms"},
            {"path": "socialV2.proactive.groupsEnabled", "label": "群里也主动", "type": "bool"},
            {"path": "socialV2.proactive.checkIntervalMinMs", "label": "检查间隔下限", "type": "ms"},
            {"path": "socialV2.proactive.checkIntervalMaxMs", "label": "检查间隔上限", "type": "ms"},
        ],
    },
    {
        "id": "intent", "title": "意图引擎", "target": "memory",
        "desc": "打分公式的阈值，存在记忆库里，改完立刻生效、不用重启任何服务。低于阈值不醒；调低更黏人，调高更冷淡。",
        "fields": [
            {"path": "intent.tuning.threshold", "label": "唤醒阈值", "type": "number", "min": 0.2, "max": 1.2, "step": 0.05,
             "hint": "默认 0.55。0.45 很黏，0.70 基本只在真有事时开口"},
            {"path": "intent.tuning.night_penalty", "label": "深夜惩罚", "type": "number", "min": 0, "max": 1, "step": 0.05,
             "hint": "0-7 点扣多少分。有硬到期的事会自动减免到 0.15"},
            {"path": "intent.tuning.noise_max", "label": "随机噪声上限", "type": "number", "min": 0, "max": 0.3, "step": 0.01,
             "hint": "只允许在这个范围内晃，不能决定结果。设 0 = 完全确定性"},
        ],
    },
    {
        "id": "rhythm", "title": "说话节奏", "target": "bridge",
        "desc": "决定她说话像不像人：长度、分条、间隔、上限。",
        "fields": [
            {"path": "socialV2.send.maxMessageChars", "label": "单条上限（字）", "type": "number", "min": 20, "max": 2000, "step": 10},
            {"path": "socialV2.send.burstMaxMessages", "label": "一次最多几条", "type": "number", "min": 1, "max": 20, "step": 1},
            {"path": "socialV2.send.longGapProbability", "label": "长停顿概率", "type": "percent"},
            {"path": "socialV2.send.maxSendPerHour", "label": "每小时上限", "type": "number", "min": 1, "max": 500, "step": 5},
            {"path": "socialV2.send.maxSendPerDay", "label": "每天上限（私聊）", "type": "number", "min": 10, "max": 5000, "step": 50},
        ],
    },
    {
        "id": "wake", "title": "睡眠与唤醒", "target": "bridge",
        "desc": "她一次睡多久、什么时候愿意醒。",
        "fields": [
            {"path": "socialV2.wake.defaultMode", "label": "默认模式", "type": "select",
             "options": [{"v": "diving", "t": "潜水（可被叫醒）"}, {"v": "active", "t": "完全在线"}]},
            {"path": "socialV2.wake.sleepMinMs", "label": "最短睡", "type": "ms"},
            {"path": "socialV2.wake.recommendedSleepMinMs", "label": "建议睡眠下限", "type": "ms"},
            {"path": "socialV2.wake.recommendedSleepMaxMs", "label": "建议睡眠上限", "type": "ms"},
            {"path": "socialV2.wake.maxWakePerHour", "label": "每小时最多醒几次", "type": "number", "min": 1, "max": 120, "step": 1},
            {"path": "socialV2.wake.noActionLimit", "label": "连续空转几次就强制睡", "type": "number", "min": 1, "max": 20, "step": 1},
        ],
    },
    {
        "id": "memory", "title": "记忆接入", "target": "bridge",
        "desc": "桥接怎么用记忆大脑：检索几条、生活层多久推一步。",
        "fields": [
            {"path": "socialV2.memoryBrain.enabled", "label": "接入记忆大脑", "type": "bool"},
            {"path": "socialV2.memoryBrain.k", "label": "每次检索几条", "type": "number", "min": 1, "max": 20, "step": 1},
            {"path": "socialV2.memoryBrain.lifeStepMinutes", "label": "生活层推进间隔", "type": "ms"},
            {"path": "role.maxInjectChars", "label": "人格注入上限（字）", "type": "number", "min": 1000, "max": 60000, "step": 1000},
        ],
    },
    {
        "id": "media", "title": "图片与表情", "target": "bridge",
        "desc": "看图、发表情包、语音的能力开关。",
        "fields": [
            {"path": "socialV2.media.describe.enabled", "label": "能看懂图", "type": "bool"},
            {"path": "socialV2.media.describe.model", "label": "识图模型", "type": "text"},
            {"path": "socialV2.media.maxCount", "label": "单条最多几张图", "type": "number", "min": 1, "max": 10, "step": 1},
            {"path": "socialV2.sticker.enabled", "label": "会发 QQ 表情", "type": "bool"},
            {"path": "socialV2.meme.enabled", "label": "会发梗图", "type": "bool"},
            {"path": "socialV2.voice.enabled", "label": "会发语音", "type": "bool"},
        ],
    },
    {
        "id": "desktop", "title": "桌面（看屏幕 / 动鼠标）", "target": "bridge",
        "desc": "她能不能看你这块屏幕、动你的鼠标键盘。只对主人本人的私聊生效——"
                "群聊和别人私聊拿着自己的令牌也会被桥接拒掉，这是代码判定，不是提示词。"
                "默认关；关掉时她连这个工具都调不动。",
        "fields": [
            {"path": "socialV2.tools.desktop", "label": "允许操作桌面", "type": "bool",
             "hint": "打开后：你私聊里她能截屏看懂画面、点鼠标、敲键盘、切窗口、开关应用。"
                     "硬拦截仍在（写死在 desktop_ctl.py）：不许锁屏、不许关机/注销、不许起命令行/注册表/任务管理器"},
        ],
    },
    {
        "id": "extras", "title": "周边能力", "target": "bridge",
        "desc": "朋友圈、黑话学习、联网搜索。",
        "fields": [
            {"path": "socialV2.qzone.enabled", "label": "看/发空间", "type": "bool"},
            {"path": "socialV2.qzone.watchFeedsIntervalMs", "label": "刷空间间隔", "type": "ms"},
            {"path": "slang.enabled", "label": "学黑话", "type": "bool"},
            {"path": "slang.injectMax", "label": "注入几条黑话", "type": "number", "min": 0, "max": 30, "step": 1},
            {"path": "webSearch.budget.dailyCalls", "label": "每天联网搜索条数", "type": "number", "min": 0, "max": 200, "step": 1},
            {"path": "webSearch.maxResults", "label": "每次搜几条", "type": "number", "min": 1, "max": 10, "step": 1},
        ],
    },
    {
        "id": "voice-tts", "title": "说话（合成语音）", "target": "bridge",
        "desc": "她用自己的声音说话：那句话交给 BreezeBlue（8001）合成，再当语音条发出去。"
                "每句带一段语气指令，所以同一句话用不同口气念出来是两回事。",
        "fields": [
            {"path": "socialV2.tts.enabled", "label": "能用语音说话", "type": "bool",
             "hint": "关掉后 qq_speak 不会出现在她手里，她只能打字"},
            {"path": "socialV2.tools.speak", "label": "qq_speak 工具", "type": "bool",
             "hint": "总开关之外再放一道：想临时收掉这个工具但保留配置时用"},
            {"path": "socialV2.voice.enabled", "label": "允许发语音条", "type": "bool",
             "hint": "发送侧总闸。关了这个，合成出来也发不出去"},
            {"path": "socialV2.tts.voice", "label": "音色", "type": "text",
             "hint": "以 127.0.0.1:8001 的 voice.json 为准：去试听台点「设为默认」最省事（内置 100 个 + refs/ 里的克隆音色）。这里填的值只在服务端 pin=false 时生效"},
            {"path": "socialV2.tts.speed", "label": "语速", "type": "number", "min": 0.5, "max": 2, "step": 0.05,
             "hint": "1 = 原速。0.95 会显得慢一点、软一点（同样以 voice.json 为准）"},
            {"path": "socialV2.tts.emo", "label": "语气怎么判", "type": "select",
             "options": [
                 {"v": "auto", "t": "自动（按每句话判）"},
                 {"v": "neutral", "t": "钉死：平常"},
                 {"v": "soft", "t": "钉死：温柔"},
                 {"v": "coq", "t": "钉死：撒娇"},
                 {"v": "wronged", "t": "钉死：委屈"},
                 {"v": "sad", "t": "钉死：低落"},
                 {"v": "happy", "t": "钉死：轻快"},
                 {"v": "pout", "t": "钉死：嘴硬"},
                 {"v": "angry", "t": "钉死：生气"},
                 {"v": "cold", "t": "钉死：冷淡"},
                 {"v": "sultry", "t": "钉死：撩人"},
                 {"v": "intimate", "t": "钉死：亲密"},
             ],
             "hint": "auto = 她自己填（qq_speak 的 emo 参数），没填就按文字和她当时的心情猜。钉死某个值只适合试音色，日常会全程一个调子"},
            {"path": "socialV2.tts.guidance", "label": "表演强度", "type": "number", "min": 0, "max": 10, "step": 1,
             "hint": "0 = 跟着情绪强度自动算（2~5）。数字越大越夸张，超过 6 会开始不像她，像配音演员"},
            {"path": "socialV2.tts.maxChars", "label": "一句话最多多少字", "type": "number", "min": 10, "max": 400, "step": 10,
             "hint": "默认 100 字≈25 秒。超过会被截断——语音太长没人听完"},
            {"path": "socialV2.voice.maxPerMinute", "label": "每分钟最多几条", "type": "number", "min": 0, "max": 10, "step": 1},
            {"path": "socialV2.voice.maxPerHour", "label": "每小时最多几条", "type": "number", "min": 0, "max": 60, "step": 1,
             "hint": "默认 6。语音跟文字共用这个额度"},
            {"path": "socialV2.tts.timeoutMs", "label": "合成超时", "type": "ms",
             "hint": "默认 25 秒。换克隆音色（F5）时冷启动要 60 秒，建议调到 45000 以上，或先去试听台点一下预热"},
        ],
    },
]

# QQ 侧的会话级模型路由（结构是动态的 conversationProfiles.<convKey>.*），单独处理
MODEL_PROVIDERS = ["qwen-token-plan-cn", "deepseek-account", "my-relay-model"]


# ────────────────────────────────────────────────────────────── 采集


def _loops(status: str = "open", limit: int = 30) -> list[dict]:
    from . import server as srv
    return srv.service().store.list_loops(status=status, limit=limit)


# ────────────────────────────────────────────────────────────── 动作

def _action(name: str, body: dict) -> dict:
    if name not in ("memory.maintenance", "intent.simulate"):
        raise _BadRequest("缺少或未知动作")
    from . import server as srv

    if name == "memory.maintenance":
        return {"result": srv.maintenance().run()}
    if name == "intent.simulate":
        # 只算分不落账，用来在调阈值之前先看一眼
        idle = float(body.get("idle_seconds") or 3600)
        conv = str(body.get("conv") or "private:3431816247")
        return {"result": srv.intent().decide(conv_key=conv, idle_seconds=idle, record=False)}


ACTIONS = [
    {"name": "intent.simulate", "label": "试算一次唤醒判定", "desc": "不落账，只看现在的分和理由"},
    {"name": "memory.maintenance", "label": "跑一次记忆维护", "desc": "衰减 / 归档 / 清孤儿实体"},
]


# ────────────────────────────────────────────────────────────── 路由

def handle(method: str, path: str, query: dict, body: dict) -> tuple[int, dict]:
    """返回 (status_code, payload)，错误仅使用固定消息。"""
    try:
        if method == "GET":
            if path in {"/api/console/overview", "/api/console/memories", "/api/console/memory",
                        "/api/console/raw", "/api/console/recall", "/api/console/wakes", "/api/console/logs"}:
                return 410, {"status": "error", "data": {"error": "unscoped_read_disabled"}}
            if path == "/api/console/schema":
                groups = [{**g, **{"apply": _apply_hint(g)}} for g in SCHEMA]
                return 200, {"status": "ok", "data": {
                    "groups": groups, "actions": ACTIONS, "providers": MODEL_PROVIDERS}}
            if path == "/api/console/drift":
                target = _query_target(query)
                return 200, {"status": "ok", "data": _drift(target)}
            if path == "/api/console/config":
                target = _query_target(query)
                if "raw" in query and query["raw"] != ["0"]:
                    raise _BadRequest("控制台只提供脱敏配置")
                if target == "memory":
                    return 200, {"status": "ok", "data": _mask(_settings_tree())}
                data = _read_json(_target_path(target))
                return 200, {"status": "ok", "data": _mask(data)}
            if path == "/api/console/services":
                table = _procs()
                return 200, {"status": "ok", "data": {
                    "services": [_svc_state(s, table) for s in SERVICES],
                    "off_keys": sorted(_off_keys() & {s["off_key"] for s in SERVICES if s.get("off_key")}),
                }}
            if path == "/api/console/loops":
                from .store import Store
                status = (query.get("status") or ["open"])[0]
                limits = {"title_chars": Store.LOOP_TITLE_CHARS, "detail_chars": Store.LOOP_DETAIL_CHARS}
                return 200, {"status": "ok", "data": {"loops": _loops(status, 40), "limits": limits}}
            if path == "/api/console/blocks":
                # 她的本子：同进程直读，不走 HTTP（省一次序列化，也避免自己调自己）
                from . import server as srv
                nb = srv.notebook()
                chars = int((query.get("chars") or ["500"])[0])
                return 200, {"status": "ok", "data": {
                    "blocks": nb.all(), "stats": nb.stats(), "rendered": nb.render(chars)}}
            if path == "/api/console/blocks/versions":
                from . import server as srv
                slug = str((query.get("block") or ["self"])[0])
                limit = min(int((query.get("limit") or ["30"])[0]), 200)
                return 200, {"status": "ok", "data": srv.notebook().versions(slug, limit)}
            if path == "/api/console/ledger":
                # 判断账（偏差档案）：同进程直读，跟本子一个路子
                from . import server as srv
                lg = srv.ledger()
                kind = (query.get("kind") or [""])[0] or None
                verdict = (query.get("verdict") or [""])[0] or None
                limit = min(int((query.get("limit") or ["60"])[0]), 300)
                return 200, {"status": "ok", "data": {
                    "records": lg.query({"kind": kind, "verdict": verdict, "limit": limit})["records"],
                    "stats": srv.service().store.ledger_stats(),
                    "reviews": srv.service().store.list_reviews(5),
                    "rendered": lg.render(),
                }}
            return 404, {"status": "error", "message": "no route"}

        if method == "POST":
            if path == "/api/console/config":
                target = _require_target(body.get("target"))
                patch = _validate_patch(body.get("patch") if body.get("patch") is not None else {})
                full = body.get("full")
                if full is not None and not isinstance(full, dict):
                    raise _BadRequest("full 必须是对象")

                if target == "memory":
                    changed = []
                    applied = []
                    for dotted, value in patch.items():
                        old = _settings_set(dotted, value)
                        if old != value:
                            changed.append(dotted)
                            applied.append(dotted)
                    return 200, {"status": "ok", "data": {
                        "changed": changed, "applied": applied, "pending": [], "errors": [], "restart": None}}

                conf_path = _target_path(target)
                data = _read_json(conf_path)
                touched: dict = {}
                if full is not None:
                    data = full
                    changed = ["<整份覆盖>"]
                    touched = {k: v for k, v in full.items() if isinstance(v, dict)}
                else:
                    changed = []
                    for dotted, value in patch.items():
                        old = _get_path(data, dotted)
                        if isinstance(old, bool) or isinstance(value, bool):
                            value = bool(value)
                        elif isinstance(old, (int, float)) or isinstance(value, (int, float)):
                            value = float(value) if isinstance(old, float) else int(value)
                        if old != value:
                            _set_path(data, dotted, value)
                            changed.append(dotted)
                        touched[dotted] = value
                if not changed:
                    return 200, {"status": "ok", "data": {
                        "changed": [], "applied": [], "pending": [], "errors": [], "restart": None}}
                backup = _write_json(conf_path, data, "console")
                # 关键一步：推给正在运行的进程，否则「已保存」只是个文件里的字
                applied, pending, errors = _hot_apply(target, touched, data)
                return 200, {"status": "ok", "data": {
                    "changed": changed, "backup": backup,
                    "applied": applied, "pending": pending, "errors": errors,
                    "restart": "qq-bridge" if pending else None,
                }}

            if path == "/api/console/hot":
                # 不写盘，只把「文件里已经是新的、运行时还是旧的」这种情况推平。
                # 给「调参」页的漂移横幅用：显示 N 项对不上 + 一个按钮直接拉齐。
                target = _require_target(body.get("target"), ("bridge",))
                patch = body.get("patch")
                data = _read_json(_target_path(target))
                if patch is None:
                    patch = {f["path"]: _get_path(data, f["path"])
                             for g in SCHEMA if g["target"] == "bridge" for f in g["fields"]}
                applied, pending, errors = _hot_apply(target, _validate_patch(patch), data)
                return 200, {"status": "ok", "data": {
                    "applied": applied, "pending": pending, "errors": errors}}

            if path == "/api/console/services":
                sid = str(body.get("id") or "")
                act = str(body.get("action") or "")
                if str(body.get("confirm") or "") != f"{act}:{sid}":
                    return 400, {"status": "error", "message": "缺少或无效的服务动作确认标记"}
                return 200, {"status": "ok", "data": _service_action(sid, act)}

            if path == "/api/console/blocks":
                # op = set / revert / clear（clear 走 set + mode=clear）
                from . import server as srv
                return 200, {"status": "ok", "data": srv.notebook().op(body)}

            if path == "/api/console/ledger":
                # op = judge / result / road / overturn / fix / drop / review / link
                from . import server as srv
                return 200, {"status": "ok", "data": srv.ledger().op(body)}

            if path == "/api/console/memory":
                from . import server as srv
                op = str(body.get("op") or "add")
                if op == "delete":
                    from .memory_delete import DeleteError
                    mode=body.get("mode","soft")
                    try:
                        ok=srv.service().store.delete_memory(body.get("id"),mode,
                            actor=body.get("actor","console"),origin="console")
                        return 200,{"status":"ok","data":{"deleted":bool(ok),"mode":mode,"undo_supported":mode=="soft"}}
                    except DeleteError as exc:
                        if mode=="purge":
                            from .memory_delete import OUTSIDE
                            exc.details.update(outside_scope=OUTSIDE,scope_notice="未确认清零；purge须停服后离线执行，范围外还有：浏览器缓存、DSH/QQ、未纳管备份。")
                        return exc.code,{"status":"error","data":{"error":exc.error,"mode":mode,**exc.details}}
                text = str(body.get("text") or "").strip()
                if len(text) < 2:
                    return 400, {"status": "error", "message": "内容太短"}
                mid = srv.service().engine.remember(
                    text,
                    kind=str(body.get("kind") or "episode"),
                    importance=float(body.get("importance") or 0.5),
                    source=str(body.get("source") or "console"),
                    visibility=str(body.get("visibility") or "shared"),
                    now=time.time(),
                )
                return 200, {"status": "ok", "data": {"id": mid}}

            if path == "/api/console/loops":
                from . import server as srv
                op = str(body.get("op") or "add")
                payload = {k: v for k, v in body.items() if k != "op"}
                return 200, {"status": "ok", "data": srv.intent().loops(op, payload)}

            if path == "/api/console/life":
                from . import server as srv
                act = str(body.get("action") or "step")
                if act == "step":
                    return 200, {"status": "ok", "data": srv.life().step(force=True)}
                if act == "set":
                    state = srv.life().state()
                    for k in ("energy", "boredom", "social", "curiosity", "play", "rest"):
                        if k in body:
                            state[k] = max(0.0, min(1.0, float(body[k])))
                    srv.life()._put("state", state)  # noqa: SLF001 控制台专用后门
                    return 200, {"status": "ok", "data": state}
                return 400, {"status": "error", "message": "未知 action"}

            if path == "/api/console/action":
                return 200, {"status": "ok", "data": _action(str(body.get("name") or ""), body)}

            if path == "/api/console/models":
                conf_path = BRIDGE_CONFIG
                data = _read_json(conf_path)
                for key, spec in (body.get("conversations") or {}).items():
                    for field in ("provider", "model", "reasoningEffort"):
                        if field in spec:
                            _set_path(data, f"socialV2.conversationProfiles.{key}.model.{field}", spec[field])
                backup = _write_json(conf_path, data, "console-models")
                return 200, {"status": "ok", "data": {"backup": backup, "restart": "qq-bridge"}}

            return 404, {"status": "error", "message": "no route"}

        return 405, {"status": "error", "message": "method not allowed"}
    except _BadRequest as exc:
        return 400, {"status": "error", "message": str(exc)}
    except (ValueError, TypeError):
        return 400, {"status": "error", "message": "请求参数无效"}
    except Exception:  # noqa: BLE001
        return 500, {"status": "error", "message": "控制台请求失败"}
