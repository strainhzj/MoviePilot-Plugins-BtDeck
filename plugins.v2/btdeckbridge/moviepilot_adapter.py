# -*- coding: utf-8 -*-
"""MoviePilot 宿主适配层（版本边界，当前实现 V2）。

设计约束（feature moviepilot-integration）：

- **只读**：仅通过 MoviePilot 自身 HTTP API（``/api/v1/history/transfer``）
  分页读取整理历史，绝不写 MoviePilot 的历史记录或数据库；
- **版本隔离**：V2 的取数与字段映射全部收敛在本模块；未来 V3 适配器实现
  同一接口（``fetch_page`` / ``to_protocol_item``）即可切换，``sync.py`` 与
  宿主插件不感知版本差异；
- V2 的该端点按 ``date`` 倒序分页、无 ID 范围查询能力——增量只能"从新到旧
  游走直到整页低于水位"，重新整理产生的旧记录更新由周期性全量重扫兜底。

认证：MoviePilot 内部 API 用实例自身的 ``API_TOKEN``（配置键 ``token`` 或
``apikey`` 查询参数）。凭据由宿主注入，本模块不读取宿主配置对象、不落日志。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import httpx


class MoviePilotAdapterError(Exception):
    """宿主侧取数失败（message 面向用户，不含凭据）。"""


class MoviePilotV2Adapter:
    """V2 适配器：自调用宿主 API 分页读取 TransferHistory。"""

    def __init__(
        self,
        base_url: str,
        api_token: str,
        timeout: float = 15.0,
        transport: Optional[httpx.BaseTransport] = None,
        verify: bool = True,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.api_token = api_token or ""
        self._http = httpx.Client(timeout=timeout, transport=transport, verify=verify)

    def close(self) -> None:
        self._http.close()

    def fetch_page(self, page: int, count: int) -> Tuple[int, List[Dict[str, Any]]]:
        """读取一页整理历史（date 倒序），返回 (total, items)。"""
        if not self.base_url:
            raise MoviePilotAdapterError("未配置 MoviePilot 自身访问地址")
        if not self.api_token:
            raise MoviePilotAdapterError("未取得 MoviePilot API_TOKEN，无法读取整理历史")
        try:
            response = self._http.get(
                f"{self.base_url}/api/v1/history/transfer",
                params={"page": page, "count": count, "apikey": self.api_token},
            )
        except httpx.HTTPError as exc:
            raise MoviePilotAdapterError(f"读取 MoviePilot 整理历史失败：{exc.__class__.__name__}") from exc
        if response.status_code != 200:
            raise MoviePilotAdapterError(f"读取 MoviePilot 整理历史失败（HTTP {response.status_code}）")
        try:
            body = response.json()
        except ValueError as exc:
            raise MoviePilotAdapterError("MoviePilot 历史接口返回非 JSON") from exc
        if not isinstance(body, dict) or not body.get("success"):
            message = body.get("message") if isinstance(body, dict) else None
            raise MoviePilotAdapterError(f"读取 MoviePilot 整理历史失败：{message or 'success=false'}")
        data = body.get("data") or {}
        items = data.get("list") if isinstance(data, dict) else None
        total = data.get("total") if isinstance(data, dict) else 0
        if not isinstance(items, list):
            items = []
        return int(total or 0), items

    # ------------------------------------------------------------ 字段映射

    @staticmethod
    def to_protocol_item(raw: Dict[str, Any]) -> Dict[str, Any]:
        """V2 TransferHistory.to_dict() → 集成协议 v1 条目（字段名对齐后端模型）。"""
        files = raw.get("files")
        files_list = files if isinstance(files, list) else None
        src_fileitem = raw.get("src_fileitem")
        dest_fileitem = raw.get("dest_fileitem")
        return {
            "historyId": raw.get("id"),
            "srcStorage": raw.get("src_storage"),
            "srcPath": raw.get("src"),
            "srcFileitem": src_fileitem if isinstance(src_fileitem, dict) else None,
            "destStorage": raw.get("dest_storage"),
            "destPath": raw.get("dest"),
            "destFileitem": dest_fileitem if isinstance(dest_fileitem, dict) else None,
            "transferMode": raw.get("mode"),
            "mediaType": raw.get("type"),
            "title": raw.get("title"),
            "year": raw.get("year"),
            "seasons": raw.get("seasons"),
            "episodes": raw.get("episodes"),
            "tmdbId": raw.get("tmdbid"),
            "doubanId": raw.get("doubanid"),
            "mediaSource": raw.get("media_source"),
            "mediaId": raw.get("media_id"),
            "mpDownloader": raw.get("downloader"),
            "downloadHash": raw.get("download_hash"),
            "status": raw.get("status"),
            "errmsg": raw.get("errmsg"),
            "recordedAt": raw.get("date"),
            "files": files_list,
        }
