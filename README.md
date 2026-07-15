# A股中期波段精选系统

面向个人投资者的 A 股研究与决策辅助 MVP：收盘后生成可解释的 3～5 只模型组合，提供市场温度、题材、个股证据、回测、决策审计和持久化模拟台账。系统不会连接券商，也不会提交真实订单。

## 当前可运行能力

- FastAPI API、确定性 Demo，以及可配置的公开日频数据接入；
- 市场、题材、个股评分及 3～5 只组合；
- 收盘信号、下一交易时点模拟意图和 SQLite 模拟台账；
- 带费用、滑点、整手及不可成交约束的研究回测；
- React/Vite 中文响应式前端；
- Windows 日终、测试、安全备份和隔离恢复验证脚本。

Demo 只能证明工程功能，不证明真实数据有效性或策略收益。真实连续模拟观察未满 8～12 周前，本项目只能标记为工程候选版。

## 要求

- Windows PowerShell 5.1+ 或 PowerShell 7；
- Python 3.11+ 与 [uv](https://docs.astral.sh/uv/)；
- Node.js 与 npm。

运行时通过 `QUANT_DATA_PROVIDER=demo|csv|licensed-csv|tushare|akshare` 明确选择数据源，见 `.env.example`。`csv` 必须同时配置 `QUANT_DATA_PATH`；`licensed-csv` 必须配置 `QUANT_DATA_BUNDLE`，且数据包的授权、PIT 声明和文件哈希必须全部通过。`tushare` 必须配置本机环境变量 `QUANT_TUSHARE_TOKEN`（或 `TUSHARE_TOKEN`）；`akshare` 必须配置 `QUANT_AKSHARE_SYMBOLS`。非法值或缺少配置会使服务启动失败，不会静默回退 Demo。默认数据库为 `data/quant_system.db`、研究制品目录为 `data/research`。

## 公开数据接入（研究与模拟观察）

先安装可选 provider 依赖，并只在本机设置 token：

```powershell
uv sync --extra providers
$env:QUANT_DATA_PROVIDER = "tushare"
$env:QUANT_TUSHARE_TOKEN = "<你的 Tushare Pro token>"
.\scripts\eod.ps1 -AsOf 2026-07-10 -EnforceFreshness
```

Tushare 会校验请求日是 SSE 交易日，抓取最近 120 个交易日的日线、复权因子、行业、估值截面和指数代理；默认每次请求间隔 1.25 秒，完整加载约 5 分钟。没有 token 时也可使用 AKShare：它对显式股票列表读取前复权行情和行业快照，默认每次请求间隔 0.5 秒；行业或行情不完整会整批停止。若实时个股元数据端点不可用，可通过 `QUANT_AKSHARE_METADATA_PATH` 指向包含 `symbol,name,industry,list_date` 的显式清单；每只股票的回退来源都会进入审计，缺项仍会阻断。东方财富前复权日线失败时会切换到 AKShare 新浪前复权日线，并统一校正成交量为“股”；实际来源逐标的写入 `price_history` 审计。公开源只有在复权、覆盖率、行业分组和新鲜度等核心门禁全部通过时，才会生成明确标记为 `observation_only` 的决策观察；只有同时取得完整涨跌停与停牌约束时才生成/撮合模拟意图，AKShare 当前默认不撮合。它仍是“非 PIT、非生产”，不能通过生产研究门禁、不能替代授权数据包，也不会开启自动交易。token 不会写入数据库、前端或日志；不要写入 `.env.example` 或提交到仓库。

## 一键开发

首次安装并在隐藏后台进程启动 API 与 Web：

```powershell
.\scripts\dev.ps1 -Install
```

后续启动可省略 `-Install`。默认地址：

- Web：`http://127.0.0.1:5173`
- API：`http://127.0.0.1:8000`
- OpenAPI：`http://127.0.0.1:8000/docs`
- 就绪检查：`http://127.0.0.1:8000/api/v1/health/ready`

脚本直接启动最终 Python/Node 监听进程，并将 PID 和日志路径写入 `data/runtime/dev-processes.json`。它不会自动打开浏览器或启用热重载；修改后重启即可。可按脚本输出的 PID 使用 `Stop-Process` 停止两个进程，不会遗留 reloader 子进程。

## Linux / Docker 部署

仓库根目录包含生产容器配置。默认以 AKShare 的 30 只显式观察池启动，最终组合仍严格控制为 3～5 只；API 仅暴露在容器网络内，由轻量 Node 静态网关同源代理 `/api`，SQLite 与研究制品保存在 Docker 持久卷中。

```bash
git clone https://github.com/huejie/a-share-swing-quant.git /opt/a-share-swing-quant
cd /opt/a-share-swing-quant
cp deploy/.env.example .env
chmod 600 .env
sh deploy/deploy.sh
curl http://127.0.0.1/health
```

首次访问仪表盘会抓取 AKShare 数据，可能需要几十秒到数分钟。公开网页接口可能限流或临时不可用；这种情况下服务会明确降级/失败，不会静默回退 Demo。当前仅部署 HTTP，面向公网前应配置域名、HTTPS 和访问控制。

## 测试与构建

```powershell
.\scripts\test.ps1 -Install
```

该命令执行全部 Python 测试（含运维备份恢复测试）、前端测试和生产构建。已安装依赖时省略 `-Install`；可使用 `-SkipFrontend` 或 `-SkipBuild` 缩小本地检查范围。

## 日终任务

确定性 Demo 默认运行代码内约定日期；也可显式指定交易日：

```powershell
.\scripts\eod.ps1 -AsOf 2026-07-03
```

非 Demo 数据应同时使用 `-EnforceFreshness`。Tushare/AKShare 即使能够读取行情也始终标记为非 PIT、非生产原型，不能作为生产研究或正式发布数据源；可用作公开数据的本地研究与模拟观察。授权 CSV 数据包的格式、构建和校验方法见 `docs/DATA_BUNDLE.md`。

## 备份与恢复验证

SQLite 备份使用在线 backup API，可安全处理 WAL 中的活动数据库；同时复制存在的 `data/research` 和 `data/audit` 制品并生成 SHA-256 manifest：

```powershell
$backup = .\scripts\backup.ps1 -DestinationRoot D:\safe-backups
.\scripts\restore-verify.ps1 -BackupPath $backup -RestoreDirectory D:\restore-drill\2026-07-06
```

恢复脚本只允许恢复到显式指定且不存在的新目录，复制前后校验长度与 SHA-256，并以只读方式执行 SQLite `PRAGMA integrity_check`。它永远不会覆盖当前数据库。

## 项目文档

- `PROJECT_PLAN.md`：产品与实施方案；
- `docs/REQUIREMENTS.md`：可验证需求；
- `docs/ARCHITECTURE.md`：模块边界与 API 契约；
- `docs/ACCEPTANCE.md`：验收矩阵；
- `docs/RUNBOOK.md`：日终、过期、故障、备份与恢复操作。
- `docs/DEPLOYMENT.md`：当前服务器部署、更新、定时任务与回滚记录。

本产品仅供研究与决策辅助，不构成收益保证，不连接券商，不自动交易。
