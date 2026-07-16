import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

const boundary = (page: Page) =>
  page.getByRole("status", { name: "数据来源与研究边界" });

async function expectLoaded(page: Page) {
  await expect(page.locator("main")).toBeVisible();
  await expect(page.getByRole("status", { name: /数据状态：/ })).toContainText(
    "deterministic-demo",
  );
  await expect(boundary(page)).toContainText("工程演示：仅验证流程");
}

test.describe("真实 Demo 全流程", () => {
  test("首页依次进入个股、题材、风险、日志和模拟，边界始终保留", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.goto("/");
    await expectLoaded(page);
    await expect(page.getByRole("heading", { name: /明日模型组合/ })).toBeVisible();

    const stockLink = page.locator('main a[href^="/stocks/"]').first();
    await expect(stockLink).toBeVisible();
    await stockLink.click();
    await expect(page.getByText("个股案卷", { exact: false }).first()).toBeVisible();
    await expect(boundary(page)).toBeVisible();

    const mainNav = page.getByRole("navigation", { name: "主导航", exact: true });
    for (const [name, heading] of [
      ["题材雷达", "追踪扩散，不追逐喧嚣"],
      ["风险中心", "风险先于收益"],
      ["决策日志", "决策日志"],
      ["模拟组合", /^模拟净值/],
    ] as const) {
      await mainNav.getByRole("link", { name }).click();
      await expect(page.getByRole("heading", { name: heading })).toBeVisible();
      await expect(boundary(page)).toBeVisible();
    }
    await expect(page.getByText("不会连接券商").first()).toBeVisible();
    const response = await page.request.get("/api/v1/simulation");
    expect(response.ok()).toBeTruthy();
    const simulation = await response.json();
    expect(simulation.ledger.length).toBeGreaterThan(0);
    const latest = simulation.ledger[0];
    expect(latest.event_time).toBeTruthy();
    expect(latest.payload).toEqual(
      expect.objectContaining({
        model_action: expect.any(String),
        stage: expect.any(String),
        current_weight: expect.any(Number),
        execution_target_weight: expect.any(Number),
      }),
    );
    const latestLedgerRow = page.locator(".simulation-ledger > div").first();
    await expect(latestLedgerRow).toContainText(latest.payload.model_action);
    await expect(latestLedgerRow).toContainText(latest.payload.stage);
    await expect(latestLedgerRow).toContainText("实际权重");
    await expect(latestLedgerRow).toContainText("执行目标");
  });

  test("localhost 可保存设置，并用保存后的资金触发回测和完整研究", async ({
    page,
  }) => {
    await page.goto("/settings");
    await expectLoaded(page);

    const capital = page.getByRole("spinbutton", { name: "模拟资金" });
    await expect(capital).toBeEnabled();
    await capital.fill("880000");
    await page.getByRole("button", { name: "保存设置" }).click();
    await expect(page.getByRole("status").filter({ hasText: "设置已保存" })).toBeVisible();

    const persisted = await page.request.get("/api/v1/settings");
    expect(persisted.ok()).toBeTruthy();
    expect((await persisted.json()).capital).toBe(880000);

    await page.goto("/backtest");
    await expect(page.getByText("本次资金参数")).toContainText("880,000");
    await page.getByRole("button", { name: "运行基准回测" }).click();
    await expect(page.getByRole("status").filter({ hasText: "基准回放已完成" })).toBeVisible({
      timeout: 90_000,
    });
    await expect(page.getByRole("heading", { name: "成交台账与假设" })).toBeVisible();

    await page.getByRole("button", { name: "运行完整研究包" }).click();
    await expect(page.getByRole("region", { name: "完整研究包结果" })).toBeVisible({
      timeout: 90_000,
    });
    await expect(page.getByRole("region", { name: "完整研究包结果" })).toContainText(
      "Overall FAIL",
    );
    await expect(page.getByRole("alert").filter({ hasText: "研究门禁失败" })).toBeVisible();
  });
});

test.describe("响应式、键盘与可访问性", () => {
  for (const width of [360, 768, 1440]) {
    test(`${width}px 下关键页面没有横向溢出`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      for (const route of ["/", "/themes", "/risk", "/logs", "/simulation", "/backtest"]) {
        await page.goto(route);
        await expectLoaded(page);
        const dimensions = await page.evaluate(() => ({
          viewport: document.documentElement.clientWidth,
          html: document.documentElement.scrollWidth,
          body: document.body.scrollWidth,
        }));
        expect(dimensions.html, `${route} html scrollWidth`).toBeLessThanOrEqual(
          dimensions.viewport,
        );
        expect(dimensions.body, `${route} body scrollWidth`).toBeLessThanOrEqual(
          dimensions.viewport,
        );
      }
    });
  }

  test("360px 关键触控目标不少于 44px，键盘焦点清晰可见", async ({ page }) => {
    await page.setViewportSize({ width: 360, height: 800 });
    await page.goto("/");
    await expectLoaded(page);

    const touchTargets = [
      page.getByRole("button", { name: "打印行动单" }),
      ...((await page
        .getByRole("navigation", { name: "移动端主导航" })
        .getByRole("link")
        .all()) as ReturnType<Page["locator"]>[]),
    ];
    for (const target of touchTargets) {
      const box = await target.boundingBox();
      expect(box, "关键触控目标应当可见").not.toBeNull();
      expect(box!.height).toBeGreaterThanOrEqual(44);
      expect(box!.width).toBeGreaterThanOrEqual(44);
    }

    await page.keyboard.press("Tab");
    const skipLink = page.getByRole("link", { name: "跳到主要内容" });
    await expect(skipLink).toBeFocused();
    const focusStyle = await skipLink.evaluate((node) => {
      const style = getComputedStyle(node);
      return { outlineStyle: style.outlineStyle, outlineWidth: style.outlineWidth };
    });
    expect(focusStyle.outlineStyle).not.toBe("none");
    expect(Number.parseFloat(focusStyle.outlineWidth)).toBeGreaterThanOrEqual(2);
  });

  test("真实 Chromium 页面无 WCAG critical/serious（含色彩对比）违规", async ({
    page,
  }, testInfo) => {
    for (const route of ["/", "/risk", "/settings", "/backtest"]) {
      await page.goto(route);
      await expectLoaded(page);
      const results = await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
        .analyze();
      const violations = results.violations.filter((item) =>
        ["critical", "serious"].includes(item.impact ?? ""),
      );
      await testInfo.attach(`axe-${route === "/" ? "home" : route.slice(1)}.json`, {
        body: JSON.stringify(violations, null, 2),
        contentType: "application/json",
      });
      expect(violations, `${route} axe critical/serious violations`).toEqual([]);
    }
  });
});
