#!/usr/bin/env python3
"""一键启动开发环境：后端（uvicorn）+ 前端（vite）。

为什么要有这个脚本，而不是在 README 里写两行命令：

这个项目在 P4 期间因为启动方式翻车了三次 —— 端口被上一轮的残留进程占着、
Git Bash 找不到 `npm`、本机代理把 127.0.0.1 的请求也拦了。三条都不是代码问题，
但每一条都表现为"后端起不来 / 页面黑屏"，排查一次十分钟。
脚本把这三件事一次性处理掉，启动变成一个命令。

用法::

    python scripts/dev.py              # 起后端 + 前端
    python scripts/dev.py --app        # 起服务并打开 Chrome/Edge 独立应用窗口
    python scripts/dev.py --open       # 起服务并用默认浏览器打开
    python scripts/dev.py --backend    # 只起后端（调试接口时）
    python scripts/dev.py --no-kill    # 不清理端口（端口被别的服务占用时）

停止：Ctrl+C（会连带终止两个子进程，不留孤儿进程）。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"

BACKEND_PORT = 8000
FRONTEND_PORT = 5173
BACKEND_HEALTH = f"http://127.0.0.1:{BACKEND_PORT}/health"

#: 桌面窗口指向的地址。
#: **必须写 127.0.0.1，不能写 localhost** —— 本机 localhost 优先解析到 IPv6 `::1`，
#: 而 uvicorn 只绑 IPv4；且 CORS 白名单按 Origin 逐字节匹配，两者是两个不同的源。
#: 写成 localhost 的结果是"窗口能开、数据全空"，且现象离原因很远。
APP_URL = f"http://127.0.0.1:{FRONTEND_PORT}/"
APP_WINDOW_SIZE = "1280,860"

#: 本机 node 不在默认 PATH 里（WorkBuddy 装在自己的目录下），启动前端前要补进去
NODE_GLOB_DIR = Path(os.path.expanduser("~/.workbuddy/binaries/node/versions"))


# --------------------------------------------------------------------------- #
# 环境准备
# --------------------------------------------------------------------------- #
def find_node_bin() -> Path | None:
    """定位 node / npm 所在目录。找不到就返回 None，交给前端启动时报错。"""
    if shutil.which("npm"):
        return None
    if not NODE_GLOB_DIR.is_dir():
        return None
    # 取版本号最大的一个，而不是碰运气取第一个
    cands = sorted(
        (d for d in NODE_GLOB_DIR.iterdir() if d.is_dir() and (d / "npm.cmd").exists()),
        key=lambda d: [int(x) if x.isdigit() else x for x in d.name.replace("-", ".").split(".")],
        reverse=True,
    )
    return cands[0] if cands else None


def build_env() -> dict:
    """子进程环境：补 node 路径，并**清掉代理变量**。

    代理这一条是本机的真实坑：`http_proxy` 指向 127.0.0.1:63000，
    结果前端页面请求 8000 端口的 API 也被送去绕了一圈代理，表现就是
    "后端明明活着，页面却一直报连接失败"。
    """
    env = os.environ.copy()
    node_bin = find_node_bin()
    if node_bin:
        env["PATH"] = str(node_bin) + os.pathsep + env.get("PATH", "")
    for key in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        env.pop(key, None)
    env["no_proxy"] = "127.0.0.1,localhost"
    env["PYTHONUNBUFFERED"] = "1"
    return env


# --------------------------------------------------------------------------- #
# 端口清理
# --------------------------------------------------------------------------- #
def _run(cmd: list[str]) -> str:
    """跑一条命令并返回合并后的输出。

    Windows 的 `taskkill` / `netstat` 输出是 **GBK**，不是 UTF-8。
    不显式声明 `errors="replace"`，中文 Windows 上一解码就抛
    UnicodeDecodeError —— 而它抛在子进程读取线程里，主线程看不到，
    只会表现为"命令好像跑了但什么都没发生"。
    """
    try:
        r = subprocess.run(
            cmd, capture_output=True, timeout=20,
            text=True, encoding="utf-8", errors="replace",
        )
        return (r.stdout or "") + (r.stderr or "")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return f"<{type(e).__name__}: {e}>"


def listening_pids(port: int) -> list[int]:
    """列出正在监听指定端口的 PID。**严格按端口过滤**。

    绝不做"杀掉所有 python.exe / node.exe"这种事 —— 那会误伤这台机器上
    正在跑的其它项目（这台机器上就还有一个独立的 Python 学习笔记工程）。
    """
    if os.name == "nt":
        pids: set[int] = set()
        for line in _run(["netstat", "-ano"]).splitlines():
            parts = line.split()
            # 列序：协议 本地地址 外部地址 状态 PID
            if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3].upper() == "LISTENING":
                if parts[-1].isdigit():
                    pids.add(int(parts[-1]))
        return sorted(pids)

    out = _run(["lsof", "-ti", f"tcp:{port}"])
    return [int(x) for x in out.split() if x.isdigit()]


def kill_port(port: int) -> bool:
    """释放端口，返回它最终是否空闲。

    这里不用 PowerShell 而用 `taskkill`：从脚本里启动的 PowerShell 子进程
    权限不足以终止*另一个会话*拉起的进程（静默失败，端口原封不动），
    而 `taskkill` 可以。这个坑导致过"脚本说清理完了，vite 却报端口被占"。
    """
    pids = listening_pids(port)
    if not pids:
        return True

    for pid in pids:
        if pid == os.getpid():
            continue
        if os.name == "nt":
            _run(["taskkill", "/F", "/PID", str(pid)])
        else:
            _run(["kill", "-9", str(pid)])
        print(f"  端口 {port} 原被进程 {pid} 占用，已终止")

    # 端口释放有延迟，杀完立刻起服务会撞上 "Address already in use"
    for _ in range(25):
        time.sleep(0.25)
        if not listening_pids(port):
            return True
    print(f"  警告：端口 {port} 仍被占用（{listening_pids(port)}）", file=sys.stderr)
    return False


def probe(url: str, timeout: float = 40.0) -> bool:
    """轮询一个地址直到它返回 200。走无代理 opener，避免被本机代理干扰。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with opener.open(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.4)
    return False


