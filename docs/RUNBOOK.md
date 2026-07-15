# 本地运行与日终运维手册

本文描述当前仓库已经实现的 Windows 本地运行、日终、备份和恢复验证流程。命令入口统一位于 `scripts/`。

## 1. 前置条件

- Python 3.11+、uv 及仓库 `uv.lock`；
- Node.js、npm 及前端锁文件；
- Windows PowerShell 5.1+ 或 PowerShell 7；
- 当前持久化使用 Python stdlib SQLite，不需要 PostgreSQL 或 Compose；
- 默认 provider 是确定性 Demo；Tushare/AKShare 可用于公开日频研究与模拟观察，但不是 PIT/生产数据；
- 系统时区可任意，但业务时间必须按 `Asia/Shanghai`。

不得配置券商 API key、交易证书或真实交易账户。本项目不需要也不接受这些凭据。

## 2. 环境配置

运行时数据源必须显式配置。示例：

```text
QUANT_DB_PATH=data/quant_system.db
QUANT_RESEARCH_PATH=data/research
QUANT_DATA_PROVIDER=demo
```

相对路径以启动进程时的项目根目录为基准。`.env` 当前不会被应用自动加载；需要改写时，在 PowerShell 设置对应的 `$env:...` 变量，或向脚本传 `-DatabasePath`。不得添加券商账户、交易 API key 或交易证书。

### 2.1 公开接口模式

```powershell
uv sync --extra providers
$env:QUANT_DATA_PROVIDER = "tushare"
$env:QUANT_TUSHARE_TOKEN = "<仅本机保存的 token>"
# 可选；默认 1.25 秒，低于常见 50 次/分钟权限
$env:QUANT_TUSHARE_MIN_REQUEST_INTERVAL_SECONDS = "1.25"
.\scripts\eod.ps1 -AsOf 2026-07-10 -EnforceFreshness
```

`tushare` 会严格拒绝非 SSE 交易日，拉取最近 120 个交易日的全市场日线、逐日复权因子、行业和可选估值/指数代理，并记录其为公开、非 PIT 数据。缺 token、缺可选依赖、核心接口不可用或数据不完整会失败，不会回退为 Demo。`akshare` 只允许明确指定范围：`QUANT_DATA_PROVIDER=akshare` 加 `QUANT_AKSHARE_SYMBOLS=600519.SH,000001.SZ`；它使用前复权行情和当前行业快照，行业/历史/OHLC 任一不完整即阻止观察。服务器机房无法访问实时个股元数据时，可设置 `QUANT_AKSHARE_METADATA_PATH=deploy/akshare-universe.csv`；文件必须包含 `symbol,name,industry,list_date`，缺失请求标的或字段会失败关闭，实际回退来源写入 `security_metadata` 审计。东方财富行情被限流时，适配器切换至新浪前复权日线，成交量统一为股并在 `price_history` 中逐标的记录来源。公开接口数据可用于本地研究和前瞻模拟观察，但不具备历史成分、发布时间、修订版本和商业授权证据，不能作为生产研究门禁的通过依据。

## 3. 首次启动检查表

1. 在项目根目录执行 `.\scripts\test.ps1 -Install`，安装锁定依赖并验证后端、运维、前端和生产构建。
2. 执行 `.\scripts\dev.ps1`。API/Web 以隐藏后台进程启动，PID 与日志写入 `data/runtime/dev-processes.json`。脚本不启用热重载，以确保记录的 PID 就是监听进程且可以干净停止。
3. 检查 `GET /api/v1/health/live` 与 `GET /api/v1/health/ready`。
4. 打开 `http://127.0.0.1:5173`，确认数据状态明确显示 Demo 或最新时间。
5. 按 `dev-processes.json` 中 PID 使用 `Stop-Process` 停止服务。

开发脚本默认 API 端口 8000、Web 端口 5173，可用 `-ApiPort`、`-WebPort` 改写。脚本不自动打开浏览器，也不启动任何交易组件。

## 4. 日终任务

### 4.1 正常时间线

仅在 A 股交易日且收盘数据预计完整后执行：

1. 读取交易日历，确定 `trade_date`；非交易日正常退出，不复制建议冒充新建议。
2. 采集日线、证券状态、复权/公司行为、指数/广度、题材/财务/公告、资金代理及全球风险。
3. 冻结原始数据批次，生成 `data_snapshot_id`。
4. 执行 DAT-007 质量门禁；关键失败则停止发布。
5. 生成特征并运行市场→题材→个股→组合→风险。
6. 校验组合不变量：数量、权重、题材、产业链、现金、容量、保护价。
7. 追加写决策快照并原子发布；记录 `decision_id/model_version/run_id`。当前实现使用 stdlib SQLite，默认路径 `data/quant_system.db`，可由 `QUANT_DB_PATH` 改写。
8. 将决策作为下一可成交日的模拟意图，绝不发真实订单。
9. 更新前端缓存/报告并发送成功或降级通知。

当前 CLI 根据 provider、日期、模型和组合配置稳定计算 run key；同日期同配置重跑不会产生第二份模拟意图。执行：

```powershell
.\scripts\eod.ps1 -AsOf 2026-07-03
```

非 Demo 数据接入后使用 `.\scripts\eod.ps1 -AsOf YYYY-MM-DD -EnforceFreshness`。公开接口模式只有在唯一生产阻断项为 `NOT_PRODUCTION_READY` 时才进入 `observation_only`：它可以记录前瞻模拟意图，但 `published=false`、`production_published=false`，前端持续显示非 PIT/非生产边界。任何复权、覆盖率、分组、OHLC、新鲜度或必需数据错误仍立即停止。当前默认 Demo 若不传日期，会使用代码内固定日期 2026-07-03，以确保复现；不得把该日期误认为实时行情。

