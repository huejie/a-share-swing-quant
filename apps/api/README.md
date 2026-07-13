# Quant API

FastAPI 服务端覆盖市场温度、题材雷达、3–5 只模型组合、备选池、股票证据、决策审计、模拟组合、回测、设置和数据状态。它没有券商依赖，也不会生成或发送真实订单。

## 本地运行

```powershell
python -m pip install -e ".[dev]"
uvicorn apps.api.main:app --reload
```

- API 文档：`http://127.0.0.1:8000/docs`
- 健康检查：`GET /health`
- 一页聚合数据：`GET /api/v1/dashboard`
- 日终流水线：`POST /api/v1/pipeline/eod`
- CLI：`quant-eod --as-of 2026-07-03`

默认 `DeterministicDemoProvider` 使用固定公式生成具备 A 股价格、成交额、板块与题材差异的演示数据；同一日期的结果严格一致。`CsvProvider` 可导入原型数据。`TushareProvider` 可通过 `QUANT_DATA_PROVIDER=tushare` 与本机 `QUANT_TUSHARE_TOKEN` 拉取最近 120 个交易日的日线、复权因子及可选增强数据（默认 1.25 秒请求节流，完整加载约 5 分钟）；`AkshareProvider` 需要显式 `QUANT_AKSHARE_SYMBOLS`。两者均为公开接口研究/模拟观察适配器：核心质量门禁通过后只生成 `observation_only` 决策观察；缺少完整涨跌停/停牌约束时不生成或撮合模拟指令。缺少包、密钥、交易日或网络时明确失败，不会退回 Demo；均不具备 PIT、历史成分和生产授权证明。

Windows 若 Python 由 uv 管理，可使用仓库已验证的命令：

```powershell
& "$env:USERPROFILE\.local\bin\uv.exe" sync --extra dev
& "$env:USERPROFILE\.local\bin\uv.exe" run uvicorn apps.api.main:app --reload
& "$env:USERPROFILE\.local\bin\uv.exe" run --extra dev pytest tests/backend -q
```

产品状态保存在 `data/quant_system.db`（可用 `QUANT_DB_PATH` 改写）。SQLite 开启 WAL；生产运维必须备份数据库文件及 `-wal` 状态，或先执行 checkpoint 后复制。健康检查分别为 `/api/v1/health/live` 与 `/api/v1/health/ready`。

日终请求应携带稳定幂等键：

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/pipeline/eod `
  -Headers @{"Idempotency-Key"="eod-2026-07-03-swing-rules-0.2.0"} `
  -ContentType "application/json" -Body '{"as_of":"2026-07-03"}'
```

同一 key 返回同一决策且不会重复写入模拟意图。非 Demo provider 默认强制新鲜度门禁；失败时不覆盖上一份已发布建议。模拟意图的 `effective_at` 不早于下一交易日 09:30，台账持续标记 `broker_connected=false`。

数据质量门禁将覆盖率、OHLC 合法性和新鲜度标为 `healthy` / `blocked`，生产发布可用 `enforce_freshness=true` 阻止陈旧数据覆盖上一版建议。

## 测试

```powershell
pytest tests/backend
```

回测遵循“收盘生成信号、下一交易日开盘成交”，模拟佣金、卖出印花税、滑点、100 股整手、1% 成交额容量以及停牌/涨跌停不可成交。结果仅是研究模拟，不是收益承诺。
