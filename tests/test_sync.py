# -*- coding: utf-8 -*-
"""同步引擎回归：水位纪律、断点续跑、幂等重投、停止信号、重试分类。"""

import threading
from typing import Any, Dict, List, Optional

import pytest

from app.plugins.btdeckbridge.btdeck_client import BtDeckApiError
from app.plugins.btdeckbridge.moviepilot_adapter import MoviePilotAdapterError
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


def _raw(history_id: int, title: str = "M", date: str = "2026-09-01 10:00:00") -> Dict[str, Any]:
    # 宿主 TransferHistory.to_dict() 按列名输出：主键为 id、时间为字符串 date
    return {
        "id": history_id,
        "title": title,
        "date": date,
        "src": f"/d/{history_id}.mkv",
        "dest": f"/m/{history_id}.mkv",
    }


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

    def test_early_stop_reads_host_id_field_without_second_fetch(self):
        """回归：宿主原始记录主键字段是 id（非 historyId）；无新增时
        整页低于水位即收口，不再读取第 2 页。"""
        checkpoint = MemoryCheckpoint()
        checkpoint.payload = {"watermark": 100}
        adapter = FakeAdapter(
            {1: [_raw(3, date="2026-09-01 10:00:00"), _raw(2, date="2026-09-01 09:00:00")]},
            total=50,
            page_size=10,
        )
        client = FakeClient()
        summary = _run(_engine(adapter, client, checkpoint))
        assert adapter.calls == 1  # 提前收口，未读第 2 页
        assert client.batches == []
        assert summary.finished is True
        assert checkpoint.payload["watermark"] == 100

    def test_mixed_new_and_old_pushes_new_then_stops(self):
        """新旧混合：第 1 页含新增（推送）+ 旧记录，第 2 页全旧即收口。"""
        checkpoint = MemoryCheckpoint()
        checkpoint.payload = {"watermark": 5}
        adapter = FakeAdapter(
            {
                1: [
                    _raw(7, date="2026-09-02 10:00:00"),
                    _raw(6, date="2026-09-02 09:59:00"),
                    _raw(4, date="2026-09-02 09:58:00"),
                    _raw(3, date="2026-09-02 09:57:00"),
                ],
                2: [_raw(2, date="2026-09-01 08:00:00"), _raw(1, date="2026-09-01 07:00:00")],
            },
            total=50,
            page_size=4,
        )
        client = FakeClient()
        engine = _engine(adapter, client, checkpoint, batch_size=4)
        summary = _run(engine)

        pushed = [item["historyId"] for batch in client.batches for item in batch["items"]]
        assert pushed == [7, 6]
        assert adapter.calls == 2  # 第 2 页全旧且跨两个时间戳 → 收口
        assert summary.finished is True
        assert checkpoint.payload["watermark"] == 7

    def test_new_records_spanning_pages_are_all_synced(self):
        """跨页新增：新增记录分布在多页时全部同步，直到安全收口页。"""
        checkpoint = MemoryCheckpoint()
        checkpoint.payload = {"watermark": 5}
        adapter = FakeAdapter(
            {
                1: [_raw(8, date="2026-09-02 10:00:00"), _raw(7, date="2026-09-02 09:59:00")],
                2: [_raw(6, date="2026-09-02 09:58:00"), _raw(5, date="2026-09-02 09:57:00")],
                3: [_raw(4, date="2026-09-01 08:00:00"), _raw(3, date="2026-09-01 07:00:00")],
            },
            total=50,
            page_size=2,
        )
        client = FakeClient()
        summary = _run(_engine(adapter, client, checkpoint, batch_size=2))

        pushed = [item["historyId"] for batch in client.batches for item in batch["items"]]
        assert pushed == [8, 7, 6]
        assert adapter.calls == 3
        assert summary.finished is True
        assert checkpoint.payload["watermark"] == 8

    def test_single_instant_page_does_not_early_stop(self):
        """整页同一时间戳：同秒记录顺序不稳定，高于水位的新记录可能落在
        后页——不得提前收口（宁可多读一页）。"""
        checkpoint = MemoryCheckpoint()
        checkpoint.payload = {"watermark": 100}
        adapter = FakeAdapter(
            {
                1: [_raw(3, date="2026-09-01 10:00:00"), _raw(2, date="2026-09-01 10:00:00")],
                2: [_raw(1, date="2026-09-01 09:00:00")],
            },
            total=3,
            page_size=2,
        )
        client = FakeClient()
        summary = _run(_engine(adapter, client, checkpoint, batch_size=2))
        assert adapter.calls == 2  # 第 1 页未收口，继续走到第 2 页
        assert client.batches == []
        assert summary.finished is True
        assert checkpoint.payload["watermark"] == 100

    def test_missing_date_does_not_early_stop(self):
        """date 缺失（排序依据不完整）时不提前收口，防止漏同步。"""
        checkpoint = MemoryCheckpoint()
        checkpoint.payload = {"watermark": 100}
        adapter = FakeAdapter({1: [_raw(3, date=None), _raw(2, date=None)]}, total=50, page_size=10)
        client = FakeClient()
        summary = _run(_engine(adapter, client, checkpoint, batch_size=10))
        assert adapter.calls == 2  # 读取空页后才结束
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


