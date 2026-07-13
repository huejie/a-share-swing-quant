# 授权 CSV 数据包交付规范

`licensed-csv` 是本地、只读、哈希锁定的数据交付格式。它只接受供应商或数据负责人已经明确声明的授权与 PIT 证据；构建工具不会创建、猜测或修改授权结论。

## 必需文件

- `bars.csv`：至少包含 `symbol,date,open,high,low,close,volume,amount,industry,published_at,effective_at,available_at`。生产包建议同时提供 `is_st,is_delisting,regulatory_risk,audit_abnormal,event_risk,adj_factor,limit_up,limit_down,suspended,quality,catalyst`，用于硬风险过滤、公司行为和可成交约束。
- `securities.csv`：`symbol,name,listed_at,delisted_at,board`。退市记录必须保留。
- `theme_memberships.csv`：`symbol,theme,effective_from,effective_to,published_at,available_at`。
- `metadata.json`：授权、PIT 方法、批次及逐数据集新鲜度声明。
- 可选 `pit_records.csv`：财务/公告等事件，字段为 `dataset,entity_id,effective_at,published_at,available_at,payload_json,revision,source_ref`。

最小 metadata 示例：

```json
{
  "batch_id": "vendor-20260706-01",
  "provider": "contracted-vendor-name",
  "authorization": {
    "authorized": true,
    "scope": "internal-research-and-decision-support",
    "reference": "由数据负责人填写的合同或授权引用"
  },
  "pit": {
    "verified": true,
    "method": "由数据负责人填写的发布时间与历史成分核验方法"
  },
  "datasets": {
    "bars": {"as_of": "2026-07-06T18:00:00+08:00", "max_age_hours": 72, "required": true},
    "theme_memberships": {"as_of": "2026-07-06T18:00:00+08:00", "max_age_hours": 168, "required": true}
  },
  "market_inputs": {
    "global_risk_score": 50,
    "fund_flow_score": 50,
    "valuation_score": 50,
    "source": "由数据负责人填写的跨市场、资金与估值快照引用"
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