# --------------------------------------------------------------------------- #
# 子进程
# --------------------------------------------------------------------------- #
def pump(prefix: str, fh) -> None:
    """把子进程输出加上前缀转发到 stdout —— 两个服务的日志混在一起时看得清是谁的。"""
    for line in iter(fh.readline, ""):
        sys.stdout.write(f"[{prefix}] {line.rstrip()}\n")
        sys.stdout.flush()


def spawn_backend(env: dict) -> subprocess.Popen:
    """起后端。

    抽成函数是为了让 `desktop.py` 能原样复用同一套启动参数 ——
    桌面窗口只是"换了个壳"，服务的启动方式必须与 Web 路径完全一致，
    否则同一份代码会有两套行为，出问题时要对着两处排查。
    """
    print(f"启动后端 → http://127.0.0.1:{BACKEND_PORT}")
    p = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(BACKEND_PORT)],
        cwd=BACKEND, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace",
    )
    threading.Thread(target=pump, args=("后端", p.stdout), daemon=True).start()
    return p


def spawn_frontend(env: dict, npm: str) -> subprocess.Popen:
    print(f"启动前端 → http://127.0.0.1:{FRONTEND_PORT}")
    p = subprocess.Popen(
        [npm, "run", "dev", "--", "--host", "127.0.0.1", "--port", str(FRONTEND_PORT)],
        cwd=FRONTEND, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace",
    )
    threading.Thread(target=pump, args=("前端", p.stdout), daemon=True).start()
    return p


# --------------------------------------------------------------------------- #
# 桌面化呈现之一：浏览器自带的 --app 独立窗口
#
# 刻意**不引入** Tauri / Electron：那会把一个纯 Web 项目变成需要编译的桌面项目，
# 既破坏"clone 下来就能跑"的复现性，也会往 Git 历史里塞进大量工具链产物。
# 而 Chrome / Edge 自带的 `--app=` 模式已经能给出无地址栏、无标签页的独立窗口，
# 系统里本来就有，零新增依赖。
# --------------------------------------------------------------------------- #
def app_profile_dir() -> Path:
    """独立窗口专用的 user-data-dir。

    `--app` 必须配一个独立的 profile：否则浏览器会把这个请求转交给已经在跑的
    实例，表现为"命令执行了、窗口没弹出来"，而进程已经悄悄退出 —— 很难查。
    """
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    d = base / "DailyPlanner" / "AppProfile"
    d.mkdir(parents=True, exist_ok=True)
    return d


def find_app_browser() -> tuple[str, str] | None:
    """按 Chrome → Edge 顺序找出支持 `--app` 的浏览器，返回 (名字, 路径)。"""
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")

    candidates: list[tuple[str, list[str]]]
    if os.name == "nt":
        candidates = [
            ("Chrome", [
                os.path.join(pf, r"Google\Chrome\Application\chrome.exe"),
                os.path.join(pf86, r"Google\Chrome\Application\chrome.exe"),
                os.path.join(local, r"Google\Chrome\Application\chrome.exe"),
            ]),
            ("Edge", [
                os.path.join(pf86, r"Microsoft\Edge\Application\msedge.exe"),
                os.path.join(pf, r"Microsoft\Edge\Application\msedge.exe"),
            ]),
        ]
    elif sys.platform == "darwin":
        candidates = [
            ("Chrome", ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]),
            ("Edge", ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]),
        ]
    else:
        candidates = [
            ("Chrome", ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
                        "/usr/bin/chromium", "/usr/bin/chromium-browser"]),
            ("Edge", ["/usr/bin/microsoft-edge", "/usr/bin/microsoft-edge-stable"]),
        ]
        # Linux 上装在哪都有可能，PATH 里再兜一次
        for name, exe in (("Chrome", "google-chrome"), ("Chrome", "chromium"),
                          ("Edge", "microsoft-edge")):
            found = shutil.which(exe)
            if found:
                candidates.append((name, [found]))

    for name, paths in candidates:
        for p in paths:
            if p and os.path.isfile(p):
                return name, p
    return None


