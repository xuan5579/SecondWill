"""DEMO_MODE 录制回放测试。

指纹必须排除 request_id / triggered_at 这类必然变化的字段，
否则"同样的输入"永远命中不了缓存，DEMO 回放和省钱都会失效。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from app.llm.cache import DemoRecorder, fingerprint


def test_fingerprint_ignores_volatile_fields():
    a = {"request_id": "r1", "triggered_at": "2026-10-28T10:00:00Z", "records": [{"id": "x"}]}
    b = {"request_id": "r2", "triggered_at": "2026-10-29T22:00:00Z", "records": [{"id": "x"}]}
    assert fingerprint("decide", a) == fingerprint("decide", b)


def test_fingerprint_varies_with_content():
    a = {"records": [{"id": "x"}]}
    b = {"records": [{"id": "y"}]}
    assert fingerprint("decide", a) != fingerprint("decide", b)


def test_fingerprint_varies_with_kind():
    payload = {"records": [{"id": "x"}]}
    assert fingerprint("decide", payload) != fingerprint("review", payload)


def test_record_and_replay_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        recorder = DemoRecorder(base_dir=base, replay=True, record=True)
        payload = {"records": [{"id": "x"}], "request_id": "ignored"}
        response = {"decision_type": "reschedule", "summary": "挪到下午"}

        # 未录制时读不到
        assert recorder.load("decide", payload) is None

        recorder.save("decide", payload, response)
        assert (base / f"{fingerprint('decide', payload)}.json").exists()

        assert recorder.load("decide", payload) == response


def test_replay_disabled_returns_none_even_if_fixture_exists():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        payload = {"records": [{"id": "x"}]}
        writer = DemoRecorder(base_dir=base, replay=False, record=True)
        writer.save("decide", payload, {"decision_type": "no_action"})

        reader = DemoRecorder(base_dir=base, replay=False, record=False)
        assert reader.load("decide", payload) is None
