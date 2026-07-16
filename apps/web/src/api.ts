import { demoSnapshot } from "./data";
import type {
  Action,
  ApiEnvelope,
  DataProvenance,
  Decision,
  EquityPoint,
  ExitAction,
  Holding,
  Simulation,
  SimulationEvent,
  SimulatedPosition,
  Snapshot,
  Theme,
} from "./types";

const BASE = "/api/v1";
type Json = Record<string, any>;
export const isManagementOriginAllowed = (context: {
  secure: boolean;
  hostname: string;
}) =>
  context.secure ||
  ["localhost", "127.0.0.1", "::1"].includes(context.hostname);
export const managementContextSecure = () =>
  typeof window !== "undefined" &&
  isManagementOriginAllowed({
    secure: window.isSecureContext === true,
    hostname: window.location.hostname,
  });
const adminHeaders = (): Record<string, string> => {
  const key =
    typeof sessionStorage === "undefined"
      ? ""
      : (sessionStorage.getItem("quant_admin_api_key") ?? "");
  return key ? { "X-Admin-Key": key } : {};
};
const pct = (value: number | undefined) => Math.round((value ?? 0) * 1000) / 10;
const money = (value: number | undefined) => `¥${(value ?? 0).toFixed(2)}`;
const failedGates = (item: Json): string[] =>
  Object.values(item.gate_results ?? {})
    .filter((gate: any) => gate?.passed === false)
    .map((gate: any) => String(gate.reason ?? "门禁未通过"));

function holdingFromAdvice(item: Json): Holding {
  const trigger = Array.isArray(item.trigger_zone)
    ? item.trigger_zone.map((x: number) => money(x)).join("—")
    : "等待触发";
  const gateFailures = failedGates(item);
  return {
    code: item.symbol,
    name: item.name,
    theme: item.theme,
    action: (item.action ?? "待买") as Action,
    weight: pct(item.target_weight),
    initialWeight: pct(item.initial_weight),
    currentWeight: pct(item.current_weight),
    price: item.entry_price ?? 0,
    change: 0,
    score: item.score ?? 0,
    thesis: item.thesis ?? [],
    invalidation: item.invalidation ?? "模型逻辑失效时退出",
    initialStop: item.initial_stop ?? 0,
    protectivePrice: item.protective_price ?? null,
    entry: trigger,
    entryState: item.entry_state ?? "等待确认",
    version: item.model_version ?? "swing-rules-0.4.0",
    timestamp: item.data_timestamp ?? "",
    risk: (item.risk_notes ?? []).join("；") || "受市场波动与流动性约束",
    days: item.holding_days ?? 0,
    expectedHoldingDays:
      Array.isArray(item.expected_holding_days) &&
      item.expected_holding_days.length === 2
        ? [
            Number(item.expected_holding_days[0]),
            Number(item.expected_holding_days[1]),
          ]
        : [40, 80],
    nextReviewAt: item.next_review_at ?? "待日终任务生成",
    kind: "model",
    allocationAssigned: true,
    themeLifecycle: item.theme_lifecycle ?? "",
    gateFailures,
  };
}

function holdingFromCandidate(
  item: Json,
  asOf: string,
  version: string,
): Holding {
  const atr = Math.max(item.atr_pct ?? 0.03, 0.06);
  const gateFailures = failedGates(item);
  return {
    code: item.symbol,
    name: item.name,
    theme: item.theme,
    action: "仅观察",
    weight: 0,
    initialWeight: 0,
    currentWeight: 0,
    price: item.close ?? 0,
    change: 0,
    score: item.score ?? 0,
    thesis: item.reasons ?? [],
    invalidation: `当前未获新开仓资格：${gateFailures.join("；") || "尚无完整入场信号"}`,
    initialStop: Number(((item.close ?? 0) * (1 - atr)).toFixed(2)),
    protectivePrice: null,
    entry: "未分配触发区间",
    entryState: gateFailures.length
      ? `未通过：${gateFailures[0]}`
      : "等待完整入场信号",
    version,
    timestamp: asOf,
    risk: "仅进入观察名单，不分配仓位、不构成买入动作",
    days: 0,
    expectedHoldingDays: [40, 80],
    nextReviewAt: "下一交易日收盘后",
    kind: "candidate",
    allocationAssigned: false,
    themeLifecycle: item.theme_lifecycle ?? "",
    gateFailures,
  };
}

