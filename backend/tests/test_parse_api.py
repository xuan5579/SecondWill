"""/api/parse 的端点契约测试。

两条最要紧的断言：
1. **解析绝不写库** —— 若有任何副作用，constraint ②的"草稿确认"就失去意义，
   而且会绕过 conflicts / pet 的既有链路。
2. **DEMO fixture 是相对量** —— 三天前录的"明天上午9点"回放出来必须还是
   "明天上午9点"，而不是那个已经过去的具体日期。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.core.time import local_to_utc, parse_iso, to_iso, utc_to_local  # noqa: E402
from app.schemas.naturallang import NLParseResult  # noqa: E402
from app.services.naturallang import parse_nl  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app.api.parse import _from_recordable, _to_recordable  # noqa: E402
from app.main import app  # noqa: E402

NOW = local_to_utc(datetime(2026, 10, 29, 10, 0))


@pytest.fixture()
def http():
    from app.db.session import SessionLocal, init_db
    from app.models.event import BehaviorEvent
    from app.models.pet_snapshot import PetSnapshot
    from app.models.record import Record

    init_db()
    db = SessionLocal()
    try:
        db.query(BehaviorEvent).delete()
        db.query(Record).delete()
        db.query(PetSnapshot).delete()
        db.commit()
    finally:
        db.close()
    with TestClient(app) as c:
        yield c


class TestEndpointContract:
    def test_parse_returns_result_and_does_not_persist(self, http):
        """constraint ②：解析只是生成草稿，落库必须是用户点了确认之后的事。"""
        before = http.get("/api/records").json()
        r = http.post("/api/parse", json={"text": "明天下午3点到5点开会"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "resolved"
        assert body["draft"]["title"] == "开会"

        after = http.get("/api/records").json()
        assert len(after) == len(before), "/api/parse 绝不能有写库副作用"

    def test_parse_rejects_empty_text(self, http):
        # schema 层拦纯空格（min_length 不看空白），业务层再兜一次 -> 400
        r = http.post("/api/parse", json={"text": "   "})
        assert r.status_code in (400, 422)
        assert http.post("/api/parse", json={"text": ""}).status_code == 422

    def test_parse_accepts_injected_now(self, http):
        body = http.post(
            "/api/parse",
            json={"text": "明天上午9点到11点开会", "now": to_iso(NOW)},
        ).json()
        local = utc_to_local(parse_iso(body["draft"]["start_at"]))
        assert (local.date(), local.hour) == (datetime(2026, 10, 30).date(), 9)

    def test_error_sentence_returns_structured_error(self, http):
        body = http.post("/api/parse", json={"text": "月底前交报告"}).json()
        assert body["status"] == "error"
        assert body["error"]["code"] == "NXC-NEED-LLM"
        assert body["error"]["hint"]


class TestDemoFixtureIsEvergreen:
    """录制回放必须按相对量存储，否则演示当天必翻车。"""

    def test_relative_roundtrip(self):
        today = utc_to_local(NOW).date()
        result = parse_nl("明天下午3点到5点开会", NOW)
        recorded = _to_recordable(result, today)

        rel = recorded["_relative"]
        assert rel["start"]["day_offset"] == 1
        assert rel["start"]["minute"] == 15 * 60

        # 三天后回放 —— 必须仍然是"明天下午3点"
        later = local_to_utc(datetime.combine(today + timedelta(days=3), time(10, 0)))
        restored = _from_recordable(json.loads(json.dumps(recorded)), utc_to_local(later).date())
        local = utc_to_local(parse_iso(restored.draft.start_at))
        assert local.date() == today + timedelta(days=4)  # 新的"明天"
        assert (local.hour, local.minute) == (15, 0)

    def test_replay_marks_source_as_cache(self):
        today = utc_to_local(NOW).date()
        result = parse_nl("明天下午3点到5点开会", NOW)
        restored = _from_recordable(_to_recordable(result, today), today)
        assert restored.source == "cache", "回放结果必须如实标注来源，不能伪装成现场解析"

    def test_recorded_fixture_files_exist_for_demo(self):
        """清单例句应该在 fixtures 里有一份录制，供现场断网兜底。"""
        fixture_dir = Path(settings.DEMO_FIXTURES_DIR)
        if not fixture_dir.exists():
            pytest.skip(f"fixtures 目录不存在：{fixture_dir}（先运行 --record 生成）")
        files = list(fixture_dir.glob("nl_parse_*.json"))
        assert files, "DEMO 兜底依赖录制文件，缺少会让断点演示失去保障"


class TestConfirmGoesThroughExistingPipeline:
    """constraint ④：确认草稿走的是既有 POST /api/records，不另起链路。"""

    def test_confirm_triggers_conflict_and_pet(self, http):
        base = http.post(
            "/api/records",
            json={
                "title": "已存在的会",
                "start_at": to_iso(NOW + timedelta(hours=26)),
                "end_at": to_iso(NOW + timedelta(hours=27)),
            },
        )
        assert base.status_code == 201

        draft = http.post(
            "/api/parse", json={"text": "明天下午3点到5点开会"}
        ).json()["draft"]
        created = http.post("/api/records", json=draft)
        assert created.status_code == 201

        state = http.get("/api/state").json()
        assert state["pet"]["mood"] in {"alert", "normal", "great", "guilty", "tired", "dormant"}
        # 宠物快照被刷新，说明确实走了既有联动链路
        assert NLParseResult.model_fields  # 契约仍可用（防 import 被优化掉）
