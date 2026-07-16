import { useEffect, useMemo, useState } from "react";
import {
  NavLink,
  Route,
  Routes,
  useLocation,
  useParams,
} from "react-router-dom";
import {
  getSettings,
  getDecision,
  getSnapshot,
  listBacktests,
  managementContextSecure,
  runBacktest,
  runResearch,
  saveSettings,
} from "./api";
import type { ApiEnvelope, DataProvenance, Holding, Snapshot } from "./types";

const nav = [
  ["/", "今日决策", "今"],
  ["/market", "市场温度", "温"],
  ["/themes", "题材雷达", "题"],
  ["/risk", "风险中心", "险"],
  ["/backtest", "回测实验", "测"],
  ["/simulation", "模拟组合", "模"],
  ["/logs", "决策日志", "录"],
  ["/settings", "设置", "设"],
] as const;
function Shell({
  env,
  children,
}: {
  env: ApiEnvelope<Snapshot>;
  children: React.ReactNode;
}) {
  const loc = useLocation();
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main">
        跳到主要内容
      </a>
      <aside className="side">
        <div className="brand">
          <span className="seal">衡</span>
          <div>
            <b>衡策</b>
            <small>中期决策账簿</small>
          </div>
        </div>
        <nav aria-label="主导航">
          {nav.map(([to, label, mark]) => (
            <NavLink key={to} to={to} end={to === "/"}>
              <span aria-hidden>{mark}</span>
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="side-foot">
          <span className="dot" /> 自动交易已关闭<small>研究与决策辅助</small>
        </div>
      </aside>
      <main id="main" tabIndex={-1}>
        <header className="topbar">
          <div>
            <small>
              {env.data.asOf.replace("T", " ").slice(0, 16)} ·{" "}
              {env.data.modelVersion}
            </small>
            <strong>
              {nav.find((x) => x[0] === loc.pathname)?.[1] ??
                (loc.pathname === "/more" ? "更多" : "个股案卷")}
            </strong>
          </div>
          <SourceBadge env={env} />
        </header>
        {children}
        <footer>
          研究与决策辅助，不构成收益承诺或自动交易指令。模型输出与真实持仓请分别核对。
        </footer>
      </main>
      <nav className="mobile-nav" aria-label="移动端主导航">
        {[...nav.slice(0, 4), ["/more", "更多", "···"] as const].map(
          ([to, label, mark]) => (
            <NavLink key={to} to={to} end={to === "/"}>
              <span>{mark}</span>
              <small>{label}</small>
            </NavLink>
          ),
        )}
      </nav>
    </div>
  );
}
function SourceBadge({ env }: { env: ApiEnvelope<Snapshot> }) {
  const label = env.stale
    ? "数据已过期"
    : {
        demo: "工程演示",
        "public-prototype": "公开接口原型",
        research: "研究数据待核验",
        verified: "已核验PIT数据",
      }[env.provenance.mode];
  return (
    <div
      className={`source ${env.source} ${env.stale ? "stale" : ""}`}
      role="status"
      aria-label={`数据状态：${label}，来源 ${env.provenance.provider}`}
    >
      <span>{label}</span>
      <small>
        {env.provenance.provider} · {env.data.asOf}
      </small>
    </div>
  );
}
function DataBoundary({ p }: { p: DataProvenance }) {
  if (p.mode === "verified") return null;
  const title =
    p.mode === "public-prototype"
      ? "公开接口：仅研究与模拟观察"
      : p.mode === "demo"
        ? "工程演示：仅验证流程"
        : "数据源待核验：仅研究与模拟观察";
  const universe = p.universeSelection;
  const notes = [
    ...(universe?.fallbackUsed
      ? [
          universe.warning ||
            `动态选股失败，使用 ${universe.selectedCount} 只配置观察池回退。`,
        ]
      : []),
    ...p.degradations,
  ];
  const preview = notes.slice(0, 2);
  return (
    <aside
      className="data-boundary"
      role="status"
      aria-label="数据来源与研究边界"
    >
      <div>
        <small>数据来源边界</small>
        <b>{title}</b>
      </div>
      <dl>
        <div>
          <dt>来源</dt>
          <dd>{p.provider}</dd>
        </div>
        <div>
          <dt>选股范围</dt>
          <dd>
            {universe?.fallbackUsed
              ? `${universe.selectedCount}只回退观察池`
              : universe
                ? `动态池 ${universe.selectedCount}只`
                : "未声明"}
          </dd>
        </div>
        <div>
          <dt>PIT</dt>
          <dd>{p.pitVerified ? "PIT 已声明" : "未验证 PIT"}</dd>
        </div>
        <div>
          <dt>发布资格</dt>
          <dd>{p.productionReady ? "可用性已声明" : "非生产数据"}</dd>
        </div>
      </dl>
      <p>
        {preview.length
          ? `主要降级：${preview.join("；")}`
          : "缺少可验证的数据质量说明。"}{" "}
        不构成生产建议、真实持仓或策略验收结论。
      </p>
      {notes.length > 2 && (
        <details>
          <summary>查看全部 {notes.length} 项数据说明</summary>
          <ul>
            {notes.map((x, i) => (
              <li key={`${i}-${x}`}>{x}</li>
            ))}
          </ul>
        </details>
      )}
    </aside>
  );
}
function PageHead({
  eyebrow,
  title,
  summary,
  action,
}: {
  eyebrow: string;
  title: string;
  summary: string;
  action?: React.ReactNode;
}) {
  return (
    <section className="page-head">
      <div>
        <span className="eyebrow">{eyebrow}</span>
        <h1>{title}</h1>
        <p>{summary}</p>
      </div>
      {action}
    </section>
  );
}
const fmt = (n: number) => `${n > 0 ? "+" : ""}${n.toFixed(2)}%`;
function HoldingRow({ h, index }: { h: Holding; index: number }) {
  const allocated = h.allocationAssigned;
  const protection = !allocated
    ? "不适用"
    : h.protectivePrice === null
      ? "未启动"
      : `¥${h.protectivePrice.toFixed(2)}`;
  return (
    <article
      className="holding"
      style={{ "--i": index } as React.CSSProperties}
    >
      <div className="holding-main">
        <span className={`action action-${h.action}`}>{h.action}</span>
        <div className="stock">
          <NavLink to={`/stocks/${h.code}`}>
            <b>{h.name}</b>
            <small>
              {h.code} · {h.theme}
            </small>
          </NavLink>
        </div>
        <div className="price">
          <b>¥{h.price.toFixed(2)}</b>
          {h.change !== 0 && (
            <small className={h.change >= 0 ? "up" : "down"}>
              {fmt(h.change)}
            </small>
          )}
        </div>
        <div className="weight">
          <small>仓位</small>
          <b>
            {allocated ? (
              <>
                {h.weight}% <i>/ 首次 {h.initialWeight}%</i>
              </>
            ) : (
              "未分配"
            )}
          </b>
        </div>
      </div>
      <div className="holding-decision">
        <div>
          <small>核心判断</small>
          <p>{h.thesis[0] || "等待证据摘要"}</p>
        </div>
        <div>
          <small>{allocated ? "失效条件" : "观察状态"}</small>
          <p>{h.invalidation}</p>
        </div>
        <div>
          <small>{allocated ? "保护价" : "仓位状态"}</small>
          <p className="tabular">{protection}</p>
        </div>
      </div>
      <p className="provenance">
        {h.kind === "simulation" ? "模拟持仓" : "模型输出"} · {h.version} ·{" "}
        {h.timestamp}
      </p>
      <details>
        <summary>展开全部证据与门禁</summary>
        <div className="evidence">
          <ul>
            {h.thesis.map((x) => (
              <li key={x}>{x}</li>
            ))}
          </ul>
          <dl>
            <div>
              <dt>信号状态</dt>
              <dd>{h.entryState}</dd>
            </div>
            <div>
              <dt>题材生命周期</dt>
              <dd>{h.themeLifecycle || "未返回"}</dd>
            </div>
            {h.gateFailures.length > 0 && (
              <div>
                <dt>未通过门禁</dt>
                <dd>{h.gateFailures.join("；")}</dd>
              </div>
            )}
            {allocated && (
              <>
                <div>
                  <dt>触发区间</dt>
                  <dd>{h.entry}</dd>
                </div>
                <div>
                  <dt>首次 / 目标</dt>
                  <dd>
                    {h.initialWeight}% / {h.weight}%
                  </dd>
                </div>
                <div>
                  <dt>初始止损</dt>
                  <dd>¥{h.initialStop.toFixed(2)}</dd>
                </div>
                <div>
                  <dt>保护机制</dt>
                  <dd>
                    {protection}
                    {h.protectivePrice === null
                      ? "（达到策略启动条件后生效）"
                      : "（只上移不下调）"}
                  </dd>
                </div>
                <div>
                  <dt>预计持有</dt>
                  <dd>
                    {h.expectedHoldingDays[0]}—{h.expectedHoldingDays[1]}{" "}
                    个交易日
                  </dd>
                </div>
              </>
            )}
            <div>
              <dt>下次复核</dt>
              <dd>{h.nextReviewAt.replace("T", " ").slice(0, 16)}</dd>
            </div>
            <div>
              <dt>关键风险</dt>
              <dd>{h.risk}</dd>
            </div>
          </dl>
        </div>
      </details>
    </article>
  );
}
function ExitActions({ s }: { s: Snapshot }) {
  if (!s.exitActions.length) return null;
  return (
    <section className="exit-actions" role="alert" aria-label="优先退出动作">
      <header>
        <div>
          <small>独立退出队列 / 不计入模型持仓</small>
          <h2>优先处理 {s.exitActions.length} 笔退出动作</h2>
        </div>
        <span>目标仓位 0%</span>
      </header>
      {s.exitActions.map((x) => (
        <article key={x.code}>
          <div>
            <b>{x.name}</b>
            <small>{x.code} · {x.theme} · 优先级 {x.priority}</small>
          </div>
          <p>{x.reason}</p>
          <strong>{x.currentWeight.toFixed(1)}% → 0%</strong>
        </article>
      ))}
    </section>
  );
}
function Dashboard({ s }: { s: Snapshot }) {
  const actions =
    s.holdings.filter((h) => h.action !== "持有").length +
    s.exitActions.length;
  const focus = s.holdings
    .slice(0, 2)
    .map((h) => `${h.name}${h.action}`)
    .join("，");
  const invested = 100 - s.cashWeight;
  return (
    <>
      <PageHead
        eyebrow={`今日行动 / ${s.asOf.replace("T", " ").slice(5, 16)}`}
        title={`明日模型组合 ${s.holdings.length} 只，现金 ${s.cashWeight.toFixed(1)}%`}
        summary={`${focus || s.portfolioReason || "当前无需调整"}。模型仓位 ${invested.toFixed(1)}%，未用满 ${s.market.exposure}% 的市场仓位上限。`}
        action={
          <button className="primary" onClick={() => window.print()}>
            打印行动单
          </button>
        }
      />
      <ExitActions s={s} />
      {s.portfolioStatus === "partial" && (
        <div className="data-notice" role="status">
          <b>组合未凑满</b>　
          {s.portfolioReason || "合格标的不足，剩余资金保留现金。"}
        </div>
      )}
      {s.portfolioStatus === "observation" && (
        <div className="data-notice" role="status">
          <b>前瞻模拟观察，不是生产建议</b>　
          {s.portfolioReason ||
            "公开来源仅用于记录模型前瞻表现，不作为真实交易或策略验收依据。"}
        </div>
      )}
      <section className="decision-strip" aria-label="今日决策摘要">
        <div>
          <small>市场状态</small>
          <b>{s.market.state}</b>
        </div>
        <div>
          <small>仓位上限</small>
          <b>{s.market.exposure}%</b>
        </div>
        <div>
          <small>模型标的</small>
          <b>{s.holdings.length} 只</b>
        </div>
        <div>
          <small>建议动作</small>
          <b>{actions} 笔</b>
        </div>
        <div>
          <small>实际留现</small>
          <b>{s.cashWeight.toFixed(1)}%</b>
        </div>
      </section>
      <DecisionSpine s={s} />
      <section className="section">
        <SectionTitle
          n="02"
          title="模型组合"
          note="模型输出，不是模拟成交或真实持仓 · 请逐项核对"
        />
        {s.holdings.length ? (
          <div className="holdings">
            {s.holdings.map((h, i) => (
              <HoldingRow key={h.code} h={h} index={i} />
            ))}
          </div>
        ) : (
          <div className="state" role="status">
            <b>
              {s.portfolioStatus === "risk_off"
                ? "风险关闭：保持现金"
                : "合格标的不足：不强行补足"}
            </b>
            <p>{s.portfolioReason}</p>
          </div>
        )}
      </section>
      <section className="section">
        <SectionTitle
          n="03"
          title="候选观察"
          note="观察名单未通过全部新开仓门禁，仅观察、未分配仓位"
        />
        {s.candidates.length ? (
          <div className="candidate-table">
            {s.candidates.map((h) => (
              <HoldingRow key={h.code} h={h} index={0} />
            ))}
          </div>
        ) : (
          <div className="state" role="status">
            <b>暂无备选股</b>
            <p>当前没有额外标的通过候选门槛，系统不会用低分股票补足。</p>
          </div>
        )}
      </section>
    </>
  );
}
function SectionTitle({
  n,
  title,
  note,
}: {
  n: string;
  title: string;
  note?: string;
}) {
  return (
    <div className="section-title">
      <span>{n}</span>
      <h2>{title}</h2>
      {note && <p>{note}</p>}
    </div>
  );
}
function DecisionSpine({ s }: { s: Snapshot }) {
  const themes =
    s.themes
      .filter((t) => ["启动", "扩散", "健康趋势"].includes(t.phase))
      .slice(0, 2)
      .map((t) => `${t.name}${t.phase}`)
      .join(" · ") || "暂无允许新开的题材";
  const steps = [
    ["市场", `${s.market.state} · 仓位上限${s.market.exposure}%`],
    ["题材", themes],
    ["个股", `${s.holdings.length}只通过评分、流动性与相关性约束`],
    ["风控", `单股不超过25% · 实际现金${s.cashWeight.toFixed(1)}%`],
  ];
  return (
    <section className="section">
      <SectionTitle
        n="01"
        title="决策链"
        note="市场只约束仓位；题材、个股和风险门槛共同决定动作"
      />
      <ol className="spine">
        {steps.map(([a, b], i) => (
          <li key={a}>
            <span>{i + 1}</span>
            <div>
              <small>{a}</small>
              <b>{b}</b>
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}
const qualityLabel = (x: string, available = 0, total = 0) =>
  x === "neutral_missing"
    ? "缺失，按中性 50 分降级"
    : x === "partial_multi_asset_public_proxy"
      ? `部分公开代理（${available}/${total} 项可用）`
      : x === "watchlist_turnover_breadth_proxy"
        ? "观察池成交额与广度代理"
        : x === "public_trend_proxy"
          ? "公开趋势代理"
          : x === "live" || x === "verified"
            ? "已采集"
            : x && x !== "unknown"
              ? `已声明：${x}`
              : "口径未声明";
function Market({ s, p }: { s: Snapshot; p: DataProvenance }) {
  const m = p.marketInputs;
  return (
    <>
      <PageHead
        eyebrow="市场状态 / 日频"
        title={`${s.market.score}° ${s.market.state}`}
        summary={s.market.summary}
      />
      <section className="market-ledger">
        <div className="thermo" aria-label={`市场温度${s.market.score}分`}>
          <span style={{ height: `${s.market.score}%` }} />
          <b>{s.market.score}</b>
          <small>100</small>
        </div>
        <div className="factor-list">
          {s.market.factors.map((f) => (
            <div key={f.name}>
              <div>
                <b>
                  {f.name} <small>权重 {f.weight}%</small>
                </b>
                <span>{f.score}</span>
              </div>
              <progress max="100" value={f.score}>
                {f.score}
              </progress>
              <p>{f.note}</p>
            </div>
          ))}
        </div>
      </section>
      <section className="note-sheet">
        <h2>全球情绪与资金口径</h2>
        <p>
          市场状态只约束仓位和风险资格，不直接选股。资金流使用成交结构、融资或
          ETF 等代理，不把推算值称为真实账户“主力流入”。
        </p>
        <dl>
          <div>
            <dt>全球风险</dt>
            <dd>
              {qualityLabel(
                m?.globalRiskQuality ?? "unknown",
                m?.globalComponentsAvailable,
                m?.globalComponentsTotal,
              )}
            </dd>
          </div>
          <div>
            <dt>全球代理</dt>
            <dd>{m?.globalRiskProxy || "未提供"}</dd>
          </div>
          <div>
            <dt>资金代理</dt>
            <dd>{qualityLabel(m?.fundFlowQuality ?? "unknown")}</dd>
          </div>
          <div>
            <dt>资金覆盖</dt>
            <dd>{m?.fundFlowProxy || "未提供"}</dd>
          </div>
          <div>
            <dt>估值输入</dt>
            <dd>{qualityLabel(m?.valuationQuality ?? "unknown")}</dd>
          </div>
          <div>
            <dt>当前口径</dt>
            <dd>{m?.source ?? "未提供"}</dd>
          </div>
        </dl>
      </section>
      <section className="note-sheet market-position">
        <h2>仓位解释</h2>
        <p>
          市场仓位上限 {s.market.exposure}%，当前模型实际仓位{" "}
          {(100 - s.cashWeight).toFixed(1)}%，保留 {s.cashWeight.toFixed(1)}%
          现金。候选不足时不会为了用满上限而降低门槛。
        </p>
        <dl>
          <div>
            <dt>主导风格</dt>
            <dd>{s.market.style}</dd>
          </div>
          <div>
            <dt>数据时点</dt>
            <dd>{s.asOf.replace("T", " ").slice(0, 16)}</dd>
          </div>
          <div>
            <dt>降档原则</dt>
            <dd>市场状态转弱时，组合风控覆盖个股评分</dd>
          </div>
        </dl>
      </section>
    </>
  );
}
function Themes({ s }: { s: Snapshot }) {
  return (
    <>
      <PageHead
        eyebrow="题材生命周期 / 周频"
        title="追踪扩散，不追逐喧嚣"
        summary="仅启动、扩散、健康趋势允许新开仓；催化与资金代理都不能单独触发买入。"
      />
      <div className="fund-flow-note" role="note">
        <b>资金口径</b>
        <span>
          “成交代理”来自成交结构得分，不等于真实账户流向或“主力净流入”。
        </span>
      </div>
      <div className="theme-head">
        <span>题材 / 阶段</span>
        <span>综合</span>
        <span>相对强度</span>
        <span>内部广度</span>
        <span>成交代理</span>
        <span>催化</span>
        <span>拥挤</span>
      </div>
      <section className="themes">
        {s.themes.map((t) => {
          const openable = ["启动", "扩散", "健康趋势"].includes(t.phase);
          return (
            <article key={t.name}>
              <div>
                <b>{t.name}</b>
                <span className={`phase phase-${t.phase}`}>{t.phase}</span>
                <span
                  className={`eligibility ${openable ? "eligible" : "ineligible"}`}
                >
                  {openable ? "✓ 允许新开" : "— 仅观察"}
                </span>
                <p>
                  {t.lifecycleReason} · {t.fundFlowLabel} · 基本面{" "}
                  {t.fundamental} · 龙头稳定 {t.leadership}
                </p>
              </div>
              {[
                ["综合", t.score],
                ["相对强度", t.relativeStrength],
                ["内部广度", t.breadth],
                ["成交代理", t.flow],
                ["催化", t.catalyst],
                ["拥挤", t.crowding],
              ].map(([a, v]) => (
                <div className="metric" key={a}>
                  <b>{v}</b>
                  <span style={{ width: `${v}%` }} />
                  <small>{a}</small>
                </div>
              ))}
            </article>
          );
        })}
      </section>
    </>
  );
}
function Stock({ s }: { s: Snapshot }) {
  const { id } = useParams();
  const h = [...s.holdings, ...s.candidates].find((x) => x.code === id);
  if (!h)
    return (
      <>
        <PageHead
          eyebrow="个股案卷"
          title="当前池中没有该标的"
          summary="它可能已被过滤、退出或尚未进入候选池。请从今日决策重新选择。"
        />
        <State kind="empty" />
      </>
    );
  const allocated = h.allocationAssigned;
  const protection =
    h.protectivePrice === null
      ? "尚未启动"
      : `¥${h.protectivePrice.toFixed(2)}`;
  return (
    <>
      <PageHead
        eyebrow={`${h.code} / 个股案卷`}
        title={`${h.name} · ${h.action}`}
        summary={`${h.theme}｜${h.entryState}｜评分 ${h.score.toFixed(1)}｜${allocated ? `目标仓位 ${h.weight}%` : "未分配仓位"}`}
      />
      <section className="stock-dossier">
        <div className="quote">
          <small>模型参考价</small>
          <b>¥{h.price.toFixed(2)}</b>
          {h.change !== 0 && (
            <span className={h.change >= 0 ? "up" : "down"}>
              {fmt(h.change)}
            </span>
          )}
          <div className="signal-ticket">
            <small>{allocated ? "买点状态" : "观察状态"}</small>
            <b>{h.entryState}</b>
            <p>{allocated ? h.entry : "仅观察，未分配触发区间"}</p>
            <span>
              {allocated
                ? `首次 ${h.initialWeight}% → 确认后目标 ${h.weight}%`
                : "未分配仓位，不构成买入动作"}
            </span>
          </div>
        </div>
        <div className="case">
          <h2>入选证据</h2>
          <ol>
            {h.thesis.map((x) => (
              <li key={x}>{x}</li>
            ))}
          </ol>
          <h2>{allocated ? "失效与卖点" : "观察门禁"}</h2>
          <p>{h.invalidation}</p>
          <dl>
            <div>
              <dt>题材生命周期</dt>
              <dd>{h.themeLifecycle || "未返回"}</dd>
            </div>
            {h.gateFailures.length > 0 && (
              <div>
                <dt>未通过门禁</dt>
                <dd>{h.gateFailures.join("；")}</dd>
              </div>
            )}
            {allocated && (
              <>
                <div>
                  <dt>初始止损</dt>
                  <dd>¥{h.initialStop.toFixed(2)}</dd>
                </div>
                <div>
                  <dt>移动保护</dt>
                  <dd>
                    {protection}
                    ；达到策略启动条件后只上移不下调，最多回吐最高浮盈30%
                  </dd>
                </div>
                <div>
                  <dt>预计持有</dt>
                  <dd>
                    {h.expectedHoldingDays[0]}—{h.expectedHoldingDays[1]}{" "}
                    个交易日
                  </dd>
                </div>
              </>
            )}
            <div>
              <dt>关键风险</dt>
              <dd>{h.risk}</dd>
            </div>
            <div>
              <dt>下次复核</dt>
              <dd>{h.nextReviewAt.replace("T", " ").slice(0, 16)}</dd>
            </div>
          </dl>
        </div>
      </section>
      <section className="audit-stamp">
        <span>审计戳</span>
        <p>
          {h.version} · {h.timestamp} · 模型输出（非模拟成交、非真实持仓）
        </p>
      </section>
    </>
  );
}
function Risk({ s }: { s: Snapshot }) {
  const groups = Object.entries(
    s.holdings.reduce<Record<string, number>>(
      (a, h) => ({ ...a, [h.theme]: (a[h.theme] ?? 0) + h.weight }),
      {},
    ),
  ).sort((a, b) => b[1] - a[1]);
  const top = groups[0] ?? ["无", 0];
  const dd = s.simulation.drawdown * 100;
  const hard = s.maxPortfolioDrawdown;
  const breached = hard !== null && Math.abs(Math.min(0, dd)) >= hard;
  const risks = [
    ["模型标的", `${s.holdings.length}只`, "3—5只为正常区间，不合格时允许更少"],
    [
      "模型实际仓位",
      `${(100 - s.cashWeight).toFixed(1)}%`,
      `市场上限 ${s.market.exposure}% · 现金 ${s.cashWeight.toFixed(1)}%`,
    ],
    ["最大题材集中", `${top[1].toFixed(1)}%`, `${top[0]}，产业链硬上限45%`],
    [
      "单股最高权重",
      `${Math.max(0, ...s.holdings.map((h) => h.weight)).toFixed(1)}%`,
      "硬上限25%",
    ],
    ["模拟组合回撤", fmt(dd), "只反映模拟净值，不代表真实账户"],
    [
      "组合硬门槛",
      hard === null ? "未读取" : `-${hard.toFixed(1)}%`,
      hard === null
        ? "服务未返回当前设置"
        : breached
          ? "模拟回撤已触及硬门槛"
          : "模拟回撤尚未触及硬门槛",
    ],
  ];
  return (
    <>
      <PageHead
        eyebrow="组合生存控制"
        title="风险先于收益"
        summary="当前前端只展示服务实际实现的一条组合硬回撤门禁，不推断中间动作。"
      />
      <section className={`risk-hard-gate ${breached ? "breached" : ""}`}>
        <div>
          <small>模拟组合当前回撤</small>
          <b>{fmt(dd)}</b>
        </div>
        <span aria-hidden>→</span>
        <div>
          <small>服务当前硬门槛</small>
          <b>{hard === null ? "未读取" : `-${hard.toFixed(1)}%`}</b>
        </div>
        <p>
          {hard === null
            ? "请检查只读设置接口；页面不会使用硬编码值代替。"
            : breached
              ? "硬门槛已触及，按服务规则停止新增风险。"
              : "尚未触及硬门槛；页面不推断服务未实现的中间动作。"}
        </p>
      </section>
      <section className="risk-ledger">
        {risks.map(([a, b, c]) => (
          <div key={a}>
            <small>{a}</small>
            <b>{b}</b>
            <p>{c}</p>
          </div>
        ))}
      </section>
      <section className="note-sheet">
        <h2>利润回吐与组合回撤不要混淆</h2>
        <p>
          “利润最多回吐30%”是单股移动保护参数。组合最大回撤是独立的硬门禁，以服务当前设置为准。
        </p>
      </section>
      <section className="note-sheet market-position">
        <h2>压力情景</h2>
        <p>
          止损无法消除停牌、跌停与隔夜跳空风险。真实账户未接入，最终成交和真实回撤必须由用户自行核对。
        </p>
      </section>
    </>
  );
}
function ResearchCollection({
  title,
  items,
}: {
  title: string;
  items: unknown[];
}) {
  return (
    <section className="research-block">
      <h3>{title} <small>{items.length} 项</small></h3>
      {items.length ? (
        <div className="research-items">
          {items.map((item, index) => (
            <pre key={index}>{compactJson(item)}</pre>
          ))}
        </div>
      ) : (
        <p className="evidence-state">未生成；对应门禁不能视为通过。</p>
      )}
    </section>
  );
}

function ResearchReport({ result }: { result: Record<string, any> }) {
  const report = result.report ?? {};
  const overall = String(result.overall ?? "UNKNOWN").toUpperCase();
  const gates = Array.isArray(result.gates) ? result.gates : [];
  const ablation = Array.isArray(report.ablation?.items)
    ? report.ablation.items
    : [];
  return (
    <section className={`research-report status-${overall.toLowerCase()}`} aria-label="完整研究包结果">
      <header>
        <div><small>完整研究包 / {result.id ?? "无运行ID"}</small><h2>Overall {overall}</h2></div>
        <b>{result.candidate_label ?? "候选标签未提供"}</b>
      </header>
      {overall === "FAIL" && <div className="research-fail" role="alert"><b>研究门禁失败</b>　该结果不能包装为策略通过或生产可用。</div>}
      <section className="gate-grid"><h3>Overall gates</h3>{gates.length?gates.map((gate:Record<string,any>,index:number)=><article className={String(gate.status??(gate.passed?"PASS":"FAIL")).toLowerCase()==="pass"?"pass":"fail"} key={gate.id??index}><b>{gate.id??gate.name??`Gate ${index+1}`}</b><span>{gate.status??(gate.passed?"PASS":"FAIL")}</span><p>{gate.reason??gate.detail??gate.message??compactJson(gate)}</p></article>):<p className="evidence-state">没有 gate 明细，不能判定通过。</p>}</section>
      <ResearchCollection title="三项基线" items={Array.isArray(report.baselines)?report.baselines:[]} />
      <ResearchCollection title="四档资金容量" items={Array.isArray(report.capacity)?report.capacity:[]} />
      <ResearchCollection title="参数敏感性" items={Array.isArray(report.sensitivity)?report.sensitivity:[]} />
      <ResearchCollection title="因子消融" items={ablation} />
      <ResearchCollection title="压力情景" items={Array.isArray(report.stress)?report.stress:[]} />
      <section className="research-block"><h3>归因与绩效摘要</h3><div className="research-items"><pre>{compactJson(report.contribution_attribution)}</pre><pre>{compactJson(report.performance_metrics??report.strategy)}</pre></div></section>
    </section>
  );
}

function Backtest({ s }: { s: Snapshot }) {
  const writeAllowed = managementContextSecure();
  const capital = s.capital;
  const [status, setStatus] = useState<"idle" | "running" | "done" | "error">(
    "idle",
  );
  const [result, setResult] = useState<Record<string, any> | null>(null);
  const [historyState, setHistoryState] = useState<"loading"|"done"|"error">("loading");
  const [history, setHistory] = useState<Record<string,any>[]>([]);
  const [researchState, setResearchState] = useState<"idle"|"running"|"done"|"error">("idle");
  const [researchResult, setResearchResult] = useState<Record<string,any>|null>(null);
  const loadHistory = async (signal?:AbortSignal) => {
    try {
      const body = await listBacktests(signal);
      setHistory(Array.isArray(body.items)?body.items:[]);
      setHistoryState("done");
    } catch (error:any) {
      if (error?.name!=="AbortError") setHistoryState("error");
    }
  };
  useEffect(()=>{const controller=new AbortController();loadHistory(controller.signal);return()=>controller.abort()},[]);
  async function run() {
    if (capital === null) return;
    setStatus("running");
    try {
      const body = await runBacktest(capital);
      setResult(body.result ?? null);
      setStatus("done");
      loadHistory();
    } catch {
      setStatus("error");
    }
  }
  async function runFullResearch(){
    if(capital===null)return;
    setResearchState("running");
    try{const body=await runResearch(capital);setResearchResult(body);setResearchState("done")}
    catch{setResearchState("error")}
  }
  const curve =
    result?.equity_curve?.map(
      (x: Record<string, number>) => x.equity / 10000,
    ) ?? s.equity;
  const stats = result
    ? [
        ["年化收益", fmt(result.annualized_return * 100)],
        ["最大回撤", fmt(result.max_drawdown * 100)],
        ["夏普比率", String(result.sharpe)],
        ["成交笔数", String(result.fills?.length ?? 0)],
        ["总收益", fmt(result.total_return * 100)],
        ["期末权益", `¥${Math.round(result.final_equity).toLocaleString()}`],
      ]
    : [
        ["状态", "尚未运行"],
        ["成交规则", "次日开盘"],
        ["费用", "佣金＋印花税"],
        ["滑点", "8 bps"],
        ["成交单位", "100股"],
        ["自动交易", "关闭"],
      ];
  return (
    <>
      <PageHead
        eyebrow="事件驱动 / 可重复实验"
        title="先证伪，再相信"
        summary="单次基准回放不等于研究通过；样本外、基线比较、敏感性和真实观察期必须分别验收。"
        action={
          <button
            className="primary"
            onClick={run}
            disabled={status === "running" || !writeAllowed || capital === null}
          >
            {capital === null
              ? "账户资金未读取"
              : !writeAllowed
              ? "需要 HTTPS 才能运行"
              : status === "running"
                ? "正在重放历史…"
                : "运行基准回测"}
          </button>
        }
      />
      <p className="backtest-capital">本次资金参数：<b>{capital===null?"设置未返回":moneyFmt(capital)}</b>，直接读取当前 settings，不使用固定 100 万。</p>
      {!writeAllowed && (
        <div className="data-notice stale" role="status">
          <b>管理操作已关闭</b>　当前是非安全 HTTP 页面。公开只读数据仍可查看；启动回测需要 HTTPS 或 localhost。
        </div>
      )}
      {status === "error" && <State kind="error" />}
      <section className="performance">
        <div className="chart">
          <svg viewBox="0 0 600 220" role="img">
            <title>事件驱动回测净值曲线</title>
            <polyline
              points={curve
                .map(
                  (v: number, i: number) =>
                    `${curve.length < 2 ? 0 : (i * 600) / (curve.length - 1)},${210 - (v - Math.min(...curve)) * 10}`,
                )
                .join(" ")}
            />
            <line x1="0" y1="190" x2="600" y2="190" />
          </svg>
          <p>
            文字摘要：信号在收盘后生成，下一交易日开盘按滑点、费用、停牌、涨跌停和容量约束模拟成交。
          </p>
        </div>
        <div className="stats">
          {stats.map(([a, b]) => (
            <div key={a}>
              <small>{a}</small>
              <b>{b}</b>
            </div>
          ))}
        </div>
      </section>
      {status === "done" && (
        <div className="result" role="status">
          基准回放已完成。它只验证成交链路；研究门禁仍不能据此标记通过。
        </div>
      )}
      {result && <section className={`backtest-audit ${String(result.assumptions?.research_gate_status??"").toLowerCase()==="fail"?"fail":""}`}><h2>成交台账与假设</h2>{String(result.assumptions?.research_gate_status??"").toUpperCase()==="FAIL"&&<div className="research-fail" role="alert"><b>Research gate FAIL</b>　基础回测只验证撮合链路，不能标记研究通过。</div>}<div className="audit-summary"><div><small>Assumptions</small><pre>{compactJson(result.assumptions)}</pre></div><div><small>Order ledger · {result.order_ledger?.length??0} 条</small><pre>{compactJson(result.order_ledger??[])}</pre></div></div></section>}
      <section className="backtest-history"><h2>历史基础回测</h2>{historyState==="loading"&&<p className="evidence-state" role="status">正在读取历史回测…</p>}{historyState==="error"&&<p className="evidence-state error" role="alert">历史回测读取失败，当前新结果不受影响。</p>}{historyState==="done"&&!history.length&&<p className="evidence-state" role="status">尚无历史基础回测。</p>}{history.map((item,index)=><details key={item.id??index}><summary>{item.id??`历史运行 ${index+1}`} · {item.status??"状态未提供"} · {moneyFmt(item.initial_capital??0)}</summary><div className="audit-summary"><div><small>绩效摘要</small><pre>{compactJson({total_return:item.total_return,max_drawdown:item.max_drawdown,sharpe:item.sharpe,final_equity:item.final_equity})}</pre></div><div><small>研究状态 / 假设</small><pre>{compactJson(item.assumptions)}</pre></div></div></details>)}</section>
      <section className="full-research"><div><span className="eyebrow">完整研究包</span><h2>基线、容量、敏感性与压力一次验收</h2><p>运行结果按原始 gate 展示；任何 FAIL 都保持失败，不降格成“基本通过”。</p></div><button className="primary" onClick={runFullResearch} disabled={!writeAllowed||capital===null||researchState==="running"}>{!writeAllowed?"需要 HTTPS 才能运行":capital===null?"账户资金未读取":researchState==="running"?"正在运行完整研究包…":"运行完整研究包"}</button></section>
      {researchState==="error"&&<p className="evidence-state error" role="alert">完整研究包运行失败；未生成的门禁不能视为通过。</p>}
      {researchState==="done"&&researchResult&&<ResearchReport result={researchResult}/>}
      <section className="experiment-table">
        <h2>研究门禁</h2>
        {[
          ["历史时点", "PIT 重建", "待授权数据", "公开源不可证明"],
          [
            "基线比较",
            "沪深300 / 中证全指 / 动量",
            "待完整研究包",
            "单次回放不替代",
          ],
          [
            "容量测试",
            "10万 / 100万 / 300万 / 1000万",
            "待完整研究包",
            "需分别计算费用与成交限制",
          ],
          [
            "最大回撤",
            s.maxPortfolioDrawdown === null
              ? "硬门槛未读取"
              : `服务硬门槛≤${s.maxPortfolioDrawdown.toFixed(1)}%`,
            "待样本外验证",
            "以服务当前设置为准",
          ],
          ["观察期", "连续8—12周", "进行中", "时间门槛不能由测试替代"],
        ].map((x) => (
          <div key={x[0]}>
            {x.map((v, i) => (
              <span key={v} data-label={["项目", "规则", "状态", "说明"][i]}>
                {v}
              </span>
            ))}
          </div>
        ))}
      </section>
    </>
  );
}
const moneyFmt = (n: number) => `¥${Math.round(n).toLocaleString("zh-CN")}`;
function Simulation({ s }: { s: Snapshot }) {
  const sim = s.simulation;
  const chart = sim.dailyEquity;
  const min = chart.length ? Math.min(...chart.map((x) => x.equity)) : 0;
  const max = chart.length ? Math.max(...chart.map((x) => x.equity)) : 1;
  const span = Math.max(1, max - min);
  return (
    <>
      <PageHead
        eyebrow="模拟组合 / 非真实资产"
        title={`模拟净值 ${moneyFmt(sim.equity)}`}
        summary={`模拟成交持仓 ${sim.positions.length} 只｜模拟现金 ${moneyFmt(sim.cash)}｜当前回撤 ${fmt(sim.drawdown * 100)}`}
      />
      <div className="simulation-banner">
        <b>与模型输出分开记账</b>　下方只显示已撮合的模拟持仓和台账，不把今日{" "}
        {s.holdings.length} 只模型标的冒充成交。不会连接券商。
      </div>
      {!sim.matchingReady && (
        <div className="data-notice stale" role="alert">
          <b>模拟撮合已关闭</b>　
          {sim.matchingReason || "当前数据不足以可靠判断停牌与涨跌停约束。"}
        </div>
      )}
      <section className="simulation-summary" aria-label="模拟账户摘要">
        <div>
          <small>模拟权益</small>
          <b>{moneyFmt(sim.equity)}</b>
        </div>
        <div>
          <small>模拟持仓市值</small>
          <b>{moneyFmt(sim.marketValue)}</b>
        </div>
        <div>
          <small>模拟现金</small>
          <b>{moneyFmt(sim.cash)}</b>
        </div>
        <div>
          <small>组合回撤</small>
          <b>{fmt(sim.drawdown * 100)}</b>
        </div>
      </section>
      {chart.length > 1 && (
        <section className="chart simulation-chart">
          <svg viewBox="0 0 600 180" role="img">
            <title>模拟组合每日权益曲线</title>
            <polyline
              points={chart
                .map(
                  (x, i) =>
                    `${(i * 600) / (chart.length - 1)},${170 - ((x.equity - min) / span) * 150}`,
                )
                .join(" ")}
            />
          </svg>
          <p>
            文字摘要：共记录 {chart.length} 个交易日；最新权益{" "}
            {moneyFmt(chart.at(-1)?.equity ?? 0)}，最新回撤{" "}
            {fmt((chart.at(-1)?.drawdown ?? 0) * 100)}。
          </p>
        </section>
      )}
      <section className="section">
        <SectionTitle
          n="持仓"
          title="已撮合的模拟持仓"
          note="模拟持仓，不是模型建议，也不是用户真实资产"
        />
        {sim.positions.length ? (
          <div className="simulation-ledger">
            {sim.positions.map((x) => (
              <div key={x.symbol}>
                <b>{x.symbol}</b>
                <span>{x.shares.toLocaleString()} 股</span>
                <span>平均成本 ¥{x.avgCost.toFixed(2)}</span>
                <small>{x.updatedAt.replace("T", " ").slice(0, 16)}</small>
              </div>
            ))}
          </div>
        ) : (
          <div className="state" role="status">
            <b>目前没有模拟成交持仓</b>
            <p>
              {sim.matchingReady
                ? "模型意图尚未在下一可成交时点完成撮合。"
                : "公开数据约束不完整，因此没有生成或撮合模拟指令。"}
            </p>
          </div>
        )}
      </section>
      <section className="section">
        <SectionTitle
          n="台账"
          title="最近模拟成交记录"
          note="记录意图、成交/部分成交/拒绝、费用与原因"
        />
        {sim.ledger.length ? (
          <div className="simulation-ledger">
            {sim.ledger.map((x) => (
              <div key={x.id}>
                <b>
                  {x.symbol} · {x.modelAction} · {x.side}
                </b>
                <span>
                  {x.status} · {x.stage} · {x.quantity} 股
                </span>
                <span>
                  {x.price === null ? "未成交" : `¥${x.price.toFixed(2)}`} ·
                  费用 ¥{x.fee.toFixed(2)}
                </span>
                <span>
                  实际权重 {fmt(x.currentWeight)}% → 执行目标 {fmt(x.executionTargetWeight)}%
                </span>
                <small>
                  {x.createdAt.replace("T", " ").slice(0, 19)}
                  {x.reason ? ` · ${x.reason}` : ""}
                </small>
              </div>
            ))}
          </div>
        ) : (
          <div className="state">
            <b>暂无模拟成交记录</b>
            <p>日终运行会在数据满足撮合条件时追加记录，历史不会由页面修改。</p>
          </div>
        )}
      </section>
    </>
  );
}
const compactJson = (value: unknown) =>
  value === undefined || value === null
    ? "未提供"
    : typeof value === "string"
      ? value
      : JSON.stringify(value, null, 2);

function DecisionEvidence({ id }: { id: string }) {
  const [open, setOpen] = useState(false);
  const [state, setState] = useState<"idle" | "loading" | "done" | "error">(
    "idle",
  );
  const [detail, setDetail] = useState<Record<string, any> | null>(null);
  useEffect(() => {
    if (!open || state !== "idle") return;
    const controller = new AbortController();
    setState("loading");
    getDecision(id, controller.signal)
      .then((body) => {
        setDetail(body);
        setState("done");
      })
      .catch((error) => {
        if (error?.name !== "AbortError") setState("error");
      });
    return () => controller.abort();
  }, [id, open, state]);
  const snapshot = detail?.snapshot ?? detail ?? {};
  const candidates = Array.isArray(snapshot.candidate_audit)
    ? snapshot.candidate_audit
    : [];
  const themes = Array.isArray(snapshot.selected_themes)
    ? snapshot.selected_themes
    : [];
  const portfolio = Array.isArray(snapshot.portfolio) ? snapshot.portfolio : [];
  const exits = Array.isArray(snapshot.exit_actions) ? snapshot.exit_actions : [];
  const simulation = snapshot.simulation ?? null;
  return (
    <div className="decision-evidence">
      <button className="evidence-toggle" onClick={() => setOpen((x) => !x)}>
        {open ? "收起当时证据" : "查看当时证据"}
      </button>
      {open && state === "loading" && (
        <p className="evidence-state" role="status">
          正在读取不可改写的决策快照…
        </p>
      )}
      {open && state === "error" && (
        <p className="evidence-state error" role="alert">
          当时证据读取失败；日志摘要仍可查看，请稍后重试。
        </p>
      )}
      {open && state === "done" && !detail && (
        <p className="evidence-state" role="status">
          该决策没有可用的证据快照。
        </p>
      )}
      {open && state === "done" && detail && (
        <section className="audit-evidence" aria-label={`决策 ${id} 的当时证据`}>
          <h3>版本与快照指纹</h3>
          <dl>
            <div><dt>模型</dt><dd>{detail.model_version ?? snapshot.model_version ?? "未提供"}</dd></div>
            <div><dt>配置版本 / hash</dt><dd>{detail.config_version ?? snapshot.config_version ?? "—"} / {detail.config_hash ?? snapshot.config_hash ?? "未提供"}</dd></div>
            <div><dt>数据 snapshot hash</dt><dd>{detail.data_snapshot_hash ?? snapshot.data_snapshot_hash ?? "未提供"}</dd></div>
            <div><dt>来源 / 发布模式</dt><dd>{detail.provider ?? snapshot.provider ?? "未提供"} / {detail.release_mode ?? snapshot.release_mode ?? "未提供"}</dd></div>
            <div><dt>质量</dt><dd><pre>{compactJson(snapshot.quality ?? detail.quality)}</pre></dd></div>
            <div><dt>input_snapshot</dt><dd>{detail.input_snapshot?.available ? `可用 · ${detail.input_snapshot.compressed_bytes ?? "?"} bytes` : "不可用或未保存"}</dd></div>
          </dl>
          <h3>当时选中题材</h3>
          {themes.length ? <ul>{themes.map((theme:Record<string,any>,index:number)=><li key={theme.name??index}><b>{theme.name??"未命名题材"}</b> · {theme.lifecycle??theme.phase??"阶段未提供"} · 得分 {theme.score??"—"}</li>)}</ul> : <p className="evidence-state">没有 selected_themes 记录。</p>}
          <h3>完整候选审计</h3>
          {candidates.length ? <div className="candidate-audit">{candidates.map((candidate:Record<string,any>,index:number)=><details key={candidate.symbol??index}><summary>{candidate.name??candidate.symbol??`候选 ${index+1}`} · {candidate.eligible?"通过基础资格":"被过滤"} · {candidate.score??"—"}分</summary><p>{(candidate.reasons??[]).join("；")||candidate.filter_reason||"未提供附加原因"}</p><dl>{Object.entries(candidate.gate_results??{}).map(([name,gate])=><div key={name}><dt>{name}</dt><dd><pre>{compactJson(gate)}</pre></dd></div>)}</dl></details>)}</div> : <p className="evidence-state">该历史记录没有 candidate_audit。</p>}
          <h3>最终结果摘要</h3>
          <div className="audit-summary"><div><small>最终组合</small><b>{portfolio.length} 只</b><pre>{compactJson(portfolio.map((x:Record<string,any>)=>({symbol:x.symbol,action:x.action,target_weight:x.target_weight})))}</pre></div><div><small>退出动作</small><b>{exits.length} 笔</b><pre>{compactJson(exits.map((x:Record<string,any>)=>({symbol:x.symbol,priority:x.priority,reason:x.exit_reason})))}</pre></div><div><small>模拟摘要</small><b>{simulation?"已记录":"未记录"}</b><pre>{compactJson(simulation)}</pre></div></div>
        </section>
      )}
    </div>
  );
}

function Logs({ s }: { s: Snapshot }) {
  const [q, setQ] = useState("");
  const items = s.decisions.filter((x) => (x.title + x.reason).includes(q));
  return (
    <>
      <PageHead
        eyebrow="不可改写的历史"
        title="决策日志"
        summary="保存当时可见的数据、模型版本与后续结果，避免事后解释。"
      />
      <label className="search">
        筛选日志
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="股票、题材或原因"
        />
      </label>
      <ol className="timeline">
        {items.map((d) => (
          <li key={d.id}>
            <time>{d.date}</time>
            <div>
              <span>
                {d.id} · {d.version}
              </span>
              <h2>{d.title}</h2>
              <p>{d.reason}</p>
              <b>{d.result}</b>
              <DecisionEvidence id={d.id} />
            </div>
          </li>
        ))}
      </ol>
      {!items.length && <State kind="empty" />}
    </>
  );
}
function Settings() {
  const writeAllowed = managementContextSecure();
  const [state, setState] = useState<
    "loading" | "idle" | "saving" | "saved" | "error"
  >("loading");
  const [values, setValues] = useState({
    capital: 1000000,
    target_count: 4,
    drawdown: 18,
    max_adv_participation: 2,
    boards: "all",
    notify_eod_success: true,
    notify_risk: true,
    notification_channel: "none",
  });
  useEffect(() => {
    const c = new AbortController();
    getSettings(c.signal)
      .then((x) => {
        setValues({
          capital: Number(x.capital ?? 1000000),
          target_count: Number(x.target_count ?? 4),
          drawdown: Number((x.max_portfolio_drawdown ?? 0.18) * 100),
          max_adv_participation: Number((x.max_adv_participation ?? 0.02) * 100),
          boards: x.include_chinext && x.include_star ? "all" : "main",
          notify_eod_success: x.notify_eod_success !== false,
          notify_risk: x.notify_risk !== false,
          notification_channel: String(x.notification_channel ?? "none"),
        });
        setState("idle");
      })
      .catch((e) => {
        if (e?.name !== "AbortError") setState("error");
      });
    return () => c.abort();
  }, []);
  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setState("saving");
    const f = new FormData(e.currentTarget);
    const boards = f.get("boards");
    const adminKey = String(f.get("admin_key") ?? "").trim();
    if (adminKey) sessionStorage.setItem("quant_admin_api_key", adminKey);
    else sessionStorage.removeItem("quant_admin_api_key");
    try {
      await saveSettings({
        capital: Number(f.get("capital")),
        target_count: Number(f.get("target_count")),
        max_portfolio_drawdown: Number(f.get("drawdown")) / 100,
        max_adv_participation: Number(f.get("max_adv_participation")) / 100,
        include_main: true,
        include_chinext: boards === "all",
        include_star: boards === "all",
        include_bse: false,
        notify_eod_success: f.get("notify_eod_success") === "on",
        notify_risk: f.get("notify_risk") === "on",
        notification_channel: String(f.get("notification_channel") ?? "none"),
      });
      setState("saved");
    } catch {
      setState("error");
    }
  }
  return (
    <>
      <PageHead
        eyebrow="账户与边界"
        title="设置"
        summary="先读取服务当前配置再编辑；保存只影响后续模型与风险约束，不会连接券商。"
      />
      {state === "loading" && (
        <div className="data-notice" role="status">
          <b>正在读取服务配置</b>　暂以安全默认值展示，读取完成前不可保存。
        </div>
      )}
      {!writeAllowed && (
        <div className="data-notice stale" role="status">
          <b>只读模式</b>　当前是非安全 HTTP 页面。可读取公开设置，但输入管理密钥和保存修改需要 HTTPS 或 localhost。
        </div>
      )}
      <form
        className="settings"
        onSubmit={submit}
        key={`${values.capital}-${values.target_count}-${values.boards}-${values.max_adv_participation}-${values.notify_eod_success}-${values.notify_risk}-${values.notification_channel}`}
      >
        <fieldset>
          <legend>账户容量</legend>
          <label>
            模拟资金
            <input
              name="capital"
              type="number"
              defaultValue={values.capital}
              min="100000"
              max="10000000"
              step="10000"
            />
          </label>
          <label>
            精选数量
            <select
              name="target_count"
              defaultValue={String(values.target_count)}
            >
              <option value="3">3只</option>
              <option value="4">4只（默认）</option>
              <option value="5">5只</option>
            </select>
          </label>
          <label>
            允许板块
            <select name="boards" defaultValue={values.boards}>
              <option value="all">主板、创业板、科创板</option>
              <option value="main">仅主板</option>
            </select>
          </label>
        </fieldset>
        <fieldset>
          <legend>风险边界</legend>
          <label>
            组合硬回撤上限
            <input
              name="drawdown"
              type="number"
              defaultValue={values.drawdown}
              min="8"
              max="18"
            />
            %
          </label>
          <label className="check">
            <input type="checkbox" defaultChecked disabled />
            数据过期时停止生成新建议
          </label>
          <p className="field-help">
            单股浮盈回吐30%由移动保护规则控制，与组合最大回撤不是同一参数。
          </p>
        </fieldset>
        <fieldset>
          <legend>流动性与容量</legend>
          <label>
            单股计划金额占20日均成交额上限
            <input
              name="max_adv_participation"
              type="number"
              defaultValue={values.max_adv_participation}
              min="1"
              max="2"
              step="0.1"
            />
            %
          </label>
          <p className="field-help">
            该比例同时进入候选容量门禁、组合仓位和回测配置；1000万元账户不会绕过此限制。
          </p>
        </fieldset>
        <fieldset>
          <legend>提醒偏好</legend>
          <label className="check">
            <input
              name="notify_eod_success"
              type="checkbox"
              defaultChecked={values.notify_eod_success}
            />
            日终任务成功后提醒
          </label>
          <label className="check">
            <input
              name="notify_risk"
              type="checkbox"
              defaultChecked={values.notify_risk}
            />
            数据门禁、组合风控或退出动作出现时提醒
          </label>
          <label>
            外部通知渠道
            <select
              name="notification_channel"
              defaultValue={values.notification_channel}
            >
              <option value="none">不发送外部通知</option>
              <option value="email">邮件</option>
              <option value="webhook">Webhook</option>
            </select>
          </label>
          <p className="field-help">
            站内风险状态始终展示。邮件地址、SMTP 凭据或 Webhook 地址只在服务器环境变量中配置，不会保存在浏览器、设置记录或决策日志中；未配置目标时会明确记录投递失败。
          </p>
        </fieldset>
        <fieldset>
          <legend>管理权限</legend>
          <label>
            管理密钥
            <input
              name="admin_key"
              type="password"
              disabled={!writeAllowed}
              autoComplete="off"
              defaultValue={sessionStorage.getItem("quant_admin_api_key") ?? ""}
              placeholder="仅保存到本次浏览器会话"
            />
          </label>
          <p className="field-help">
            密钥只保存在 sessionStorage，关闭浏览器会话后清除；不会写入
            localStorage 或前端包。它仅用于保存设置和启动研究任务。
          </p>
        </fieldset>
        <fieldset>
          <legend>执行边界</legend>
          <p>自动交易永久关闭。管理密钥不是券商密钥，系统不会生成真实订单。</p>
        </fieldset>
        <button
          className="primary"
          type="submit"
          disabled={!writeAllowed || state === "saving" || state === "loading"}
          aria-disabled={!writeAllowed}
        >
          {!writeAllowed
            ? "需要 HTTPS 才能保存"
            : state === "saving"
              ? "正在保存…"
              : "保存设置"}
        </button>
        {state === "saved" && (
          <span className="saved" role="status">
            设置已保存到决策服务。
          </span>
        )}
        {state === "error" && (
          <span className="saved" role="alert">
            未能读取或保存服务设置；当前显示安全默认值，请检查管理密钥或服务状态。
          </span>
        )}
      </form>
    </>
  );
}
function More() {
  return (
    <>
      <PageHead
        eyebrow="全部功能"
        title="研究工作台"
        summary="移动端保留所有页面入口；行动相关页面仍排在最前。"
      />
      <nav className="more-links" aria-label="更多功能">
        {nav.slice(4).map(([to, label, mark]) => (
          <NavLink to={to} key={to}>
            <span>{mark}</span>
            <div>
              <b>{label}</b>
              <small>
                {label === "回测实验"
                  ? "验证模型与成交假设"
                  : label === "模拟组合"
                    ? "查看非真实资产的模拟成交"
                    : label === "决策日志"
                      ? "追溯当时证据与模型版本"
                      : "调整账户容量与风险边界"}
              </small>
            </div>
            <i aria-hidden>→</i>
          </NavLink>
        ))}
      </nav>
    </>
  );
}
function State({ kind }: { kind: "loading" | "error" | "empty" }) {
  const map = {
    loading: ["正在核对数据…", "正在读取行情、题材与风险快照。"],
    error: [
      "未能完成回测",
      "服务暂时不可用，请稍后重新运行；已有结果不受影响。",
    ],
    empty: ["没有匹配记录", "清除筛选词可查看全部决策日志。"],
  } as const;
  return (
    <div
      className={`state state-${kind}`}
      role={kind === "error" ? "alert" : "status"}
    >
      <b>{map[kind][0]}</b>
      <p>{map[kind][1]}</p>
    </div>
  );
}
export default function App() {
  const [env, setEnv] = useState<ApiEnvelope<Snapshot> | null>(null);
  useEffect(() => {
    const c = new AbortController();
    getSnapshot(c.signal).then(setEnv).catch((error) => {
      if (error?.name !== "AbortError") console.error("snapshot loading failed", error);
    });
    return () => c.abort();
  }, []);
  if (!env)
    return (
      <div className="boot">
        <State kind="loading" />
      </div>
    );
  return (
    <Shell env={env}>
      {env.stale && (
        <div className="data-notice stale" role="alert">
          <b>数据已过期</b>　新建议已暂停，请检查数据源后再行动。
        </div>
      )}
      {env.message && (
        <div className="data-notice" role="status">
          <b>演示数据</b>　{env.message}
        </div>
      )}
      <DataBoundary p={env.provenance} />
      <Routes>
        <Route path="/" element={<Dashboard s={env.data} />} />
        <Route
          path="/market"
          element={<Market s={env.data} p={env.provenance} />}
        />
        <Route path="/themes" element={<Themes s={env.data} />} />
        <Route path="/stocks/:id" element={<Stock s={env.data} />} />
        <Route path="/risk" element={<Risk s={env.data} />} />
        <Route path="/backtest" element={<Backtest s={env.data} />} />
        <Route path="/simulation" element={<Simulation s={env.data} />} />
        <Route path="/logs" element={<Logs s={env.data} />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="/more" element={<More />} />
      </Routes>
    </Shell>
  );
}
