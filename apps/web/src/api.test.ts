import { afterEach, describe, expect, it, vi } from "vitest";
import {
  getDecision,
  getSettings,
  isManagementOriginAllowed,
  listBacktests,
  normalizeSimulation,
  runBacktest,
  runResearch,
  saveSettings,
} from "./api";

afterEach(() => {
  sessionStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("管理请求边界", () => {
  it("映射模拟动作、阶段、权重和真实事件时间", () => {
    const result = normalizeSimulation(
      {
        ledger: [
          {
            id: 7,
            symbol: "600000.SH",
            event_time: "2026-07-16T15:01:02+08:00",
            created_at: "错误字段不应使用",
            status: "filled",
            quantity: 100,
            price: 12.3,
            fee: 5,
            payload: {
              side: "buy",
              model_action: "确认加仓",
              stage: "target",
              current_weight: 0.08,
              execution_target_weight: 0.16,
            },
          },
        ],
      },
      undefined,
    );
    expect(result.ledger[0]).toMatchObject({
      createdAt: "2026-07-16T15:01:02+08:00",
      modelAction: "确认加仓",
      stage: "target",
      currentWeight: 8,
      executionTargetWeight: 16,
    });
  });

  it.each([
    [{ secure: false, hostname: "106.54.203.187" }, false],
    [{ secure: false, hostname: "example.com" }, false],
    [{ secure: false, hostname: "localhost" }, true],
    [{ secure: true, hostname: "106.54.203.187" }, true],
  ])("仅 HTTPS 或本机来源允许管理写操作：%j", (context, expected) => {
    expect(isManagementOriginAllowed(context)).toBe(expected);
  });

  it.each([
    ["回测", () => runBacktest(300000)],
    ["完整研究包", () => runResearch(300000)],
    ["设置", () => saveSettings({ capital: 1000000 })],
  ])("%s请求只从sessionStorage发送管理密钥", async (_label, request) => {
    sessionStorage.setItem("quant_admin_api_key", "session-secret");
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ ok: true, json: async () => ({}) });
    vi.stubGlobal("fetch", fetchMock);
    await request();
    const options = fetchMock.mock.calls[0][1];
    expect(options.headers["X-Admin-Key"]).toBe("session-secret");
    expect(localStorage.getItem("quant_admin_api_key")).toBeNull();
  });

  it("只读设置请求不携带管理密钥", async () => {
    sessionStorage.setItem("quant_admin_api_key", "session-secret");
    const fetchMock = vi
      .fn()
      .mockResolvedValue({
        ok: true,
        json: async () => ({ capital: 3000000 }),
      });
    vi.stubGlobal("fetch", fetchMock);
    await getSettings();
    expect(fetchMock.mock.calls[0][1].headers).toEqual({
      Accept: "application/json",
    });
  });

  it("回测与研究请求使用调用方传入的当前资金", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ ok: true, json: async () => ({}) });
    vi.stubGlobal("fetch", fetchMock);
    await runBacktest(880000);
    await runResearch(660000);
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      capital: 880000,
    });
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({
      capital: 660000,
    });
  });

  it("历史回测和决策证据保持公开只读请求", async () => {
    sessionStorage.setItem("quant_admin_api_key", "session-secret");
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ ok: true, json: async () => ({ items: [] }) });
    vi.stubGlobal("fetch", fetchMock);
    await listBacktests();
    await getDecision("D/1");
    expect(fetchMock.mock.calls[0][1].headers).toEqual({
      Accept: "application/json",
    });
    expect(fetchMock.mock.calls[1][0]).toContain("D%2F1");
    expect(fetchMock.mock.calls[1][1].headers).toEqual({
      Accept: "application/json",
    });
  });
});
