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
  服务端按 实例+历史ID 幂等去重）。

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
   集成账号用户名/密码、实例名称；按需调整同步间隔（分钟，0 关闭周期）、
   全量重扫间隔（小时）、每批条数（10-200）；勾选启用。

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
  sync.py                           # 同步引擎：水位/断点/重试/停止信号
requirements.txt
tests/                              # 脱离宿主的单测（stub _PluginBase + httpx MockTransport）
```

## 测试

```bash
python -m pytest tests -q
```

不依赖 MoviePilot/BtDeck 运行环境（stub `_PluginBase` + httpx MockTransport）；
真实宿主联调步骤见 BtDeck 仓库 `PLANS/moviepilot-integration.md`。

## 许可证

[GPL-3.0](./LICENSE)（与 BtDeck 主项目一致）。
