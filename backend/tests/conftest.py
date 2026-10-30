"""测试配置。

关键：必须在 import app 之前把 DATABASE_PATH 指向临时库，
否则测试会读写开发库 data/app.db。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

_TMP_DB_DIR = Path(tempfile.gettempdir()) / "daily_planner_test"
_TMP_DB_DIR.mkdir(parents=True, exist_ok=True)

os.environ["DATABASE_PATH"] = str(_TMP_DB_DIR / "test.db")
os.environ.setdefault("DEMO_MODE", "false")
os.environ.setdefault("DEMO_RECORD", "false")

# 测试必须**离线**。开发机一旦在 .env 里配了真 Key，没这一行的话
# 测试就会真的去打 api.deepseek.com：既烧 token，又让"没有 Key 时怎么降级"
# 这类断言永远跑不到（它们会静默变成"模型恰好也返回了同样结果"）。
# 需要模拟模型响应的用例自己注入 MockTransport，不依赖真 Key。
os.environ["DEEPSEEK_API_KEY"] = ""

import pytest  # noqa: E402


@pytest.fixture()
def client():
    """每个用例使用一个干净的数据库。"""
    from fastapi.testclient import TestClient

    from app.db.session import SessionLocal, init_db
    from app.main import app
    from app.models.event import BehaviorEvent
    from app.models.intervention import Intervention
    from app.models.pet_snapshot import PetSnapshot
    from app.models.record import Record

    init_db()
    db = SessionLocal()
    try:
        # P5 补上 Intervention：干预历史会喂给节奏闸门，
        # 上一个用例点过的 mute 若残留到下一个用例，会出现"什么都没做却被降权"
        # 这种极难定位的串扰 —— 闸门测试最先炸，但根因在这里。
        db.query(BehaviorEvent).delete()
        db.query(Record).delete()
        db.query(PetSnapshot).delete()
        db.query(Intervention).delete()
        db.commit()
    finally:
        db.close()

    # 内存缓存同样是跨用例状态（2026-10-30 补）：决策指纹是"相对化"的，
    # 两个"形状相同、时间整体平移"的请求会共享一个缓存条目 —— 于是
    # "空库 + manual"这类再普通不过的请求，上一个用例算过、这个用例
    # 就直接命中 cache，source 从 fallback 变成 cache，断言炸得莫名其妙。
    # 与上面清库是同一课：**测试隔离清单里每加一种跨请求状态，就要在这里清一次。**
    from app.llm.orchestrator import clear_cache

    clear_cache()

    with TestClient(app) as c:
        yield c