function exitActionFrom(raw: Json, asOf: string, version: string): ExitAction {
  return {
    code: raw.symbol ?? "—",
    name: raw.name ?? raw.symbol ?? "未知标的",
    theme: raw.theme ?? "未分类",
    currentWeight: pct(raw.current_weight),
    targetWeight: 0,
    reason: raw.exit_reason ?? raw.invalidation ?? "退出条件已触发",
    priority: raw.priority ?? "normal",
    kind: raw.kind ?? "model_exit",
    version: raw.model_version ?? version,
    timestamp: raw.data_timestamp ?? asOf,
  };
}

const factorLabels: Record<string, string> = {
  trend_breadth: "A股趋势与广度",
  style: "风格结构",
  liquidity: "成交与流动性",
  global_risk: "全球风险",
  sentiment: "情绪与拥挤",
  valuation: "估值与风险溢价",
};
const factorWeights: Record<string, number> = {
  trend_breadth: 25,
  style: 20,
  liquidity: 15,
  global_risk: 15,
  sentiment: 15,
  valuation: 10,
};
export function normalizeSimulation(
  raw: Json,
  dashboardSimulation: Json | undefined,
): Simulation {
  const account = raw.simulated_account ?? {};
  const latest =
    (raw.daily_equity ?? []).at?.(-1) ?? dashboardSimulation?.valuation ?? {};
  const positions: SimulatedPosition[] = (raw.simulated_positions ?? []).map(
    (x: Json) => ({
      symbol: x.symbol,
      shares: x.shares ?? 0,
      avgCost: x.avg_cost ?? 0,
      updatedAt: x.updated_at ?? "",
    }),
  );
  const ledger: SimulationEvent[] = (raw.ledger ?? []).map((x: Json) => ({
    id: String(x.id ?? ""),
    symbol: x.symbol ?? "—",
    side: x.payload?.side ?? "—",
    status: x.status ?? "unknown",
    quantity: x.quantity ?? 0,
    price: x.price ?? null,
    fee: x.fee ?? 0,
    createdAt: x.event_time ?? "",
    reason: x.payload?.reason ?? "",
    modelAction: x.payload?.model_action ?? "—",
    stage: x.payload?.stage ?? "—",
    currentWeight: pct(x.payload?.current_weight),
    executionTargetWeight: pct(x.payload?.execution_target_weight),
  }));
  const dailyEquity: EquityPoint[] = (raw.daily_equity ?? []).map(
    (x: Json) => ({
      day: x.day ?? x.trade_date ?? "",
      cash: x.cash ?? 0,
      marketValue: x.market_value ?? 0,
      equity: x.equity ?? 0,
      drawdown: x.drawdown ?? 0,
    }),
  );
  return {
    matchingReady: dashboardSimulation?.matching_ready ?? true,
    matchingReason: dashboardSimulation?.matching_reason ?? "",
    cash: latest.cash ?? account.cash ?? 0,
    marketValue: latest.market_value ?? 0,
    equity: latest.equity ?? account.initial_capital ?? 0,
    drawdown: latest.drawdown ?? 0,
    positions,
    ledger,
    dailyEquity,
  };
}
function normalize(
  raw: Json,
  logs: Json[],
  simulationRaw: Json = {},
  settingsRaw: Json = {},
): Snapshot {
  const asOf = raw.as_of ?? new Date().toISOString();
  const version =
    raw.model_version ?? raw.portfolio?.[0]?.model_version ?? "unknown-model";
  const held = new Set((raw.portfolio ?? []).map((x: Json) => x.symbol));
  const holdings = (raw.portfolio ?? []).map(holdingFromAdvice);
  const exitActions = (raw.exit_actions ?? []).map((item: Json) =>
    exitActionFrom(item, asOf, version),
  );
  const candidates = (raw.candidates ?? [])
    .filter((x: Json) => !held.has(x.symbol))
    .slice(0, 3)
    .map((x: Json) => holdingFromCandidate(x, asOf, version));
  const themes: Theme[] = (raw.themes ?? []).map((t: Json) => ({
    name: t.name,
    phase: t.lifecycle,
    score: t.score,
    breadth: t.breadth,
    flow: t.turnover,
    crowding: t.crowding,
    relativeStrength: t.relative_strength ?? 50,
    fundamental: t.fundamental ?? 50,
    catalyst: t.catalyst ?? 50,
    leadership: t.leadership ?? 50,
    note: `相对强度 ${t.relative_strength} · 龙头稳定 ${t.leadership}`,
    lifecycleReason: t.lifecycle_reason ?? "生命周期依据未返回",
    fundFlowLabel: t.fund_flow_label ?? "资金流口径未返回",
  }));
  const decisions: Decision[] = logs.map((d: Json) => ({
    id: d.id,
    date: (d.timestamp ?? "").replace("T", " ").slice(5, 16),
    title: `${d.market_regime}：模型组合 ${d.holdings?.length ?? 0} 只`,
    reason: (d.reasons ?? []).join("；"),
    version: d.model_version ?? "swing-rules-0.4.0",
    result: "已记录",
  }));
  const components = raw.market?.components ?? {};
  return {
    asOf,
    modelVersion: version,
    capital:
      typeof settingsRaw.capital === "number" ? settingsRaw.capital : null,
    cashWeight: pct(raw.cash_weight ?? 1 - (raw.market?.exposure_cap ?? 0)),
    portfolioStatus: raw.portfolio_status ?? "healthy",
    portfolioReason: raw.portfolio_reason,
    market: {
      state: raw.market?.regime ?? "未知",
      score: raw.market?.score ?? 0,
      exposure: pct(raw.market?.exposure_cap),
      style: raw.market?.style ?? "待判断",
      summary: (raw.market?.reasons ?? []).join("；") || "等待市场状态计算",
      factors: Object.entries(components).map(([name, score]) => ({
        name: factorLabels[name] ?? name,
        score: Number(score),
        weight: factorWeights[name] ?? 0,
        note: `当前分项 ${Number(score).toFixed(1)} 分`,
      })),
    },
    holdings,
    exitActions,
    candidates,
    themes,
    decisions: decisions.length ? decisions : [],
    equity: [100],
    simulation: normalizeSimulation(simulationRaw, raw.simulation),
    maxPortfolioDrawdown:
      typeof settingsRaw.max_portfolio_drawdown === "number"
        ? pct(settingsRaw.max_portfolio_drawdown)
        : null,
  };
}

