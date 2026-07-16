# 当前部署记录

- 公网访问地址：`http://106.54.203.187`（端口 80；8080 仅为容器内部端口，不对公网开放）
- 服务器目录：`/opt/a-share-swing-quant`
- 容器：`a-share-swing-quant-api-1`、`a-share-swing-quant-web-1`
- 数据卷：`a-share-swing-quant_quant-data`
- 数据源：AKShare，当前全A动态预选最多60只；端点降级时使用30只显式观察池，最终组合3～5只
- 仓库部署目标模型契约：`swing-rules-0.4.0`；每次发布后必须以 API 返回的 `model_version` 核对实际运行版本
- 自动交易：关闭；不连接券商

## 日常检查

```bash
cd /opt/a-share-swing-quant
docker compose ps
docker compose logs --tail=200 api web
curl http://127.0.0.1/api/v1/health/ready
systemctl status a-share-quant-eod.timer
systemctl status a-share-quant-backup.timer
systemctl status a-share-quant-restore-verify.timer
systemctl list-timers a-share-quant-eod.timer a-share-quant-backup.timer a-share-quant-restore-verify.timer
```

工作日18:35（Asia/Shanghai）由 `a-share-quant-eod.timer` 调用内部日终流水线。服务器首次部署自动生成个人管理密钥，API 只从环境读取；日终任务使用权限为 `0600` 的 `/etc/a-share-quant/eod.curl.conf`，密钥不写入 unit 或进程参数。任务具备运行键幂等性；失败后5分钟重试。每天02:30由 `a-share-quant-backup.timer` 使用 SQLite 在线备份 API 写入宿主机 `/var/backups/a-share-quant`，同时归档研究制品，执行完整性校验、SHA-256 manifest 和30天保留。每周日03:15由 `a-share-quant-restore-verify.timer` 在隔离临时目录验证最新备份、数据库完整性和研究制品哈希。上述任务都不连接券商、不发送真实订单。

## HTTP 阶段的安全管理

在域名与 HTTPS 尚未配置时，公网入口只读。需要从产品页面修改设置、运行回测/研究或重试通知时，在开发机建立 SSH 本地转发：

```powershell
ssh -N -L 18080:127.0.0.1:80 -i $HOME\.ssh\a_share_quant_deploy root@106.54.203.187
```

保持该终端运行，再访问 `http://localhost:18080`。浏览器处于 localhost 安全上下文，服务器网关看到的也是 SSH 回环连接；输入个人管理密钥后才允许写操作。密钥只从服务器 `/opt/a-share-swing-quant/.env` 读取，并仅保存在当前浏览器 `sessionStorage`，不得粘贴到公网 IP 的 HTTP 页面、聊天或日志。MobaXterm 可用相同的 Local port forwarding：本地端口 `18080`，远端主机 `127.0.0.1`，远端端口 `80`。

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
- AKShare 的公开接口、当前行业快照及跨市场代理不构成历史 PIT、商业授权或生产研究证据；即使页面可访问、日终任务成功，也不能据此判定生产数据门禁通过。
- 当前仅有 HTTP，因此网关默认 `QUANT_PUBLIC_ADMIN_ENABLED=false`：公网只读页面可访问，所有 POST/PATCH/DELETE 管理操作在网关即拒绝；服务器本机定时任务仍需个人管理密钥。不要在公网 HTTP 页面输入密钥。配置域名和 HTTPS 后仍需人工评审，才可显式开放远程管理。
- 策略有效性仍需至少8～12周连续前瞻观察，不能用当前工程验收替代收益验证。
