"""DEMO_MODE 录制回放 + 决策结果缓存。

两个用途，同一套存储：
1. **录制回放**：`DEMO_MODE=true` 时，编排器不再真实调用模型，
   而是按请求指纹读取预录制的响应。答辩现场断网/限流时的保底手段。
2. **结果缓存**：同样输入不再重复调用模型，省钱也让重复演示结果稳定。

两者都靠"请求指纹"定位：把请求体规范化后取哈希，
因此与字段顺序无关，但会排除 request_id 与时间戳这类必然变化的字段。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# 参与指纹计算的字段会被排除：这些字段每次请求都不同，但语义相同
_VOLATILE_KEYS = {"request_id", "triggered_at"}


def fingerprint(kind: str, payload: dict) -> str:
    """计算请求指纹。

    `kind` 参与哈希，避免不同类别的请求撞到同一个 key。
    """
    def _clean(obj):
        if isinstance(obj, dict):
            return {
                k: _clean(v)
                for k, v in obj.items()
                if k not in _VOLATILE_KEYS
            }
        if isinstance(obj, (list, tuple)):
            return [_clean(v) for v in obj]
        return obj

    canonical = json.dumps(_clean(payload), sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"{kind}_{digest}"


class DemoRecorder:
    """录制/回放器。

    replay=True  ：命中 fixture 则返回，未命中返回 None（由调用方决定降级）
    record=True  ：把真实响应写入 fixture
    """

    def __init__(self, base_dir: Path | None = None, replay: bool = False, record: bool = False):
        self.base_dir = Path(base_dir or settings.DEMO_FIXTURES_DIR)
        self.replay = replay
        self.record = record

    def _path(self, key: str) -> Path:
        return self.base_dir / f"{key}.json"

    def load(self, kind: str, payload: dict) -> dict | None:
        """按指纹读取已录制的响应。仅在 replay 模式下生效。"""
        if not self.replay:
            return None
        path = self._path(fingerprint(kind, payload))
        if not path.exists():
            logger.debug("demo fixture 未命中: %s", path.name)
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            logger.info("demo fixture 命中: %s", path.name)
            return data
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("demo fixture 读取失败 %s: %s", path.name, exc)
            return None

    def save(self, kind: str, payload: dict, response: dict) -> None:
        """写入录制。仅在 record 模式下生效。"""
        if not self.record:
            return
        self.base_dir.mkdir(parents=True, exist_ok=True)
        path = self._path(fingerprint(kind, payload))
        path.write_text(
            json.dumps(response, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        logger.info("demo fixture 已录制: %s", path.name)


def get_recorder() -> DemoRecorder:
    """按配置构造录制器。P4 的编排器会用到它。"""
    return DemoRecorder(
        replay=settings.DEMO_MODE,
        record=settings.DEMO_RECORD,
    )