function provenance(raw: Json, status: Json | undefined): DataProvenance {
  const provider = String(status?.active ?? raw.provider ?? "unknown");
  const details = Array.isArray(status?.providers) ? status.providers : [];
  const detail =
    details.find((item: Json) => String(item.provider) === provider) || {};
  const demo = /demo/i.test(provider);
  const publicPrototype = /tushare|akshare/i.test(provider);
  const pitVerified = detail.pit_verified === true;
  const productionReady = detail.production_ready === true;
  const degradations: string[] = [];
  if (typeof detail.warning === "string") degradations.push(detail.warning);
  if (typeof detail.reason === "string" && detail.reason)
    degradations.push(detail.reason);
  for (const issue of raw.quality?.issues ?? [])
    if (typeof issue?.message === "string") degradations.push(issue.message);
  const sourceAudit = raw.data_provenance ?? status?.provenance ?? {};
  const securityAudit = sourceAudit.security_metadata ?? {};
  const historyAudit = sourceAudit.price_history ?? {};
  if (securityAudit.live_endpoint_available === false)
    degradations.push(
      "实时个股元数据不可用，当前使用显式观察池静态元数据回退。",
    );
  const historySources = Object.values(historyAudit.sources ?? {}).map(String);
  if (historySources.includes("sina_qfq_fallback"))
    degradations.push(
      "东方财富行情不可用，部分或全部标的已切换至新浪前复权日线。",
    );
  if (!demo && !pitVerified)
    degradations.push(
      "未验证 PIT：历史可见时间、证券状态与题材成分不能视为已重建。",
    );
  if (!demo && !productionReady)
    degradations.push("非生产数据：不能作为可发布建议或策略验收依据。");
  const marketInputs = sourceAudit.market_inputs ?? {};
  const universeSelection = sourceAudit.universe_selection ?? {};
  return {
    provider,
    mode: demo
      ? "demo"
      : publicPrototype
        ? "public-prototype"
        : productionReady && pitVerified
          ? "verified"
          : "research",
    pitVerified,
    productionReady,
    degradations: [...new Set(degradations)],
    marketInputs: {
      source: marketInputs.source ?? "未提供输入口径",
      globalRiskQuality: marketInputs.global_risk_quality ?? "unknown",
      fundFlowQuality: marketInputs.fund_flow_quality ?? "unknown",
      valuationQuality: marketInputs.valuation_quality ?? "unknown",
      globalComponentsAvailable: Number(
        marketInputs.global_components_available ?? 0,
      ),
      globalComponentsTotal: Number(marketInputs.global_components_total ?? 0),
      globalRiskProxy: marketInputs.global_risk_proxy ?? "",
      fundFlowProxy: marketInputs.fund_flow_proxy ?? "",
    },
    universeSelection: {
      mode: universeSelection.mode ?? "unknown",
      fallbackUsed: Boolean(universeSelection.fallback_used),
      selectedCount: Number(universeSelection.selected_count ?? 0),
      requestedAsOf: universeSelection.requested_as_of ?? null,
      snapshotKind: universeSelection.snapshot_kind ?? "unknown",
      warning: universeSelection.warning ?? "",
    },
  };
}

