#!/usr/bin/env python3
"""慢速滚动后端决策日志 —— 给镜头看。

后端日志只有 stdout 一路（`core/logging.py` 的 StreamHandler），
所以先把后端单独起起来并落文件，再用本脚本按行慢放::

    python scripts/dev.py --backend > backend.log 2>&1     # 终端 A
    python scripts/watch_log.py backend.log                # 终端 B（这个上镜头）

默认只放**决策链路**的行（触发 / 闸门 / 校验 / 降级 / 干预 / 反馈），
SSE 心跳、SQLAlchemy 之类全部滤掉 —— 镜头里滚动的应该句句有效。
字号靠终端自己放大（Windows Terminal：`Ctrl +` 滚轮；cmd：属性里改），
脚本只负责「一次一行 + 慢放 + 高亮」。

用法::

    python scripts/watch_log.py backend.log                # 每行间隔 0.8s
    python scripts/watch_log.py backend.log --slow 2.0     # 更慢，方便念稿
    python scripts/watch_log.py backend.log --all          # 不过滤，全量
    python scripts/watch_log.py backend.log --tail 20      # 先把最后 20 行放一遍
    python scripts/watch_log.py backend.log --no-color     # 纯文本（非 Windows Terminal 时用）
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

#: 决策链路的关键行。命中任一关键词才放。
KEYWORDS = (
    "决策",
    "闸门",
    "干预",
    "反馈",
    "LLM",
    "校验",
    "降级",
    "降权",
    "节奏",
    "fixture",
)

BOLD = "\033[1m"
CYAN = "\033[96m"
RESET = "\033[0m"


def enable_vt() -> None:
    """Windows 上开启 ANSI 转义支持。

    不开启的话 `\\033[1m` 会原样印成一串乱码，镜头里很难看。
    `os.system("")` 是 Windows 10+ 上启用 VT 的最小手段。
    """
    if os.name == "nt":
        os.system("")  # noqa: S605,S607 —— 空命令，只为让系统开启 VT 模式


def is_target(line: str, show_all: bool) -> bool:
    return show_all or any(k in line for k in KEYWORDS)


def emit(line: str, color: bool) -> None:
    text = line.rstrip()
    if color:
        print(f"\n{BOLD}{CYAN}{text}{RESET}", flush=True)
    else:
        print(f"\n{text}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="慢速滚动后端决策日志")
    parser.add_argument("file", nargs="?", default="backend.log", help="日志文件（默认 backend.log）")
    parser.add_argument("--slow", type=float, default=0.8, help="每行间隔秒数（默认 0.8）")
    parser.add_argument("--all", action="store_true", help="不过滤，全量输出")
    parser.add_argument("--tail", type=int, default=0, help="先把最后 N 行放一遍")
    parser.add_argument(
        "--once",
        action="store_true",
        help="只放 --tail 指定的那几行然后退出（开拍前预演一遍用，不进入跟踪）",
    )
    parser.add_argument("--no-color", action="store_true", help="关闭高亮（终端不支持 ANSI 时用）")
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"找不到日志文件：{path}")
        print("先用这条命令把后端输出落文件：python scripts/dev.py --backend > backend.log 2>&1")
        return 1

    color = not args.no_color
    if color:
        enable_vt()

    with path.open("r", encoding="utf-8", errors="replace") as fh:
        if args.tail:
            for line in fh.readlines()[-args.tail:]:
                if is_target(line, args.all):
                    emit(line, color)
                    time.sleep(args.slow)
        if args.once:
            return 0
        fh.seek(0, os.SEEK_END)
        print(f"\n--- 开始跟踪 {path.name}（Ctrl+C 停止）---", flush=True)
        try:
            while True:
                line = fh.readline()
                if line:
                    if is_target(line, args.all):
                        emit(line, color)
                        time.sleep(args.slow)
                else:
                    time.sleep(0.2)
        except KeyboardInterrupt:
            print("\n停止跟踪。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
