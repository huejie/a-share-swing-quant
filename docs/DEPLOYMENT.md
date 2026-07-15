# 当前部署记录

- 访问地址：`http://106.54.203.187`
- 服务器目录：`/opt/a-share-swing-quant`
- 容器：`a-share-swing-quant-api-1`、`a-share-swing-quant-web-1`
- 数据卷：`a-share-swing-quant_quant-data`
- 数据源：AKShare，当前全A动态预选最多60只；端点降级时使用30只显式观察池，最终组合3～5只
- 模型契约：`swing-rules-0.3.0`
- 自动交易：关闭；不连接券商

## 日常检查

```bash
cd /opt/a-share-swing-quant
docker compose ps
docker compose logs --tail=200 api web
curl http://127.0.0.1/api/v1/health/ready
systemctl status a-share-quant-eod.timer
systemctl status a-share-quant-backup.timer
systemctl list-timers a-share-quant-eod.timer a-share-quant-backup.timer
```

工作日18:35（Asia/Shanghai）由 `a-share-quant-eod.timer` 调用内部日终流水线。服务器首次部署自动生成个人管理密钥，API 只从环境读取；日终任务使用权限为 `0600` 的 `/etc/a-share-quant/eod.curl.conf`，密钥不写入 unit 或进程参数。任务具备运行键幂等性；失败后5分钟重试。每天02:30由 `a-share-quant-backup.timer` 使用 SQLite 在线备份 API 写入宿主机 `/var/backups/a-share-quant`，执行完整性校验、SHA-256 manifest 和30天保留。两项任务都不连接券商、不发送真实订单。

## 更新

服务器不保存 GitHub 写权限。由已授权开发机将已提交版本打包上传，然后在服务器验证并滚动重建：

```powershell
git archive --format=tar.gz -o $env:TEMP\a-share-swing-quant.tar.gz HEAD
scp -i $HOME\.ssh\a_share_quant_deploy $env:TEMP\a-share-swing-quant.tar.gz root@106.54.203.187:/tmp/
$commit = git rev-parse HEAD
ssh -i $HOME\.ssh\a_share_quant_deploy root@106.54.203.187 "tar -xzf /tmp/a-share-swing-quant.tar.gz -C /opt/a-share-swing-quant && cd /opt/a-share-swing-quant && sh deploy/deploy.sh /opt/a-share-swing-quant $commit"
```

## 回滚

从 GitHub 检出目标提交，在开发机重新执行上述“打包上传—构建—启动”流程。SQLite 数据位于独立 Docker 卷，不会因代码回滚被覆盖。任何数据库回滚必须先按 `docs/RUNBOOK.md` 执行备份与隔离恢复验证。

## 当前边界

- AKShare/东方财富全A、实时元数据和行情在机房 IP 上可能被限流。正常路径先从当前全A快照做有界60只预选，再读取行业/上市日期和历史；全A或动态行业端点不可用时整体切回仓库内30只显式清单，行情端点失败时再切换新浪前复权日线。预选、降级原因和逐标的来源分别在 `/api/v1/data/status` 的 `universe_selection`、`security_metadata`、`price_history` 审计。
- 公开源非授权PIT数据，只发布 `observation_only`；缺少完整涨跌停/停牌约束，因此不生成或撮合模拟成交意图。
- 当前仅有 HTTP，因此网关默认 `QUANT_PUBLIC_ADMIN_ENABLED=false`：公网只读页面可访问，所有 POST/PATCH/DELETE 管理操作在网关即拒绝；服务器本机定时任务仍需个人管理密钥。不要在公网 HTTP 页面输入密钥。配置域名和 HTTPS 后仍需人工评审，才可显式开放远程管理。
- 策略有效性仍需至少8～12周连续前瞻观察，不能用当前工程验收替代收益验证。