class TestPartialFailure:
    def test_partial_failure_keeps_watermark_and_resumes_whole_page(self):
        """回归：批次部分失败（服务端逐条确认 failed>0）不得推进水位；
        本趟立即收口，断点留在失败页，重试整页时已成功条目由服务端幂等吸收。"""
        checkpoint = MemoryCheckpoint()
        adapter = FakeAdapter(
            {
                1: [_raw(6), _raw(5)],
                2: [_raw(4), _raw(3)],
                3: [_raw(2), _raw(1)],
            },
            total=6,
            page_size=2,
        )
        client = FakeClient(
            results=[
                {"inserted": 2, "updated": 0, "skipped": 0, "failed": 0, "errors": []},  # 第 1 页
                {  # 第 2 页部分失败：id=3 被拒
                    "inserted": 1, "updated": 0, "skipped": 0, "failed": 1,
                    "errors": [{"historyId": 3, "error": "字段校验失败"}],
                },
            ]
        )
        engine = _engine(adapter, client, checkpoint, batch_size=2)
        summary = _run(engine, mode="full")

        # 未整趟完成：不报成功、不推进水位、断点留在失败页（第 2 页）；
        # 本趟立即收口，不再读取/推送第 3 页
        assert summary.finished is False
        assert "部分记录推送失败" in summary.stopped_reason
        assert summary.failed == 1
        assert any("historyId=3" in err for err in summary.errors)
        assert checkpoint.payload.get("watermark", 0) == 0
        assert checkpoint.payload["pass"]["mode"] == "full"
        assert checkpoint.payload["pass"]["next_page"] == 2
        assert summary.pages_walked == 2

        # 重试：从断点页（第 2 页）续跑，重推 [4,3]（3 重试成功、4 幂等跳过），
        # 再走第 3 页 [2,1]（首次推送）；全程不产生重复
        calls_before = adapter.calls
        client2 = FakeClient(
            results=[
                {"inserted": 1, "updated": 0, "skipped": 1, "failed": 0, "errors": []},  # [4,3]
                {"inserted": 2, "updated": 0, "skipped": 0, "failed": 0, "errors": []},  # [2,1] 首次推送
            ]
        )
        engine2 = _engine(adapter, client2, checkpoint, batch_size=2)
        summary2 = _run(engine2, mode="full")

        assert adapter.calls - calls_before == 2  # 重读第 2、3 页
        assert summary2.finished is True
        assert summary2.inserted == 3 and summary2.skipped == 1
        assert checkpoint.payload["watermark"] == 6
        assert "pass" not in checkpoint.payload
        # 重试批次正是失败页起的整页内容
        retried = [item["historyId"] for batch in client2.batches for item in batch["items"]]
        assert retried == [4, 3, 2, 1]

    def test_partial_failure_then_read_error_keeps_failed_page_checkpoint(self):
        """回归（P1）：第 1 页部分失败后若继续游走、第 2 页读取异常，
        中间保存的断点（next_page+1）会丢失失败页信息，重试将越过失败
        记录推进水位。修复后本趟在失败页立即收口：第 2 页根本不会被读取，
        断点稳定留在失败页。"""
        checkpoint = MemoryCheckpoint()

        class AdapterExplodesOnPage2(FakeAdapter):
            def fetch_page(self, page, count):
                if page == 2:
                    raise MoviePilotAdapterError("读取 MoviePilot 整理历史失败（HTTP 500）")
                return super().fetch_page(page, count)

        adapter = AdapterExplodesOnPage2({1: [_raw(4), _raw(3)]}, total=4, page_size=2)
        client = FakeClient(
            results=[
                {  # 第 1 页 [4,3]：3 推送失败
                    "inserted": 1, "updated": 0, "skipped": 0, "failed": 1,
                    "errors": [{"historyId": 3, "error": "字段校验失败"}],
                }
            ]
        )
        summary = _run(_engine(adapter, client, checkpoint, batch_size=2), mode="full")

        # 部分失败立即收口：第 2 页未读取（读取异常无从发生），断点留第 1 页
        assert adapter.calls == 1
        assert summary.finished is False
        assert "部分记录推送失败" in summary.stopped_reason
        assert checkpoint.payload.get("watermark", 0) == 0
        assert checkpoint.payload["pass"]["next_page"] == 1

        # 恢复读取后重试：3 被重新推送并成功，随后水位才推进到 4
        adapter2 = FakeAdapter({1: [_raw(4), _raw(3)], 2: [_raw(2), _raw(1)]}, total=4, page_size=2)
        client2 = FakeClient(
            results=[
                {"inserted": 1, "updated": 0, "skipped": 1, "failed": 0, "errors": []},  # [4,3]：3 重试成功
                {"inserted": 2, "updated": 0, "skipped": 0, "failed": 0, "errors": []},  # [2,1]
            ]
        )
        summary2 = _run(_engine(adapter2, client2, checkpoint, batch_size=2), mode="full")
        assert summary2.finished is True
        pushed = [item["historyId"] for batch in client2.batches for item in batch["items"]]
        assert 3 in pushed  # 失败记录确已重试
        assert checkpoint.payload["watermark"] == 4
        assert "pass" not in checkpoint.payload

    def test_incremental_partial_failure_retries_failed_item_only_new(self):
        """增量部分失败：失败条目重试成功，已成功条目幂等跳过，水位最终推进。"""
        checkpoint = MemoryCheckpoint()
        checkpoint.payload = {"watermark": 5}
        adapter = FakeAdapter({1: [_raw(7), _raw(6)]}, total=2, page_size=2)
        client = FakeClient(
            results=[
                {  # 7 成功、6 失败
                    "inserted": 1, "updated": 0, "skipped": 0, "failed": 1,
                    "errors": [{"historyId": 6, "error": "字段校验失败"}],
                }
            ]
        )
        engine = _engine(adapter, client, checkpoint, batch_size=2)
        summary = _run(engine)

        assert summary.finished is False
        assert checkpoint.payload.get("watermark") == 5  # 水位未推进
        assert checkpoint.payload["pass"]["next_page"] == 1

        client2 = FakeClient(
            results=[
                {"inserted": 1, "updated": 0, "skipped": 1, "failed": 0, "errors": []},  # 7 幂等跳过，6 成功
            ]
        )
        summary2 = _run(_engine(adapter, client2, checkpoint, batch_size=2))
        assert summary2.finished is True
        assert summary2.inserted == 1 and summary2.skipped == 1
        assert checkpoint.payload["watermark"] == 7
        assert "pass" not in checkpoint.payload
