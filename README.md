# MoviePilot-Plugins-BtDeck（MoviePilot V2 插件市场仓库）

本仓库是一个 [MoviePilot](https://github.com/jxxghp/MoviePilot) V2 插件市场仓库，收录 BtDeck 相关插件：

- **[BtDeckBridge](./plugins.v2/btdeckbridge/)** —— 将 MoviePilot 的整理历史（TransferHistory）**只读**同步到
  [BtDeck](https://github.com/strainhzj/BtDeck)，建立「媒体库文件 ↔ 整理源文件 ↔ BT 任务」关联，帮助识别与保留需要的
  媒体库文件、源数据与种子任务。

BtDeckBridge 特性：

- 对 MoviePilot 零写入：仅通过宿主自身 API 分页读取整理历史，不修改
  历史记录、不删除/移动文件、不触碰 BT 任务；
- 对 BtDeck 零写入：只调用握手与同步两个只读镜像端点，不产生任何对
  生产任务的删除/暂停/修改；
- 同步策略：增量水位（新整理的记录总会出现在最前页）+ 周期性全量重扫
  （捕获重新整理/历史字段更新，不依赖"最大 ID"）+ 断点续跑（页级断点，
  服务端按 实例+历史ID 幂等去重）；
- 部分失败安全：单批中个别记录被服务端拒绝时，本趟立即收口——不推进
  水位、断点留在失败页，下次同步重试整页（已成功条目由服务端幂等吸收，
  不产生重复数据），状态页与 API 返回值如实呈现失败原因。

## 宿主版本支持（V2/V3）

- **已验证（源码级）**：V2 线。插件契约（定时服务 `kwargs` 结构、
  `TransferHistory` 字段 `id` 与 `date` 倒序分页、插件目录
  `requirements.txt` 依赖发现、`version.py` 的 `APP_VERSION`）已对照
  jxxghp/MoviePilot `v2` 分支源码（v2.15.6）逐条核验。
- **未验证（不声明支持）**：V3。v3.0.0 源码显示其同样扫描 `plugins.v2`
  目录、调度与依赖发现契约一致，但**未在 V3 宿主上运行验证过**；
  `package.v2.json` 的 `system_version` 限定为 `>=2.0.0,<3`，
  在 V3 宿主上安装会被版本门槛拒绝。完成 V3 实机联调后再放宽。
- 本仓库未包含 V2 运行环境，以上均为对宿主源码的静态核验结论，
  实机安装/联调步骤见 BtDeck 仓库 `PLANS/moviepilot-integration.md`。

## 安装

**方式一：插件市场（推荐）**

把本仓库地址加入 MoviePilot 的 `PLUGIN_MARKET`（设置 → 插件市场，逗号分隔可多个；或环境变量）：

```
https://github.com/strainhzj/MoviePilot-Plugins-BtDeck/
```

刷新后在「插件市场」安装 **BtDeckBridge**。远程市场要求 GitHub 仓库默认分支为 `main`
（MoviePilot 固定从 `raw.githubusercontent.com/{user}/{repo}/main/package.v2.json` 拉取索引）。

**方式二：本地插件仓库（开发/联调用，零网络零 git）**

`PLUGIN_LOCAL_REPO_PATHS` 指向的目录会被当作"本地市场仓库"扫描
（`package.v2.json` → `plugins.v2/<插件>/`），命中的插件出现在插件市场并标记
"本地"，安装时直接从挂载目录复制进 `app/plugins/`（`install_local`），不访问
GitHub、**不需要加入 PLUGIN_MARKET**；`PLUGIN_AUTO_RELOAD=true` 开启热重载：

```yaml
# docker compose 环境变量（把本仓库克隆后挂载进容器）
environment:
  - PLUGIN_LOCAL_REPO_PATHS=/plugin-dev
  - PLUGIN_AUTO_RELOAD=true
volumes:
  - /path/to/MoviePilot-Plugins-BtDeck:/plugin-dev
```

挂载后重启 MoviePilot，在「插件市场 → 本地」安装 BtDeckBridge。

## 配置

1. **BtDeck 侧**（先做）：创建一个专用集成账号（建议不开两步验证、不设强制改密）；
   设置 → MoviePilot → 打开集成开关。首次握手后实例自动注册，在该页配置
   「MoviePilot 下载器 → BtDeck 下载器」映射。
2. **插件侧**：填写 BtDeck 地址（**MoviePilot 容器可达的地址，勿填 localhost**）、
   集成账号用户名/密码、实例名称；按需调整同步间隔（分钟）、全量重扫间隔
   （小时）、每批条数（10-200）；勾选启用。
   - **同步间隔**：正数按分钟注册周期任务（最小 5 分钟，低于下限按下限执行）；
     **填 0 关闭周期任务**，之后只通过手动 API 触发同步；空值/非法值/负数
     回退默认 30 分钟。

## 手动触发

```
GET /api/v1/plugin/BtDeckBridge/sync?mode=incremental   # 增量
GET /api/v1/plugin/BtDeckBridge/sync?mode=full          # 强制全量重扫（幂等）
```

（宿主 apikey 鉴权，即浏览器已登录态或 `apikey` 查询参数。）

## 结构

```
package.v2.json                     # 市场索引（键 = 类名）
plugins.v2/btdeckbridge/
  __init__.py                       # BtDeckBridge(_PluginBase)：配置/状态页、周期服务、手动 API
  moviepilot_adapter.py             # V2 版本边界：宿主历史 API 取数 + 协议字段映射
  btdeck_client.py                  # BtDeck 客户端：登录/刷新令牌轮换、握手、同步
  sync.py                           # 同步引擎：水位/断点/部分失败重试/停止信号
  requirements.txt                  # 运行依赖（宿主安装器只发现插件目录内的 requirements.txt）
requirements.txt                    # 仓库开发环境依赖提示（宿主安装器不读取）
tests/                              # 脱离宿主的单测（stub _PluginBase + httpx MockTransport）
```

## 测试

```bash
pip install -r requirements.txt   # 开发/测试依赖（httpx、pytest、APScheduler 3.x）
python -m pytest tests -q
```

不依赖 MoviePilot/BtDeck 运行环境（stub `_PluginBase` + httpx MockTransport）；
真实宿主联调步骤见 BtDeck 仓库 `PLANS/moviepilot-integration.md`。

## 许可证

[GPL-3.0](./LICENSE)（与 BtDeck 主项目一致）。
