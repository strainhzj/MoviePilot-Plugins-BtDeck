# -*- coding: utf-8 -*-
"""BtDeckBridge —— MoviePilot ⇄ BtDeck 整理关系联动插件（V2，v1 首版）。

将 MoviePilot 的整理历史（TransferHistory）只读同步到 BtDeck，建立
「媒体库文件 ↔ 整理源文件 ↔ BT 任务」关联。**本插件对两侧均零写入**：
不修改 MoviePilot 历史/文件，不触发 BtDeck 任务的删除/暂停/修改。

- 同步策略见 ``sync.py``（增量水位 + 周期全量重扫 + 断点续跑）；
- 取数与版本适配见 ``moviepilot_adapter.py``（V2 边界）；
- BtDeck 认证复用其用户令牌体系（专用集成账号），见 ``btdeck_client.py``；
- 实例身份：首次初始化生成 UUID 并持久化，作为 BtDeck 侧幂等身份基础。

宿主 API（get_api，apikey 鉴权）：

- ``GET /api/v1/plugin/BtDeckBridge/sync?mode=incremental|full`` 手动触发。
"""

from __future__ import annotations

import threading
import uuid
from typing import Any, Dict, List, Optional, Tuple

from app.plugins import _PluginBase

from .btdeck_client import BtDeckApiError, BtDeckClient
from .moviepilot_adapter import MoviePilotAdapterError, MoviePilotV2Adapter
from .sync import SyncEngine

PLUGIN_VERSION = "1.0.0"
PROTOCOL_VERSION = 1