### 4.2 成功检查

- ready 状态为 healthy，数据日期等于预期最新交易日；
- 组合状态为 healthy/partial/risk_off 之一，而非 stale；
- 0～5 只持仓符合约束；partial/risk_off 的现金与原因明确；
- 审计快照能回查所有输入版本和过滤原因；
- 模拟台账只生成意图，成交时间不早于下一可成交时点；
- UI 时间、模型版本、风险提示与 API 一致。

## 5. 数据过期与故障处理

### 5.1 数据尚未更新或过期

1. 不发布新的健康建议，不更新建议日期。
2. 上一建议保持只读，标记“数据过期/仅供回看”，显示最后成功时间。
3. 检查交易日历、provider 状态、各数据集最新 `effective_at/public_at` 和质量报告。
4. 数据补齐后用同一 run key 重跑；确认没有重复模拟成交。
5. 若开盘前仍未恢复，维持暂停并通知用户，不以插值或旧数据补核心字段。

### 5.2 Provider 失败

- 单一非关键分项失败：仅当策略定义了可验证降级路径时继续，API/UI 标记 `partial`，记录重分配/缺失规则。
- 核心行情、证券状态、复权或交易日历失败：立即停止建议。
- 不得自动从 Demo 数据回退生产；切换 provider 必须产生新 snapshot 并完成交叉核验。

### 5.3 质量门禁失败

按 `run_id` 保存失败报告。先判断上游延迟、schema 变化、真实市场异常或代码回归；不得为“让任务变绿”手工关闭门禁。门限变更需代码审查、版本化和历史回归。

### 5.4 日终计算失败

保留最近已发布决策，标记 stale；不得发布半成品。修复后以原 run key 重跑，比较重跑前后的数据 snapshot 和模型版本。若任一发生变化，必须创建更正版本并注明原因。

### 5.5 组合约束告警

任何单股>25%、3只时现金<25%、同题材>2、产业链>45%、总权重超市场上限、保护价下降或正式持仓>5，均视为发布阻断错误。不得在前端层裁剪结果掩盖后端错误。

### 5.6 模拟盘对账失败

立即暂停应用新模拟意图；保存原始台账，禁止手工改余额。检查重复幂等键、公司行为、费用、价格精度和部分成交；通过补偿事件纠正，不能修改历史事件。

## 6. 回测运行

1. 冻结数据 snapshot、特征/模型版本、配置、成本模型、seed 和样本切分。
2. 先跑小 fixture 回归，再跑完整研究。
3. 调参只看训练/验证区间；冻结后才运行最终隔离测试集。
4. 同时生成三基线、四资金档、敏感性、消融和压力结果。
5. 产出 `manifest` 与 `gates`；任一硬门槛失败时 UI 显示“不通过”。
6. 不删除失败实验，不用新参数覆盖旧模型版本。

长任务失败可从已验证 checkpoint 重启，但 checkpoint 必须绑定 snapshot/config hash。不得把不同数据或配置的片段拼成一个报告。

## 7. 模拟盘运行

- 每个交易日先处理此前意图在当日是否可成交，再按收盘后新决策生成下一交易日意图。
- 处理停牌、涨跌停、成交量上限、100 股取整、费用、滑点、部分成交和拒绝。
- 每日对账：`期末现金 + 持仓市值 = 期末净值`（允许明确的小数精度容差）。
- 每日报告回撤、风险动作、未成交及模拟与模型目标偏差。
- 策略修改必须新建模拟账户或明确分段，不能污染连续 8～12 周观察证据。

## 8. 备份、恢复与升级

当前 SQLite 开启 WAL。必须使用脚本中的 SQLite 在线 backup API，不能在服务运行时只复制 `.db` 文件：

```powershell
$backup = .\scripts\backup.ps1 -DestinationRoot D:\safe-backups
.\scripts\restore-verify.ps1 -BackupPath $backup -RestoreDirectory D:\restore-drill\2026-07-06
```

`backup.ps1` 默认备份 `QUANT_DB_PATH`（未设置时为 `data/quant_system.db`），以及存在的 `data/research`、`data/audit`，生成包含长度和 SHA-256 的 `manifest.json`，并在完成前执行 SQLite integrity check。

`restore-verify.ps1` 的两个参数都是强制项。恢复目标必须明确指定且尚不存在；脚本先校验源备份，复制到新目录，再校验副本 hash，并只读执行 `PRAGMA integrity_check`。它不会把文件写回当前数据库路径，也不会覆盖任何已有目录。恢复失败时保留源数据库不变。

建议每日备份并将备份根目录放在项目目录之外；定期执行隔离恢复演练。升级前执行 `.\scripts\test.ps1` 和备份；升级后检查 ready、代表性历史决策、模拟台账以及一次显式日期的 Demo 日终。

## 9. 发布前人工巡检

- 首页能在 1 分钟内读出明日动作、仓位、原因和退出条件；
- 模型输出、模拟持仓和真实持仓概念没有混淆；
- 数据时间/状态、模型版本和风险提示持续可见；
- 3～5、三个月、10～1000 万和浮盈回吐 30% 均有测试证据；
- 无自动交易入口、broker 依赖、券商密钥或保证性文案；
- 研究门禁及真实 8～12 周模拟观察期满足后，才允许标记正式 MVP。

## 10. 故障记录模板

```text
事件编号：
开始/恢复时间（Asia/Shanghai）：
影响交易日与用户范围：
run_id / snapshot_id / model_version：
现象与告警：
是否抑制新建议：是/否（理由）
数据/策略/模拟/展示哪一层：
根因：
修复与验证证据：
是否产生更正版本：
预防措施与负责人：
```
