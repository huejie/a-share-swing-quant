import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "./App";
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
describe("衡策前端", () => {
  it("API不可用时展示演示数据与完整组合字段", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    render(
      <MemoryRouter>
        <App />
      </MemoryRouter>,
    );
    expect((await screen.findAllByText("演示数据")).length).toBeGreaterThan(0);
    expect(screen.getByText("模型组合")).toBeInTheDocument();
    expect(screen.getAllByText("失效条件").length).toBeGreaterThan(0);
    expect(screen.getAllByText("保护价").length).toBeGreaterThan(0);
    expect(
      screen.getByText(/模型输出，不是模拟成交或真实持仓/),
    ).toBeInTheDocument();
  });
  it("主要导航包含研究与模拟页面", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    render(
      <MemoryRouter>
        <App />
      </MemoryRouter>,
    );
    await screen.findAllByText("演示数据");
    for (const label of [
      "市场温度",
      "题材雷达",
      "风险中心",
      "回测实验",
      "模拟组合",
      "决策日志",
      "设置",
    ])
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
  });
  it("设置页展示可保存的通知偏好且不要求输入外部目标", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string) =>
        Promise.resolve({
          ok: true,
          json: async () =>
            url.includes("/settings")
              ? {
                  capital: 1000000,
                  target_count: 4,
                  max_portfolio_drawdown: 0.18,
                  max_adv_participation: 0.015,
                  include_chinext: true,
                  include_star: true,
                  notify_eod_success: true,
                  notify_risk: true,
                  notification_channel: "webhook",
                }
              : url.includes("/dashboard")
                ? {
                    as_of: "2026-07-07",
                    provider: "licensed",
                    quality: { freshness: "fresh", status: "healthy" },
                    market: { regime: "震荡", score: 60, exposure_cap: 0.5, components: {} },
                    portfolio: [],
                    candidates: [],
                    themes: [],
                  }
                : url.includes("/decisions")
                  ? { items: [] }
                  : url.includes("/simulation")
                    ? {}
                    : { active: "licensed", providers: [] },
        }),
      ),
    );
    render(
      <MemoryRouter initialEntries={["/settings"]}>
        <App />
      </MemoryRouter>,
    );
    expect(await screen.findByText("日终任务成功后提醒")).toBeInTheDocument();
    expect(screen.getByText(/数据门禁、组合风控或退出动作/)).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Webhook" })).toBeInTheDocument();
    await waitFor(() =>
      expect(
        (screen.getByRole("spinbutton", { name: /单股计划金额/ }) as HTMLInputElement).value,
      ).toBe("1.5"),
    );
    expect(screen.getByText(/只在服务器环境变量中配置/)).toBeInTheDocument();
  });
  it("公开接口显示研究/模拟观察、非PIT和非生产边界", async () => {
    const raw = {
      as_of: "2026-07-07T16:10:00+08:00",
      provider: "akshare",
      quality: { freshness: "fresh", status: "healthy" },
      market: {
        regime: "震荡偏强",
        score: 68,
        exposure_cap: 0.72,
        style: "成长占优",
        reasons: ["市场广度改善"],
        components: { trend_breadth: 76 },
      },
      portfolio: [
        {
          symbol: "002050.SZ",
          name: "三花智控",
          theme: "机器人",
          action: "持有",
          target_weight: 0.2,
          current_weight: 0.18,
          entry_price: 44.8,
          initial_stop: 40.9,
          thesis: ["趋势健康"],
          invalidation: "跌破结构",
          risk_notes: ["事件风险"],
          model_version: "v1",
          data_timestamp: "2026-07-07",
        },
      ],
      candidates: [],
      themes: [],
    };
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockImplementation((url: string) =>
          Promise.resolve({
            ok: true,
            json: async () =>
              url.includes("/dashboard")
                ? raw
                : url.includes("/decisions")
                  ? { items: [] }
                  : {
                      active: "akshare",
                      providers: [
                        {
                          provider: "akshare",
                          pit_verified: false,
                          production_ready: false,
                          warning: "网页接口未证明PIT、历史成分或商业授权",
                        },
                      ],
                    },
          }),
        ),
    );
    render(
      <MemoryRouter>
        <App />
      </MemoryRouter>,
    );
    await waitFor(() =>
      expect(screen.getByText("公开接口原型")).toBeInTheDocument(),
    );
    expect(screen.getByText(/仅研究与模拟观察/)).toBeInTheDocument();
    expect(screen.getByText("未验证 PIT")).toBeInTheDocument();
    expect(screen.getByText("非生产数据")).toBeInTheDocument();
    expect(screen.getByText("三花智控")).toBeInTheDocument();
  });
  it.each([
    ["/market", "68° 震荡偏强"],
    ["/themes", "追踪扩散，不追逐喧嚣"],
    ["/risk", "风险先于收益"],
    ["/backtest", "先证伪，再相信"],
    ["/simulation", "模拟净值 ¥1,000,000"],
    ["/logs", "决策日志"],
    ["/settings", "设置"],
    ["/more", "研究工作台"],
    ["/stocks/002050", "三花智控 · 持有"],
  ])("路由 %s 可独立访问并提供实质内容", async (path, title) => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    render(
      <MemoryRouter initialEntries={[path]}>
        <App />
      </MemoryRouter>,
    );
    expect(
      await screen.findByRole("heading", { level: 1, name: title }),
    ).toBeInTheDocument();
  });
  it("首页区分市场仓位上限与模型实际现金，并在空仓时显示服务端模型版本", async () => {
    const raw = {
      as_of: "2026-07-07T16:10:00+08:00",
      model_version: "server-model-v9",
      provider: "licensed",
      cash_weight: 0.8,
      quality: { freshness: "fresh", status: "healthy" },
      market: {
        regime: "震荡偏弱",
        score: 52,
        exposure_cap: 0.5,
        components: {},
      },
      portfolio: [],
      candidates: [],
      themes: [],
    };
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockImplementation((url: string) =>
          Promise.resolve({
            ok: true,
            json: async () =>
              url.includes("/dashboard")
                ? raw
                : url.includes("/decisions")
                  ? { items: [] }
                  : url.includes("/simulation")
                    ? { daily_equity: [], simulated_positions: [], ledger: [] }
                    : {
                        active: "licensed",
                        providers: [
                          {
                            provider: "licensed",
                            pit_verified: true,
                            production_ready: true,
                          },
                        ],
                      },
          }),
        ),
    );
    render(
      <MemoryRouter>
        <App />
      </MemoryRouter>,
    );
    expect(
      await screen.findByRole("heading", { level: 1, name: /现金 80.0%/ }),
    ).toBeInTheDocument();
    expect(screen.getByText(/server-model-v9/)).toBeInTheDocument();
    expect(screen.getByText("50%")).toBeInTheDocument();
    expect(screen.getAllByText("80.0%").length).toBeGreaterThan(0);
  });
  it("模拟页只展示实际撮合持仓，不把模型输出冒充成交", async () => {
    const raw = {
      as_of: "2026-07-07T16:10:00+08:00",
      provider: "licensed",
      cash_weight: 0.8,
      quality: { freshness: "fresh", status: "healthy" },
      market: {
        regime: "震荡偏强",
        score: 68,
        exposure_cap: 0.5,
        components: {},
      },
      portfolio: [
        {
          symbol: "MODEL.SZ",
          name: "模型股票",
          theme: "测试",
          action: "待买",
          target_weight: 0.2,
          initial_weight: 0.1,
          entry_price: 10,
          initial_stop: 9,
          thesis: ["模型证据"],
          invalidation: "跌破结构",
        },
      ],
      candidates: [],
      themes: [],
      simulation: { matching_ready: true },
    };
    const simulation = {
      simulated_account: { cash: 800000, initial_capital: 1000000 },
      simulated_positions: [
        {
          symbol: "FILLED.SZ",
          shares: 1000,
          avg_cost: 12.3,
          updated_at: "2026-07-07T10:00:00+08:00",
        },
      ],
      ledger: [],
      daily_equity: [
        {
          day: "2026-07-07",
          cash: 800000,
          market_value: 200000,
          equity: 1000000,
          drawdown: 0,
        },
      ],
    };
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockImplementation((url: string) =>
          Promise.resolve({
            ok: true,
            json: async () =>
              url.includes("/dashboard")
                ? raw
                : url.includes("/decisions")
                  ? { items: [] }
                  : url.includes("/simulation")
                    ? simulation
                    : {
                        active: "licensed",
                        providers: [
                          {
                            provider: "licensed",
                            pit_verified: true,
                            production_ready: true,
                          },
                        ],
                      },
          }),
        ),
    );
    render(
      <MemoryRouter initialEntries={["/simulation"]}>
        <App />
      </MemoryRouter>,
    );
    expect(await screen.findByText("FILLED.SZ")).toBeInTheDocument();
    expect(screen.queryByText("模型股票")).not.toBeInTheDocument();
    expect(
      screen.getByText(
        (_text, node) =>
          node?.classList.contains("simulation-banner") === true &&
          node.textContent?.includes("不把今日 1 只模型标的冒充成交") === true,
      ),
    ).toBeInTheDocument();
  });

  it("决策日志按需展示当时快照指纹和候选门禁", async () => {
    const raw = {
      as_of: "2026-07-07T16:10:00+08:00",
      provider: "licensed",
      quality: { freshness: "fresh", status: "healthy" },
      market: { regime: "震荡", score: 60, exposure_cap: 0.5, components: {} },
      portfolio: [], candidates: [], themes: [],
    };
    const detail = {
      id: "D-1", model_version: "model-v4", config_hash: "config-hash",
      data_snapshot_hash: "snapshot-hash", provider: "licensed",
      input_snapshot: { available: true, compressed_bytes: 321 },
      snapshot: {
        quality: { status: "healthy" },
        selected_themes: [{ name: "机器人", lifecycle: "扩散", score: 82 }],
        candidate_audit: [{ symbol: "A.SZ", name: "候选A", eligible: false, score: 61, gate_results: { acceleration: { passed: false, reason: "题材加速禁止新开" } } }],
        portfolio: [], exit_actions: [],
      },
    };
    vi.stubGlobal("fetch", vi.fn().mockImplementation((url: string) => Promise.resolve({ ok: true, json: async () => url.includes("/decisions/D-1") ? detail : url.includes("/dashboard") ? raw : url.includes("/decisions") ? { items: [{ id: "D-1", timestamp: "2026-07-07T16:10:00", market_regime: "震荡", model_version: "model-v4", holdings: [], reasons: ["证据已保存"] }] } : url.includes("/simulation") ? {} : url.includes("/settings") ? { capital: 880000 } : { active: "licensed", providers: [{ provider: "licensed", pit_verified: true, production_ready: true }] } })));
    render(<MemoryRouter initialEntries={["/logs"]}><App /></MemoryRouter>);
    fireEvent.click(await screen.findByRole("button", { name: "查看当时证据" }));
    expect(await screen.findByText("snapshot-hash")).toBeInTheDocument();
    expect(screen.getByText(/题材加速禁止新开/)).toBeInTheDocument();
    expect(screen.getByText(/可用 · 321 bytes/)).toBeInTheDocument();
  });

  it("回测页读取设置资金并列出公开历史", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation((url: string) => Promise.resolve({ ok: true, json: async () => url.includes("/dashboard") ? { as_of: "2026-07-07", provider: "licensed", quality: { freshness: "fresh", status: "healthy" }, market: { regime: "震荡", score: 60, exposure_cap: 0.5, components: {} }, portfolio: [], candidates: [], themes: [] } : url.includes("/decisions") ? { items: [] } : url.includes("/settings") ? { capital: 880000, max_portfolio_drawdown: 0.16 } : url.endsWith("/backtests") ? { items: [{ id: "BT-OLD", status: "completed", initial_capital: 880000, total_return: 0.1, assumptions: { research_gate_status: "FAIL" } }] } : url.includes("/simulation") ? {} : { active: "licensed", providers: [{ provider: "licensed", pit_verified: true, production_ready: true }] } })));
    render(<MemoryRouter initialEntries={["/backtest"]}><App /></MemoryRouter>);
    expect(await screen.findByText(/¥880,000/)).toBeInTheDocument();
    expect(await screen.findByText(/BT-OLD/)).toBeInTheDocument();
    expect(screen.getByText(/settings/)).toBeInTheDocument();
  });

  it("完整研究包保留 FAIL 并展示各研究维度", async () => {
    const research = {
      id: "R-FAIL", overall: "FAIL", candidate_label: "工程候选版/模拟观察中",
      gates: [{ id: "BT-006-BASELINES", passed: false, reason: "缺少完整基线" }],
      report: {
        baselines: [{ name: "沪深300" }, { name: "中证全指" }, { name: "简单动量" }],
        capacity: [{ capital: 100000 }, { capital: 1000000 }, { capital: 3000000 }, { capital: 10000000 }],
        sensitivity: [{ parameter: "rebalance_days", stable_neighbor: false }],
        ablation: { items: [{ factor: "trend_breadth" }] },
        stress: [{ scenario: "gap_down" }],
        contribution_attribution: { status: "unavailable" },
        performance_metrics: { max_drawdown: -0.2 },
      },
    };
    const fetchMock = vi.fn().mockImplementation((url: string, options?: RequestInit) => Promise.resolve({ ok: true, json: async () => url.includes("/research/runs") && options?.method === "POST" ? research : url.includes("/dashboard") ? { as_of: "2026-07-07", provider: "licensed", quality: { freshness: "fresh", status: "healthy" }, market: { regime: "震荡", score: 60, exposure_cap: 0.5, components: {} }, portfolio: [], candidates: [], themes: [] } : url.includes("/decisions") ? { items: [] } : url.includes("/settings") ? { capital: 880000 } : url.endsWith("/backtests") ? { items: [] } : url.includes("/simulation") ? {} : { active: "licensed", providers: [{ provider: "licensed", pit_verified: true, production_ready: true }] } }));
    vi.stubGlobal("fetch", fetchMock);
    render(<MemoryRouter initialEntries={["/backtest"]}><App /></MemoryRouter>);
    fireEvent.click(await screen.findByRole("button", { name: "运行完整研究包" }));
    expect(await screen.findByRole("heading", { name: "Overall FAIL" })).toBeInTheDocument();
    expect(screen.getByText(/该结果不能包装为策略通过/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /三项基线/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /四档资金容量/ })).toBeInTheDocument();
  });
});
