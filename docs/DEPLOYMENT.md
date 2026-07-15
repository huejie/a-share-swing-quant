# 当前部署记录

- 访问地址：`http://106.54.203.187`
- 服务器目录：`/opt/a-share-swing-quant`
- 容器：`a-share-swing-quant-api-1`、`a-share-swing-quant-web-1`
- 数据卷：`a-share-swing-quant_quant-data`
- 数据源：AKShare，30只显式观察池，最终组合3～5只
- 模型契约：`swing-rules-0.2.1`
- 自动交易：关闭；不连接券商

## 日常检查

```bash
cd /opt/a-share-swing-quant
docker compose ps
docker compose logs --tail=200 api web
curl http://127.0.0.1/api/v1/health/ready
systemctl status a-share-quant-eod.timer
systemctl list-timers a-share-quant-eod.timer
```

工作日18:35（Asia/Shanghai）由 `a-share-quant-eod.timer` 调用内部日终流水线。任务具备运行键幂等性；失败后5分钟重试。它只生成决策观察，不连接券商、不发送真实订单。

## 更新

服务器不保存 GitHub 写权限。由已授权开发机将已提交版本打包上传，然后在服务器验证并滚动重建：

```powershell
git archive --format=tar.gz -o $env:TEMP\a-share-swing-quant.tar.gz HEAD
scp -i $HOME\.ssh\a_share_quant_deploy $env:TEMP\a-share-swing-quant.tar.gz root@106.54.203.187:/tmp/
ssh -i $HOME\.ssh\a_share_quant_deploy root@106.54.203.187 "tar -xzf /tmp/a-share-swing-quant.tar.gz -C /opt/a-share-swing-quant && cd /opt/a-share-swing-quant && sh deploy/deploy.sh"
```

## 回滚

从 GitHub 检出目标提交，在开发机重新执行上述“打包上传—构建—启动”流程。SQLite 数据位于独立 Docker 卷，不会因代码回滚被覆盖。任何数据库回滚必须先按 `docs/RUNBOOK.md` 执行备份与隔离恢复验证。

## 当前边界

- AKShare/东方财富实时元数据和行情在机房 IP 上可能被限流；当前元数据使用仓库内显式清单，行情自动切到新浪前复权日线，来源均在 `/api/v1/data/status` 审计。
- 公开源非授权PIT数据，只发布 `observation_only`；缺少完整涨跌停/停牌约束，因此不生成或撮合模拟成交意图。
- 当前仅有 HTTP。配置域名、HTTPS 和访问认证前，不应视为正式公网生产发布。
- 策略有效性仍需至少8～12周连续前瞻观察，不能用当前工程验收替代收益验证。