def open_app_window(url: str) -> str:
    """用浏览器自身的 `--app` 模式打开独立窗口，返回一句说明（成功/回退都要说清）。"""
    found = find_app_browser()
    if not found:
        webbrowser.open(url)
        return "未找到 Chrome / Edge —— 已用默认浏览器打开（带地址栏的普通标签页）"

    name, exe = found
    profile = app_profile_dir()
    cmd = [
        exe,
        f"--app={url}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        f"--window-size={APP_WINDOW_SIZE}",
    ]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        webbrowser.open(url)
        return f"{name} 启动失败（{e}）—— 已回退默认浏览器"
    return f"{name} 独立应用窗口已打开（profile: {profile}）"


def main() -> int:
    ap = argparse.ArgumentParser(description="启动 SecondWill 开发环境")
    ap.add_argument("--backend", action="store_true", help="只启动后端")
    ap.add_argument("--no-kill", action="store_true", help="不清理端口上的旧进程")
    ap.add_argument("--open", action="store_true", help="就绪后自动打开浏览器（双击启动时用）")
    ap.add_argument(
        "--app", action="store_true",
        help="用 Chrome / Edge 的 --app 模式打开独立窗口（无地址栏、无标签页）",
    )
    args = ap.parse_args()

    if not (BACKEND / "app" / "main.py").exists():
        print(f"找不到后端入口：{BACKEND / 'app' / 'main.py'}（脚本位置不对？）", file=sys.stderr)
        return 1

    env = build_env()

    if not args.no_kill:
        print("清理端口…")
        kill_port(BACKEND_PORT)
        if not args.backend:
            kill_port(FRONTEND_PORT)
        time.sleep(1)

    backend = spawn_backend(env)
    if not probe(BACKEND_HEALTH):
        print("后端启动超时：健康检查未通过。上面 [后端] 前缀的日志里有原因。", file=sys.stderr)
        backend.terminate()
        return 1
    print(f"  后端就绪 ✓  {BACKEND_HEALTH}")

    frontend = None
    if not args.backend:
        npm = shutil.which("npm", path=env.get("PATH")) or shutil.which("npm.cmd", path=env.get("PATH"))
        if not npm:
            print("找不到 npm：请确认 node 已安装，或把它的目录加进 PATH。", file=sys.stderr)
            print("后端已在 8000 运行，前端请手动启动。")
        elif listening_pids(FRONTEND_PORT):
            # 端口没清干净时**不要**让 vite 悄悄换端口：那样页面跑在 5174，
            # 而 README 和书签里都是 5173，人会对着黑屏找半天原因。
            print(
                f"端口 {FRONTEND_PORT} 仍被占用（PID {listening_pids(FRONTEND_PORT)}），跳过前端启动。\n"
                f"后端已在 {BACKEND_PORT} 运行；确认占用进程后重跑本脚本。",
                file=sys.stderr,
            )
        else:
            frontend = spawn_frontend(env, npm)
            if not probe(f"http://127.0.0.1:{FRONTEND_PORT}/", timeout=60):
                print("前端启动超时（后端仍在运行），请看上面 [前端] 日志。", file=sys.stderr)
            else:
                print(f"  前端就绪 ✓  http://127.0.0.1:{FRONTEND_PORT}")

    if frontend is not None:
        if args.app:
            print("  " + open_app_window(APP_URL))
        elif args.open:
            webbrowser.open(APP_URL)

    print("\n服务已就绪，Ctrl+C 停止。\n")
    try:
        while True:
            if backend.poll() is not None:
                print("后端进程已退出，收尾中…（上面 [后端] 日志里有原因）", file=sys.stderr)
                break
            if frontend is not None and frontend.poll() is not None:
                # 单侧退出**不连带**停掉另一个：vite 崩了不该让接口一起死，
                # 否则调试前端时每次都得重启后端。
                print("前端进程已退出，后端仍在运行。Ctrl+C 可全部停止。", file=sys.stderr)
                frontend = None
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在停止…")
    finally:
        for p in (frontend, backend):
            if p is not None and p.poll() is None:
                p.terminate()
        time.sleep(1)
        for p in (frontend, backend):
            if p is not None and p.poll() is None:
                p.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
