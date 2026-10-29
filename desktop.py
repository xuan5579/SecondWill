#!/usr/bin/env python3
"""可选：用 pywebview 开一个原生窗口，承载本地跑起来的 Web 应用。

这是一个**独立的呈现层**，不进主流程，也不改变任何现有行为：

- **不改动 Web 启动路径**：`scripts/dev.py` 该怎么起服务还是怎么起，
  这里只是 `import dev` 复用它的 `spawn_backend` / `spawn_frontend`；
- **不引入 Tauri / Electron**：那会把一个纯 Web 项目变成需要编译的桌面项目，
  破坏"clone 下来就能跑"的复现性，还会往 Git 历史里塞进工具链产物；
- **唯一新增依赖是 pywebview**，且只在 `import webview` 这一处用到。
  装不上、不想装，都完全不影响正常使用 —— 直接删掉本文件即可。

浏览器仍然是运行载体，原生窗口只是"换了个壳"：页面、接口、SSE、宠物状态机
全部照旧，没有为这个窗口写过任何一行前端分支代码。

用法::

    pip install pywebview        # 仅这个脚本需要
    python desktop.py
    python desktop.py --debug    # 打开调试菜单（右键可检查元素）
"""

from __future__ import annotations

import argparse
import atexit
import shutil
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))

import dev  # noqa: E402  （必须先补 sys.path 才能 import）


def main() -> int:
    ap = argparse.ArgumentParser(description="原生窗口模式（可选，需 pywebview）")
    ap.add_argument("--debug", action="store_true", help="启用调试菜单")
    ap.add_argument("--no-kill", action="store_true", help="不清理端口上的旧进程")
    args = ap.parse_args()

    # 延迟到确认要用时才导入：没装 pywebview 也要给出一个能照做的提示，
    # 而不是抛一段 traceback 让人去猜。
    try:
        import webview
    except ImportError:
        print(
            "缺少 pywebview。这个脚本是可选的，装不上也不影响 Web 用法。\n"
            f"要用原生窗口的话：  {sys.executable} -m pip install pywebview\n"
            "或者直接用浏览器 / start.bat（Chrome、Edge 的 --app 独立窗口），不需要任何额外依赖。",
            file=sys.stderr,
        )
        return 1

    env = dev.build_env()

    if not args.no_kill:
        print("清理端口…")
        dev.kill_port(dev.BACKEND_PORT)
        dev.kill_port(dev.FRONTEND_PORT)
        time.sleep(1)

    backend = dev.spawn_backend(env)
    if not dev.probe(dev.BACKEND_HEALTH):
        print("后端启动超时，放弃打开窗口。", file=sys.stderr)
        backend.terminate()
        return 1
    print(f"  后端就绪 ✓  {dev.BACKEND_HEALTH}")

    npm = shutil.which("npm", path=env.get("PATH")) or shutil.which("npm.cmd", path=env.get("PATH"))
    if not npm:
        print("找不到 npm，无法启动前端；窗口会白屏，就此停止。", file=sys.stderr)
        backend.terminate()
        return 1

    frontend = dev.spawn_frontend(env, npm)
    if not dev.probe(dev.APP_URL, timeout=60):
        print("前端启动超时，放弃打开窗口。", file=sys.stderr)
        for p in (frontend, backend):
            p.terminate()
        return 1
    print(f"  前端就绪 ✓  {dev.APP_URL}")

    procs = [backend, frontend]
    stopped = False

    def shutdown(*_args) -> None:
        """关窗口 / Ctrl+C / 进程被杀，都要把两个服务收干净。

        只 `terminate()` 是不够的：npm 会拉起真正监听端口的 node 子进程，
        父进程死了子进程可能还留着，表现为"窗口关了，端口还占着"。
        所以先 terminate，再按端口补一次清理。
        """
        nonlocal stopped
        if stopped:
            return
        stopped = True
        for p in procs:
            if p.poll() is None:
                p.terminate()
        time.sleep(1)
        for p in procs:
            if p.poll() is None:
                p.kill()
        # 兜底：清掉可能残留的 vite node 子进程
        dev.kill_port(dev.FRONTEND_PORT)
        dev.kill_port(dev.BACKEND_PORT)
        print("已停止后端与前端进程。")

    atexit.register(shutdown)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: (shutdown(), sys.exit(0)))
        except (ValueError, OSError):
            # 非主线程里注册不了 signal，atexit 仍然兜得住
            pass

    window = webview.create_window(
        title="每日规划助手 · AI 执行教练",
        url=dev.APP_URL,
        width=1280,
        height=860,
        min_size=(900, 640),
        background_color="#12151c",
    )
    window.events.closed += shutdown

    print("\n窗口已打开，关闭窗口即停止服务。\n")
    webview.start(debug=args.debug)
    shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
