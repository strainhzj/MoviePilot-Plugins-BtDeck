# -*- coding: utf-8 -*-
"""整理历史同步引擎（BtDeckBridge 插件）。

同步策略（对应需求"不仅依赖最大历史 ID 判断增量"）：

- **增量（incremental）**：宿主列表按 date 倒序，从第 1 页向旧游走，
  收集 ``historyId > watermark`` 的条目分批推送；遇到"整页均 ≤ watermark
  且页内至少跨两个不同时间戳"才提前收口——新整理的记录总会出现在最前面
  的页里；整页同秒（date 相同）或存在缺失 date 时同秒顺序不稳定，继续
  游走防漏同步（时钟回拨造成的乱序由周期全量重扫兜底）；
- **全量（full）**：从头游走全部页（服务端按 (instance, historyId) 幂等
  去重，重复推送只会计入 skipped），用于首次同步、手动重扫与周期补偿
  （捕获重新整理/历史字段更新——这类更新不改变 id，增量游走发现不了）；
- **水位纪律**：``watermark`` 只在一趟完整走完后用本趟 ``max_seen`` 推进；
  中途失败/停止/部分失败均保留 ``pass`` 断点，下次从断点页续跑，已推批次
  靠服务端幂等去重，不会重复入库；
- **部分失败**：服务端单批返回 ``failed > 0``（逐条校验失败等）时本趟
  立即收口：不推进水位、断点留在失败页（其后各页下次重走，幂等无损）、
  结果如实上报 failed 数与原因；下次同步重试整页，已成功条目由服务端
  幂等吸收为 skipped；
- **重试**：单批网络/服务错误指数退避重试（默认 3 次），仍失败则中止本趟
  并保留断点；停止信号（插件停用/重载）在页与批之间检查，立即中止。

引擎与宿主解耦：adapter/client/checkpoint_store 均为注入的协议对象，
可脱离 MoviePilot 单测。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol

from .btdeck_client import BtDeckApiError, BtDeckClient
from .moviepilot_adapter import MoviePilotAdapterError, MoviePilotV2Adapter


class CheckpointStore(Protocol):
    """断点持久化边界（宿主注入：save_data/get_data）。"""

    def load(self) -> Optional[Dict[str, Any]]:
        ...

    def save(self, payload: Dict[str, Any]) -> None:
        ...


class _NullCheckpointStore:
    def load(self) -> Optional[Dict[str, Any]]:
        return None

    def save(self, payload: Dict[str, Any]) -> None:
        return None


@dataclass
class SyncSummary:
    """一趟同步的可展示结果（写入插件状态页与日志）。"""

    mode: str
    pages_walked: int = 0
    batches_pushed: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    finished: bool = False
    stopped_reason: str = ""
    watermark: int = 0
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "pagesWalked": self.pages_walked,
            "batchesPushed": self.batches_pushed,
            "inserted": self.inserted,
            "updated": self.updated,
            "skipped": self.skipped,
            "failed": self.failed,
            "finished": self.finished,
            "stoppedReason": self.stopped_reason,
            "watermark": self.watermark,
            "errors": self.errors[:5],
        }


class SyncEngine:
    """同步引擎（同步实现，跑在宿主调度器线程池里）。"""

    MAX_BATCH_ITEMS = 200
    MAX_RETRIES = 3
    RETRY_BACKOFF_SECONDS = 2.0
    MAX_PAGES_HARD_LIMIT = 10000  # 防御：宿主 total 异常时的硬上限

    def __init__(
        self,
        adapter: MoviePilotV2Adapter,
        client: BtDeckClient,
        checkpoint_store: Optional[CheckpointStore] = None,
        logger: Optional[Callable[[str], None]] = None,
        stop_event: Optional[threading.Event] = None,
        batch_size: int = 100,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        self.adapter = adapter
        self.client = client
        self.checkpoint_store: CheckpointStore = checkpoint_store if checkpoint_store is not None else _NullCheckpointStore()
        self.log = logger if logger is not None else (lambda message: None)
        self.stop_event = stop_event if stop_event is not None else threading.Event()
        self.batch_size = max(1, min(int(batch_size or 100), self.MAX_BATCH_ITEMS))
        self._sleep = sleep_fn

    # ------------------------------------------------------------ 入口

    def run(
        self,
        instance_id: str,
        instance_name: str,
        plugin_version: str,
        moviepilot_version: str,
        mode: str = "auto",
        full_interval_hours: float = 24.0,
    ) -> SyncSummary:
        """执行一趟同步。mode：incremental / full / auto（按周期决定全量或增量）。"""
        checkpoint = self._load_checkpoint()
        resolved_mode = mode
        if mode == "auto":
            resolved_mode = "full" if self._full_due(checkpoint, full_interval_hours) else "incremental"
        # 手动强制全量：清掉残留的"增量"断点从头走；但中断的全量断点保留续跑
        # （服务端幂等，重走亦无害——续跑只是省时）
        if resolved_mode == "full" and mode == "full":
            stored_pass = checkpoint.get("pass")
            if not isinstance(stored_pass, dict) or stored_pass.get("mode") != "full":
                checkpoint.pop("pass", None)

        summary = SyncSummary(mode=resolved_mode)
        try:
            self.client.handshake(instance_id, instance_name, plugin_version, moviepilot_version)
        except BtDeckApiError as exc:
            summary.stopped_reason = f"握手失败：{exc.message}"
            summary.errors.append(summary.stopped_reason)
            self.log(summary.stopped_reason)
            return summary

        try:
            self._run_pass(instance_id, resolved_mode, checkpoint, summary)
        except MoviePilotAdapterError as exc:
            summary.stopped_reason = f"读取整理历史失败：{exc}"
            summary.errors.append(summary.stopped_reason)
            self.log(summary.stopped_reason)
        except BtDeckApiError as exc:
            summary.stopped_reason = f"推送 BtDeck 失败：{exc.message}"
            summary.errors.append(summary.stopped_reason)
            self.log(summary.stopped_reason)
        finally:
            summary.watermark = int(checkpoint.get("watermark", 0) or 0)
            self._save_checkpoint(checkpoint)
        return summary

    # ------------------------------------------------------------ 一趟

    def _run_pass(self, instance_id: str, mode: str, checkpoint: Dict[str, Any], summary: SyncSummary) -> None:
        page_state = checkpoint.get("pass")
        if not isinstance(page_state, dict) or page_state.get("mode") != mode:
            page_state = {"mode": mode, "next_page": 1, "max_seen": int(checkpoint.get("watermark", 0) or 0)}
        watermark = int(checkpoint.get("watermark", 0) or 0)
        next_page = int(page_state.get("next_page", 1) or 1)
        max_seen = int(page_state.get("max_seen", watermark) or watermark)

        pending: List[Dict[str, Any]] = []
        # 部分失败策略：服务端确认 failed>0 的页，本趟立即收口并把断点留在
        # 该页（服务端按 (instance, historyId) 幂等去重，重推已成功条目只计
        # skipped，不产生重复）。不继续游走后续页：否则循环内的中间断点保存
        # 只记录 next_page+1，一旦后续读取异常提前退出，失败页信息就会丢失。
        first_partial_failed_page: Optional[int] = None

        def flush(is_last_batch: bool) -> None:
            nonlocal pending, first_partial_failed_page
            for start in range(0, len(pending), self.batch_size):
                chunk = pending[start : start + self.batch_size]
                last = is_last_batch and start + self.batch_size >= len(pending)
                batch_clean = self._push_batch(instance_id, mode, next_page, len(chunk), last, chunk, summary)
                if not batch_clean and first_partial_failed_page is None:
                    first_partial_failed_page = next_page
            pending = []

        while next_page <= self.MAX_PAGES_HARD_LIMIT:
            if self.stop_event.is_set():
                summary.stopped_reason = "插件停用，本趟中止（断点已保留）"
                break
            total, raw_items = self.adapter.fetch_page(next_page, self.batch_size)
            summary.pages_walked += 1
            if not raw_items:
                break
            for raw in raw_items:
                item = MoviePilotV2Adapter.to_protocol_item(raw)
                history_id = item.get("historyId")
                if isinstance(history_id, int):
                    max_seen = max(max_seen, history_id)
                if mode == "full":
                    pending.append(item)
                else:
                    if isinstance(history_id, int) and history_id > watermark:
                        pending.append(item)

            last_page = next_page * self.batch_size >= total
            if mode == "incremental":
                # 宿主原始记录的主键字段是 id（TransferHistory.to_dict 按列名输出）
                ids = [raw.get("id") for raw in raw_items]
                all_below_watermark = all(isinstance(i, int) and i <= watermark for i in ids)
                # 宿主按 date 倒序分页且同秒记录间顺序不稳定：整页同一时间戳或
                # 存在缺失 date 时，高于水位的记录仍可能落在后续页——继续游走，
                # 宁多读一页也不漏同步（时钟回拨导致的乱序由周期全量重扫兜底）
                page_dates = {raw.get("date") for raw in raw_items}
                ordering_safe = None not in page_dates and len(page_dates) >= 2
                if all_below_watermark and not pending and ordering_safe:
                    break

            # 每走完一页就落断点（页级续跑粒度；批内失败不推进页码）
            flush(last_page)
            if summary.stopped_reason:
                break
            if first_partial_failed_page is not None:
                # 本页部分失败：立即收口，断点留在本页（统一走末尾的收口逻辑）
                break
            page_state = {"mode": mode, "next_page": next_page + 1, "max_seen": max_seen}
            checkpoint["pass"] = page_state
            self._save_checkpoint(checkpoint)
            if last_page:
                break
            next_page += 1

        finished = not summary.stopped_reason and first_partial_failed_page is None
        if finished:
            # 整趟完成：推进水位并清除断点；全量重扫刷新 last_full_at
            checkpoint["watermark"] = max(int(checkpoint.get("watermark", 0) or 0), max_seen)
            checkpoint.pop("pass", None)
            if mode == "full":
                checkpoint["last_full_at"] = time.time()
            summary.finished = True
            summary.watermark = int(checkpoint["watermark"])
        else:
            if first_partial_failed_page is not None and not summary.stopped_reason:
                summary.stopped_reason = (
                    f"部分记录推送失败（failed={summary.failed}），"
                    f"断点保留在第 {first_partial_failed_page} 页，下次同步重试整页"
                )
            # 未整趟完成不推进水位：跨过失败记录推进会导致其永久漏同步
            resume_page = first_partial_failed_page if first_partial_failed_page is not None else next_page
            page_state = {"mode": mode, "next_page": resume_page, "max_seen": max_seen}
            checkpoint["pass"] = page_state
        self._save_checkpoint(checkpoint)
        self.log(
            f"同步完成 mode={mode} pages={summary.pages_walked} batches={summary.batches_pushed} "
            f"inserted={summary.inserted} updated={summary.updated} skipped={summary.skipped} "
            f"failed={summary.failed} finished={summary.finished}"
        )

    def _push_batch(
        self,
        instance_id: str,
        mode: str,
        page_number: int,
        page_size: int,
        is_last_batch: bool,
        items: List[Dict[str, Any]],
        summary: SyncSummary,
    ) -> bool:
        """推送一批，返回本批是否全部成功（failed==0 且未被中止/重试耗尽）。"""
        if not items:
            return True
        last_error: Optional[BtDeckApiError] = None
        for attempt in range(self.MAX_RETRIES):
            if self.stop_event.is_set():
                summary.stopped_reason = "插件停用，本趟中止（断点已保留）"
                return False
            try:
                result = self.client.sync_transfer_history(
                    instance_id=instance_id,
                    items=items,
                    sync_mode=mode,
                    page_number=page_number,
                    page_size=page_size,
                    is_last_batch=is_last_batch,
                )
                summary.batches_pushed += 1
                summary.inserted += int(result.get("inserted", 0) or 0)
                summary.updated += int(result.get("updated", 0) or 0)
                summary.skipped += int(result.get("skipped", 0) or 0)
                failed = int(result.get("failed", 0) or 0)
                summary.failed += failed
                for entry in result.get("errors", []) or []:
                    if isinstance(entry, dict):
                        summary.errors.append(f"historyId={entry.get('historyId')}: {entry.get('error')}")
                if failed > 0:
                    # 服务端确认的单条失败：本批不算干净，调用方保留整页重试断点
                    self.log(f"第 {page_number} 页有 {failed} 条记录推送失败，本趟将保留断点重试整页")
                    return False
                return True
            except BtDeckApiError as exc:
                last_error = exc
                if exc.http_status in (400, 401, 403, 404, 409, 422):
                    # 不可重试类错误：重试无意义，直接中止本趟
                    summary.stopped_reason = f"推送被拒绝（HTTP {exc.http_status}）：{exc.message}"
                    summary.errors.append(summary.stopped_reason)
                    return False
                self.log(f"推送失败（第 {attempt + 1} 次）：{exc.message}，退避后重试")
                self._sleep(self.RETRY_BACKOFF_SECONDS * (2**attempt))
        if last_error is not None:
            summary.stopped_reason = f"推送连续失败：{last_error.message}"
            summary.errors.append(summary.stopped_reason)
        return False

    # ------------------------------------------------------------ 断点

    def _load_checkpoint(self) -> Dict[str, Any]:
        stored = self.checkpoint_store.load()
        if isinstance(stored, dict):
            return stored
        return {}

    def _save_checkpoint(self, checkpoint: Dict[str, Any]) -> None:
        self.checkpoint_store.save(checkpoint)

    @staticmethod
    def _full_due(checkpoint: Dict[str, Any], full_interval_hours: float) -> bool:
        if full_interval_hours <= 0:
            return False
        last_full_at = checkpoint.get("last_full_at")
        if not isinstance(last_full_at, (int, float)) or last_full_at <= 0:
            return True
        return (time.time() - float(last_full_at)) >= full_interval_hours * 3600
