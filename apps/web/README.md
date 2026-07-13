# 衡策 Web

面向个人投资者的 A 股中期决策前端。默认请求 `/api/v1/dashboard`；接口不可用时自动回退到确定性演示数据，并在界面顶部明确标注。

```bash
npm install
npm run dev
npm test
npm run build
```

关键 API：

- `GET /api/v1/dashboard`：市场、组合、候选、题材、日志与净值快照。
- `POST /api/v1/backtests`：创建回测实验。

产品不连接券商，不执行真实交易。