class BtDeckBridge(_PluginBase):
    # 展示属性
    plugin_name = "BtDeckBridge"
    plugin_desc = "将 MoviePilot 整理历史同步到 BtDeck，建立媒体库↔源文件↔BT任务关联（只读联动）。"
    plugin_version = PLUGIN_VERSION
    plugin_author = "BtDeck"
    author_url = "https://github.com/strainhzj/BtDeck"

    # 配置键（与 get_form 的 model 一一对应）
    KEY_ENABLED = "enabled"
    KEY_URL = "btdeck_url"
    KEY_USERNAME = "username"
    KEY_PASSWORD = "password"
    KEY_INSTANCE_NAME = "instance_name"
    KEY_INTERVAL = "sync_interval_minutes"
    KEY_FULL_HOURS = "full_rescan_hours"
    KEY_BATCH = "batch_size"

    def __init__(self):
        super().__init__()
        self._config: Dict[str, Any] = {}
        self._stop_event = threading.Event()
        self._run_lock = threading.Lock()

    # ------------------------------------------------------------ 生命周期

    def init_plugin(self, config: dict = None):
        """配置生效（安装/重载/保存配置后由框架调用）。"""
        self._config = dict(config or {})
        # 新一轮生命周期：复位停止信号（正在跑的一趟持有旧事件引用，会自行收敛）
        self._stop_event = threading.Event()

    def get_state(self) -> bool:
        return bool(self._config.get(self.KEY_ENABLED))

    def stop_service(self):
        """停用/重载清理：通知在途同步中止并释放运行锁。"""
        self._stop_event.set()
        if self._run_lock.locked():
            self._log("BtDeckBridge 停用：等待在途同步收口（断点已保留）")
        else:
            self._log("BtDeckBridge 停用：无在途同步")

    # ------------------------------------------------------------ 周期与手动入口

    def get_service(self) -> List[Dict[str, Any]]:
        """注册周期同步任务（BackgroundScheduler 线程池执行，同步函数）。"""
        if not self.get_state():
            return []
        interval = self._positive_int(self.KEY_INTERVAL, default=30, minimum=5)
        return [
            {
                "id": "BtDeckBridgeSync",
                "name": "BtDeck 整理历史同步",
                "trigger": "interval",
                "func": self.sync_job,
                "minutes": interval,
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/sync",
                "endpoint": self.api_sync_now,
                "methods": ["GET"],
                "summary": "手动触发 BtDeck 同步（mode=incremental|full）",
                "description": "立即执行一次同步；mode=full 强制全量重扫（幂等）。",
            }
        ]

    def sync_job(self):
        """周期任务入口：auto 模式（按周期决定全量/增量）。"""
        self._run_sync("auto")

    def api_sync_now(self, mode: str = "incremental") -> Dict[str, Any]:
        """手动同步入口（宿主 apikey 鉴权后调用）。"""
        resolved = "full" if str(mode).lower() == "full" else "incremental"
        return self._run_sync(resolved)

    # ------------------------------------------------------------ 同步主体

    def _run_sync(self, mode: str) -> Dict[str, Any]:
        if not self._run_lock.acquire(blocking=False):
            message = "已有同步在途，本次跳过（防重复启动）"
            self._log(message)
            return {"ok": False, "message": message}
        try:
            if not self._config.get(self.KEY_URL):
                self._log("未配置 BtDeck 地址，跳过同步")
                return {"ok": False, "message": "未配置 BtDeck 地址"}
            adapter = self._build_adapter()
            client = BtDeckClient(
                base_url=str(self._config.get(self.KEY_URL) or ""),
                username=str(self._config.get(self.KEY_USERNAME) or ""),
                password=str(self._config.get(self.KEY_PASSWORD) or ""),
                token_store=_PluginDataStore(self, "btdeck_tokens"),
                logger=self._log,
            )
            try:
                engine = SyncEngine(
                    adapter=adapter,
                    client=client,
                    checkpoint_store=_PluginDataStore(self, "sync_checkpoint"),
                    logger=self._log,
                    stop_event=self._stop_event,
                    batch_size=self._positive_int(self.KEY_BATCH, default=100, minimum=10),
                )
                summary = engine.run(
                    instance_id=self._instance_id(),
                    instance_name=str(self._config.get(self.KEY_INSTANCE_NAME) or ""),
                    plugin_version=PLUGIN_VERSION,
                    moviepilot_version=self._moviepilot_version(),
                    mode=mode,
                    full_interval_hours=float(self._config.get(self.KEY_FULL_HOURS) or 24),
                )
            finally:
                client.close()
                adapter.close()
            self._save_status(summary.to_dict())
            return self._status_payload(summary)
        except (BtDeckApiError, MoviePilotAdapterError) as exc:
            payload = {"ok": False, "message": str(exc) or exc.__class__.__name__}
            self._save_status(payload)
            self._log(f"同步失败：{payload['message']}")
            return payload
        finally:
            self._run_lock.release()

    @staticmethod
    def _status_payload(summary) -> Dict[str, Any]:
        payload = summary.to_dict()
        payload["ok"] = bool(summary.finished)
        return payload

    # ------------------------------------------------------------ 宿主装配

    def _build_adapter(self) -> MoviePilotV2Adapter:
        """构造 V2 取数适配器（宿主配置懒加载，测试可绕开）。"""
        try:
            from app.core.config import settings
        except ImportError as exc:  # pragma: no cover - 宿主外运行（单测注入桩）
            raise MoviePilotAdapterError("MoviePilot 宿主配置不可用") from exc
        port = getattr(settings, "PORT", 3001)
        api_token = getattr(settings, "API_TOKEN", "")
        return MoviePilotV2Adapter(base_url=f"http://127.0.0.1:{port}", api_token=str(api_token or ""))

    def _moviepilot_version(self) -> str:
        try:
            from app.core.config import settings

            return str(getattr(settings, "VERSION", "") or "")
        except ImportError:  # pragma: no cover
            return ""

    def _instance_id(self) -> str:
        """实例 UUID：首次生成并持久化（BtDeck 侧幂等身份）。"""
        existing = self.get_data("instance_id")
        if isinstance(existing, str) and existing:
            return existing
        instance_id = uuid.uuid4().hex
        self.save_data("instance_id", instance_id)
        return instance_id

    def _save_status(self, payload: Dict[str, Any]) -> None:
        payload = dict(payload)
        payload["ok"] = bool(payload.get("finished"))
        self.save_data("sync_status", payload)

    def _positive_int(self, key: str, default: int, minimum: int) -> int:
        raw = self._config.get(key)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = default
        return max(minimum, value)

    def _log(self, message: str) -> None:
        """宿主日志（令牌等敏感信息在底层模块已脱敏，这里只透传文案）。"""
        try:
            from app.log import logger

            logger.info(f"[BtDeckBridge] {message}")
        except ImportError:  # pragma: no cover - 宿主外运行
            print(f"[BtDeckBridge] {message}")

    # ------------------------------------------------------------ 页面

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """配置页（Vuetify JSON，v2 默认渲染模式）。"""
        form = [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": self.KEY_URL,
                                            "label": "BtDeck 地址（MoviePilot 容器可达，勿用 localhost）",
                                            "placeholder": "http://192.168.1.10:9090",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": self.KEY_INSTANCE_NAME,
                                            "label": "实例名称（BtDeck 侧展示用）",
                                            "placeholder": "如：家庭 MoviePilot",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {"model": self.KEY_USERNAME, "label": "BtDeck 集成账号用户名"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": self.KEY_PASSWORD,
                                            "label": "BtDeck 集成账号密码",
                                            "type": "password",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": self.KEY_INTERVAL,
                                            "label": "同步间隔（分钟，0 关闭周期）",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": self.KEY_FULL_HOURS,
                                            "label": "全量重扫间隔（小时）",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": self.KEY_BATCH,
                                            "label": "每批条数（10-200）",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": self.KEY_ENABLED,
                                            "label": "启用插件（BtDeck 侧还需开启集成开关）",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ]
        default_config = {
            self.KEY_ENABLED: False,
            self.KEY_URL: "",
            self.KEY_USERNAME: "",
            self.KEY_PASSWORD: "",
            self.KEY_INSTANCE_NAME: "",
            self.KEY_INTERVAL: 30,
            self.KEY_FULL_HOURS: 24,
            self.KEY_BATCH: 100,
        }
        return form, default_config

    def get_page(self) -> Optional[List[dict]]:
        """状态页：最近一次同步结果与实例身份。"""
        status = self.get_data("sync_status")
        if not isinstance(status, dict):
            status = {}
        rows = [
            f"实例 ID：{self._instance_id()}（已持久化，重装插件前不变）",
            f"最近同步：mode={status.get('mode', '-')} finished={status.get('finished', '-')} "
            f"inserted={status.get('inserted', 0)} updated={status.get('updated', 0)} "
            f"skipped={status.get('skipped', 0)} failed={status.get('failed', 0)}",
            f"水位（watermark）：{status.get('watermark', '-')}",
        ]
        stopped_reason = status.get("stoppedReason")
        if stopped_reason:
            rows.append(f"中止原因：{stopped_reason}")
        return [
            {
                "component": "VAlert",
                "props": {
                    "type": "info" if status.get("ok") else ("warning" if status else "info"),
                    "variant": "tonal",
                    "density": "compact",
                    "text": "\n".join(rows),
                },
            },
            {
                "component": "VAlert",
                "props": {
                    "type": "tip",
                    "variant": "tonal",
                    "density": "compact",
                    "text": "手动同步：GET /api/v1/plugin/BtDeckBridge/sync?mode=full（apikey 鉴权）。"
                    "关联查询与下载器映射在 BtDeck 设置 → MoviePilot 页配置。",
                },
            },
        ]


class _PluginDataStore:
    """把宿主 save_data/get_data 适配为引擎所需的 load/save 边界。"""

    def __init__(self, plugin: BtDeckBridge, key: str):
        self._plugin = plugin
        self._key = key

    def load(self) -> Optional[Dict[str, Any]]:
        value = self._plugin.get_data(self._key)
        return value if isinstance(value, dict) else None

    def save(self, payload: Dict[str, Any]) -> None:
        self._plugin.save_data(self._key, payload)