export async function getSnapshot(
  signal?: AbortSignal,
): Promise<ApiEnvelope<Snapshot>> {
  try {
    const [dashboardRes, decisionRes, statusRes, simulationRes, settingsRes] =
      await Promise.all([
        fetch(`${BASE}/dashboard`, {
          signal,
          headers: { Accept: "application/json" },
        }),
        fetch(`${BASE}/decisions?limit=20`, {
          signal,
          headers: { Accept: "application/json" },
        }),
        fetch(`${BASE}/data/status`, {
          signal,
          headers: { Accept: "application/json" },
        }).catch(() => null),
        fetch(`${BASE}/simulation`, {
          signal,
          headers: { Accept: "application/json" },
        }).catch(() => null),
        fetch(`${BASE}/settings`, {
          signal,
          headers: { Accept: "application/json" },
        }).catch(() => null),
      ]);
    if (!dashboardRes.ok) throw new Error(`HTTP ${dashboardRes.status}`);
    const raw = await dashboardRes.json();
    const logBody = decisionRes.ok ? await decisionRes.json() : { items: [] };
    const statusBody = statusRes?.ok ? await statusRes.json() : undefined;
    const simulationBody = simulationRes?.ok ? await simulationRes.json() : {};
    const settingsBody = settingsRes?.ok ? await settingsRes.json() : {};
    const isDemo = String(raw.provider ?? "").includes("demo");
    const stale =
      raw.quality?.freshness === "stale" || raw.quality?.status === "blocked";
    return {
      data: normalize(raw, logBody.items ?? [], simulationBody, settingsBody),
      source: isDemo ? "demo" : "live",
      stale,
      provenance: provenance(raw, statusBody),
      message: isDemo
        ? "API 已连接，当前使用可重复的确定性演示数据。配置持牌数据源后才可生成真实日终建议。"
        : undefined,
    };
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError")
      throw error;
    return {
      data: demoSnapshot,
      source: "demo",
      stale: false,
      provenance: {
        provider: "deterministic-demo",
        mode: "demo",
        pitVerified: false,
        productionReady: false,
        degradations: ["服务不可用，当前仅显示本地确定性演示数据。"],
      },
      message: "实时接口暂不可用，当前显示可重复的演示数据。",
    };
  }
}

export async function getDecision(id: string, signal?: AbortSignal) {
  const r = await fetch(`${BASE}/decisions/${encodeURIComponent(id)}`, {
    signal,
    headers: { Accept: "application/json" },
  });
  if (!r.ok) throw new Error(`决策证据服务返回 HTTP ${r.status}`);
  return await r.json();
}

export async function listBacktests(signal?: AbortSignal) {
  const r = await fetch(`${BASE}/backtests`, {
    signal,
    headers: { Accept: "application/json" },
  });
  if (!r.ok) throw new Error(`回测历史服务返回 HTTP ${r.status}`);
  return await r.json();
}

export async function runBacktest(capital: number) {
  const r = await fetch(`${BASE}/backtests`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...adminHeaders() },
    body: JSON.stringify({ capital }),
  });
  if (!r.ok) throw new Error(`回测服务返回 HTTP ${r.status}`);
  return await r.json();
}

export async function runResearch(capital: number) {
  const r = await fetch(`${BASE}/research/runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...adminHeaders() },
    body: JSON.stringify({ capital }),
  });
  if (!r.ok) throw new Error(`研究服务返回 HTTP ${r.status}`);
  return await r.json();
}

export async function saveSettings(settings: Record<string, unknown>) {
  const r = await fetch(`${BASE}/settings`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json", ...adminHeaders() },
    body: JSON.stringify(settings),
  });
  if (!r.ok) throw new Error(`设置服务返回 HTTP ${r.status}`);
  return await r.json();
}

export async function getSettings(signal?: AbortSignal) {
  const r = await fetch(`${BASE}/settings`, {
    signal,
    headers: { Accept: "application/json" },
  });
  if (!r.ok) throw new Error(`设置服务返回 HTTP ${r.status}`);
  return await r.json();
}
