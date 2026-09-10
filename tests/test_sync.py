# -*- coding: utf-8 -*-
"""同步引擎回归：水位纪律、断点续跑、幂等重投、停止信号、重试分类。"""

import threading
from typing import Any, Dict, List, Optional

import pytest

from app.plugins.btdeckbridge.btdeck_client import BtDeckApiError
from app.plugins.btdeckbridge.sync import SyncEngine


class MemoryCheckpoint:
    def __init__(self):
        self.payload: Optional[Dict[str, Any]] = None

    def load(self):
        return dict(self.payload) if self.payload else None

    def save(self, payload):
        self.payload = dict(payload)


class FakeAdapter:
    """脚本化宿主历史源：pages = {页码: [原始条目]}，total 固定传入。"""

    def __init__(self, pages: Dict[int, List[Dict[str, Any]]], total: int, page_size: int):
        self.pages = pages
        self.total = total
        self.page_size = page_size
        self.calls = 0

    def fetch_page(self, page: int, count: int):
        self.calls += 1
        return self.total, list(self.pages.get(page, []))

    def close(self):
        pass


class FakeClient:
    """脚本化 BtDeck 端点：按批次序号返回结果或抛错。"""

    def __init__(self, results: Optional[List[Any]] = None):
        self.results = list(results or [])
        self.batches: List[Dict[str, Any]] = []
        self.handshakes = 0

    def handshake(self, *args, **kwargs):
        self.handshakes += 1
        return {"status": "ok", "created": self.handshakes == 1}

    def sync_transfer_history(self, instance_id, items, sync_mode, page_number, page_size, is_last_batch):
        self.batches.append(
            {
                "items": list(items),
                "mode": sync_mode,
                "page": page_number,
                "last": is_last_batch,
            }
        )
        if self.results:
            outcome = self.results.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            if isinstance(outcome, dict):
                return outcome
        return {
            "inserted": len(items),
            "updated": 0,
            "skipped": 0,
            "failed": 0,
            "errors": [],
        }


def _raw(history_id: int, title: str = "M") -> Dict[str, Any]:
    return {"id": history_id, "title": title, "src": f"/d/{history_id}.mkv", "dest": f"/m/{history_id}.mkv"}


def _engine(adapter, client, checkpoint, stop_event=None, batch_size=100):
    return SyncEngine(
        adapter=adapter,
        client=client,
        checkpoint_store=checkpoint,
        logger=lambda message: None,
        stop_event=stop_event if stop_event is not None else threading.Event(),
        batch_size=batch_size,
        sleep_fn=lambda seconds: None,
    )


def _run(engine, mode="incremental"):
    return engine.run("inst-1", "测试", "1.0.0", "2.9.9", mode=mode, full_interval_hours=24)


class TestFullPass:
    def test_full_pushes_all_and_advances_watermark(self):
        adapter = FakeAdapter({1: [_raw(3), _raw(2)], 2: [_raw(1)]}, total=3, page_size=2)
        client = FakeClient()
        checkpoint = MemoryCheckpoint()
        summary = _run(_engine(adapter, client, checkpoint, batch_size=2), mode="full")

        assert summary.finished is True
        assert summary.inserted == 3
        pushed = [item["historyId"] for batch in client.batches for item in batch["items"]]
        assert sorted(pushed) == [1, 2, 3]
        assert checkpoint.payload["watermark"] == 3
        assert "pass" not in checkpoint.payload
        assert checkpoint.payload["last_full_at"] > 0

    def test_batch_chunking_respects_batch_size(self):
        adapter = FakeAdapter({1: [_raw(i) for i in range(10, 0, -1)]}, total=10, page_size=100)
        client = FakeClient()
        engine = _engine(adapter, client, MemoryCheckpoint(), batch_size=4)
        summary = _run(engine, mode="full")
        assert summary.finished is True
        assert len(client.batches) == 3
        assert [len(b["items"]) for b in client.batches] == [4, 4, 2]

    def test_rerun_full_is_idempotent_server_side(self):
        adapter = FakeAdapter({1: [_raw(1)]}, total=1, page_size=10)
        client = FakeClient(results=[{"inserted": 0, "updated": 0, "skipped": 1, "failed": 0, "errors": []}])
        checkpoint = MemoryCheckpoint()
        summary = _run(_engine(adapter, client, checkpoint), mode="full")
        assert summary.skipped == 1
        assert summary.inserted == 0
        assert checkpoint.payload["watermark"] == 1


