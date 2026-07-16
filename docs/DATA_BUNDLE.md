# 授权 CSV 数据包交付规范

`licensed-csv` 是本地、只读、哈希锁定的数据交付格式。它只接受供应商或数据负责人已经明确声明的授权与 PIT 证据；构建工具不会创建、猜测或修改授权结论。

## 必需文件

- `bars.csv`：必须包含 `symbol,date,open,high,low,close,volume,amount,industry,published_at,effective_at,collected_at,available_at,is_st,is_delisting,regulatory_risk,audit_abnormal,event_risk,adj_factor,limit_up,limit_down,suspended,listed_trading_days,free_float_market_cap,schema_version,source_ref`。交易状态、精确交易日龄、流通市值、来源和复权字段均不是可选项。
- `securities.csv`：`symbol,name,listed_at,delisted_at,board`。退市记录必须保留。
- `theme_memberships.csv`：`symbol,theme,effective_from,effective_to,published_at,available_at`。
- `metadata.json`：授权、PIT 方法、批次及逐数据集新鲜度声明。
- `pit_records.csv`：必需，字段为 `dataset,entity_id,effective_at,published_at,collected_at,available_at,payload_json,revision,source_ref,parser_version`。所有字段均不可为空，`published_at ≤ collected_at ≤ available_at`，`payload_json` 必须是非空对象，`source_ref` 必须能追溯到原始来源。

四个 CSV 都必须被 manifest 哈希覆盖。仅在 `metadata.datasets` 中自称存在，不构成数据证据。

## 必需数据集与 PIT payload

`metadata.datasets` 必须明确把以下八类数据声明为 `required: true`，并为每类提供可解析的 `as_of`：

- `bars`：A股日行情及交易状态；
- `security_master`：含退市历史的证券主数据；
- `theme_memberships`：历史题材成分；
- `corporate_actions`：公司行为与复权因子；
- `financials`：财务质量与审计风险；
- `announcements`：公告催化与硬风险；
- `market_funding`：A股市场、融资及ETF资金/估值输入；
- `global_risk`：全球市场风险输入。

其中后五类必须在 `pit_records.csv` 中实际出现。证券级记录的 `entity_id` 使用股票代码，例如 `600000.SH`。最小 payload 契约如下：

| dataset | 必需 payload 字段 |
|---|---|
| `corporate_actions` | `adj_factor`、`event_type`、`share_multiplier`（正数）、`cash_dividend_per_share`（非负数）；后两项为事件值，只在首个生效交易日物化到 Bar |
| `financials` | `quality_score`（0–100）、`audit_abnormal`（布尔） |
| `announcements` | `catalyst_score`（0–100）、`event_risk,regulatory_risk,is_delisting,is_st` 四类风险布尔值，以及 `event_type,event_date,raw_text_ref,parser_version` |
| `market_funding` | `fund_flow_score,valuation_score`（0–100）及原始 `margin_balance,margin_balance_change,etf_share_change,market_breadth` |
| `global_risk` | `global_risk_score`（0–100）及 `global_equity,usd_cny,interest_rate,volatility_index,commodity_index` 五个原始分项 |

加载器会在每个 bar 的收盘时点选择当时已经发布且可用的最新记录：财务和公告分别物化为 `quality`、`catalyst` 与风险标记，公司行为物化为 `adj_factor`，市场与全球记录物化为 `market_inputs_history`。缺少对应证券/日期的记录、bar 与公司行为复权因子不一致，都会使生产门禁失败；不会回退到默认分数或伪造中性值。`listed_days` 按数据包中该证券已有交易日数量计算，不按自然日计算。

最小 metadata 示例：

```json
{
  "batch_id": "vendor-20260706-01",
  "provider": "contracted-vendor-name",
  "authorization": {
    "authorized": true,
    "scope": "internal-research-and-decision-support",
    "reference": "由数据负责人填写的合同或授权引用",
    "valid_until": "2030-12-31",
    "permitted_uses": ["research", "decision_support", "derived_output_display"]
  },
  "pit": {
    "verified": true,
    "method": "由数据负责人填写的发布时间与历史成分核验方法"
  },
  "datasets": {
    "bars": {"as_of": "2026-07-06T18:00:00+08:00", "max_age_hours": 72, "required": true, "source_ref": "vendor-bars", "schema_version": "bars/v2"},
    "security_master": {"as_of": "2026-07-06T18:00:00+08:00", "max_age_hours": 168, "required": true, "source_ref": "vendor-securities", "schema_version": "security/v2"},
    "theme_memberships": {"as_of": "2026-07-06T18:00:00+08:00", "max_age_hours": 168, "required": true, "source_ref": "vendor-themes", "schema_version": "themes/v2"},
    "corporate_actions": {"as_of": "2026-07-06T18:00:00+08:00", "max_age_hours": 168, "required": true, "source_ref": "vendor-actions", "schema_version": "actions/v2"},
    "financials": {"as_of": "2026-07-06T18:00:00+08:00", "max_age_hours": 2880, "required": true, "source_ref": "vendor-financials", "schema_version": "financials/v2"},
    "announcements": {"as_of": "2026-07-06T18:00:00+08:00", "max_age_hours": 72, "required": true, "source_ref": "vendor-announcements", "schema_version": "announcements/v2"},
    "market_funding": {"as_of": "2026-07-06T18:00:00+08:00", "max_age_hours": 72, "required": true, "source_ref": "vendor-market", "schema_version": "market/v2"},
    "global_risk": {"as_of": "2026-07-06T18:00:00+08:00", "max_age_hours": 72, "required": true, "source_ref": "vendor-global", "schema_version": "global/v2"}
  }
}
```

若真实授权并非 `true`，不得为了通过工具而修改字段；此时该数据包不能作为生产数据源。

## 构建与校验

```powershell
quant-build-bundle --input D:\incoming\vendor-batch --output D:\data\bundle-20260706
quant-validate-bundle --bundle D:\data\bundle-20260706
```

构建命令复制明确输入文件（输出省略时原地更新），生成每个数据文件的 SHA-256、metadata hash 和 manifest hash，并把 manifest 写入 `metadata.json`。校验命令不修改文件。任何文件被改动、必需字段缺失、授权或 PIT 未明确验证，校验均失败。

## 运行选择

```powershell
$env:QUANT_DATA_PROVIDER = "licensed-csv"
$env:QUANT_DATA_BUNDLE = "D:\data\bundle-20260706"
uvicorn apps.api.main:app
```

错误路径、无效 manifest 或未通过授权/PIT 的 bundle 会阻止 API/CLI 启动，不会回退到 Demo。通用 `csv` 与 Tushare/AKShare 行情仅用于原型研究，质量门禁禁止其发布生产建议。