class TestIncremental:
    def test_only_newer_than_watermark_pushed(self):
        checkpoint = MemoryCheckpoint()
        checkpoint.payload = {"watermark": 5}
        adapter = FakeAdapter({1: [_raw(7), _raw(6), _raw(5), _raw(4)]}, total=4, page_size=4)
        client = FakeClient()
        summary = _run(_engine(adapter, client, checkpoint))

        pushed = [item["historyId"] for batch in client.batches for item in batch["items"]]
        assert pushed == [7, 6]
        assert summary.finished is True
        assert checkpoint.payload["watermark"] == 7

    def test_early_stop_when_page_below_watermark_no_push(self):
        checkpoint = MemoryCheckpoint()
        checkpoint.payload = {"watermark": 100}
        adapter = FakeAdapter({1: [_raw(3), _raw(2)]}, total=2, page_size=10)
        client = FakeClient()
        summary = _run(_engine(adapter, client, checkpoint))
        assert client.batches == []
        assert summary.finished is True
        assert checkpoint.payload["watermark"] == 100

    def test_auto_mode_first_run_chooses_full(self):
        adapter = FakeAdapter({1: [_raw(1)]}, total=1, page_size=10)
        client = FakeClient()
        summary = _run(_engine(adapter, client, MemoryCheckpoint()), mode="auto")
        assert summary.mode == "full"
        # 全量完成后置 last_full_at，下一轮 auto 走增量
        adapter2 = FakeAdapter({1: [_raw(1)]}, total=1, page_size=10)
        client2 = FakeClient()
        checkpoint2 = MemoryCheckpoint()
        summary2 = _engine(adapter2, client2, checkpoint2).run("inst-1", "t", "1.0.0", "2.9.9", mode="auto")
        # 独立 checkpoint 无 last_full_at → 仍是 full；带 last_full_at 的走增量
        assert summary2.mode == "full"
        adapter3 = FakeAdapter({1: [_raw(1)]}, total=1, page_size=10)
        client3 = FakeClient()
        checkpoint3 = MemoryCheckpoint()
        checkpoint3.payload = {"watermark": 1, "last_full_at": 9e9}
        summary3 = _engine(adapter3, client3, checkpoint3).run("inst-1", "t", "1.0.0", "2.9.9", mode="auto")
        assert summary3.mode == "incremental"


class TestResumeAndWatermark:
    def test_transient_failure_keeps_watermark_and_resumes_from_checkpoint_page(self):
        checkpoint = MemoryCheckpoint()
        # 第 1 页推送成功；第 2 页网络错误（可重试类，退避后仍失败）
        adapter = FakeAdapter({1: [_raw(2), _raw(1)], 2: [_raw(3), _raw(4)]}, total=4, page_size=2)
        adapter.page_order = [1, 2]
        client = FakeClient(
            results=[
                {"inserted": 2, "updated": 0, "skipped": 0, "failed": 0, "errors": []},
                BtDeckApiError("网络中断", http_status=0),
                BtDeckApiError("网络中断", http_status=0),
                BtDeckApiError("网络中断", http_status=0),
            ]
        )
        summary = _run(_engine(adapter, client, checkpoint, batch_size=2), mode="full")
        assert summary.finished is False
        assert "推送连续失败" in summary.stopped_reason
        # 水位未推进；断点停在失败页
        assert "watermark" not in checkpoint.payload or checkpoint.payload["watermark"] == 0
        assert checkpoint.payload["pass"]["next_page"] == 2

        # 恢复后从断点页续跑（第 1 页不再读取）
        calls_before = adapter.calls
        client2 = FakeClient()
        summary2 = _run(_engine(adapter, client2, checkpoint, batch_size=2), mode="full")
        assert summary2.finished is True
        assert summary2.inserted == 2
        pages_fetched = adapter.calls - calls_before
        assert pages_fetched == 1

    def test_non_retryable_rejection_stops_without_retry(self):
        adapter = FakeAdapter({1: [_raw(1)]}, total=1, page_size=10)
        client = FakeClient(results=[BtDeckApiError("不支持的协议版本 2", http_status=400, code="400")])
        checkpoint = MemoryCheckpoint()
        summary = _run(_engine(adapter, client, checkpoint), mode="full")
        assert summary.finished is False
        assert "HTTP 400" in summary.stopped_reason
        assert len(client.batches) == 1  # 无重试

    def test_handshake_failure_short_circuits(self):
        adapter = FakeAdapter({1: [_raw(1)]}, total=1, page_size=10)
        client = FakeClient()

        def boom(*args, **kwargs):
            raise BtDeckApiError("握手被拒绝（403）：集成开关未开启", http_status=403)

        client.handshake = boom
        summary = _run(_engine(adapter, client, MemoryCheckpoint()))
        assert summary.finished is False
        assert "握手失败" in summary.stopped_reason
        assert client.batches == []

    def test_stop_event_mid_pass_keeps_resume_point(self):
        stop_event = threading.Event()
        stop_event.set()  # 一开始即停止（页循环首检）
        adapter = FakeAdapter({1: [_raw(1)]}, total=1, page_size=10)
        client = FakeClient()
        checkpoint = MemoryCheckpoint()
        summary = _run(_engine(adapter, client, checkpoint, stop_event=stop_event))
        assert summary.finished is False
        assert "插件停用" in summary.stopped_reason
        assert client.batches == []
