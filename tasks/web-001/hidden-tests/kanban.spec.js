// web-001 隐藏验收（Playwright）。验证侧执行，不进 Agent 镜像。
// 契约：三列 data-testid=column-{todo,doing,done}；卡片 [data-card-id]；
// 列内列表 [data-testid=list-<col>]；计数 [data-testid=card-count-<col>]；
// 空状态 [data-testid=empty-state-<col>]；主题切换 [data-testid=theme-toggle]。
const { test, expect } = require("@playwright/test");

const COLUMNS = ["todo", "doing", "done"];

async function resetBoard(page) {
  const errors = [];
  page.on("pageerror", (error) => errors.push(String(error)));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  await page.goto("/index.html");
  await page.evaluate(() => localStorage.clear());
  await page.reload();
  await expect(page.locator('[data-testid="column-todo"]')).toBeVisible();
  return errors;
}

async function dragToCard(page, cardId, targetCardId) {
  const card = page.locator('[data-card-id="' + cardId + '"]');
  const target = page.locator('[data-card-id="' + targetCardId + '"]');
  const cardBox = await card.boundingBox();
  const targetBox = await target.boundingBox();
  await page.mouse.move(cardBox.x + cardBox.width / 2, cardBox.y + cardBox.height / 2);
  await page.mouse.down();
  await page.mouse.move(targetBox.x + targetBox.width / 2, targetBox.y + cardBox.height / 2, {
    steps: 10,
  });
  await page.mouse.up();
}

async function dragToColumnEnd(page, cardId, column) {
  const card = page.locator('[data-card-id="' + cardId + '"]');
  const list = page.locator('[data-testid="list-' + column + '"]');
  const cardBox = await card.boundingBox();
  const listBox = await list.boundingBox();
  await page.mouse.move(cardBox.x + cardBox.width / 2, cardBox.y + cardBox.height / 2);
  await page.mouse.down();
  await page.mouse.move(listBox.x + listBox.width / 2, listBox.y + listBox.height - 8, {
    steps: 10,
  });
  await page.mouse.up();
}

async function order(page, column) {
  return page.locator('[data-testid="list-' + column + '"] [data-card-id]').evaluateAll((nodes) =>
    nodes.map((node) => node.dataset.cardId)
  );
}

test.describe("web-001 看板交互验收", () => {
  test("T1 初始渲染：三列、计数、无控制台错误", async ({ page }) => {
    const errors = await resetBoard(page);
    for (const column of COLUMNS) {
      await expect(page.locator('[data-testid="column-' + column + '"]')).toBeVisible();
    }
    await expect(page.locator('[data-testid="card-count-todo"]')).toHaveText("3");
    await expect(page.locator('[data-testid="card-count-doing"]')).toHaveText("1");
    await expect(page.locator('[data-testid="card-count-done"]')).toHaveText("2");
    expect(errors).toEqual([]);
  });

  test("T2 跨列拖拽：落点前插入并刷新后保持", async ({ page }) => {
    await resetBoard(page);
    await dragToCard(page, "card-1", "card-4");
    await expect(page.locator('[data-testid="card-count-todo"]')).toHaveText("2");
    await expect(page.locator('[data-testid="card-count-doing"]')).toHaveText("2");
    expect(await order(page, "doing")).toEqual(["card-1", "card-4"]);
    await page.reload();
    expect(await order(page, "doing")).toEqual(["card-1", "card-4"]);
  });

  test("T3 列内排序：拖动到目标卡片前", async ({ page }) => {
    await resetBoard(page);
    await dragToCard(page, "card-3", "card-1");
    expect(await order(page, "todo")).toEqual(["card-3", "card-1", "card-2"]);
    await page.reload();
    expect(await order(page, "todo")).toEqual(["card-3", "card-1", "card-2"]);
  });

  test("T4 键盘跨列：ArrowRight / ArrowLeft", async ({ page }) => {
    await resetBoard(page);
    await page.locator('[data-card-id="card-1"]').focus();
    await page.keyboard.press("ArrowRight");
    expect(await order(page, "todo")).toEqual(["card-2", "card-3"]);
    expect(await order(page, "doing")).toEqual(["card-4", "card-1"]);
    await page.keyboard.press("ArrowLeft");
    expect(await order(page, "doing")).toEqual(["card-4"]);
    expect(await order(page, "todo")).toEqual(["card-2", "card-3", "card-1"]);
  });

  test("T5 键盘列内排序：ArrowUp / ArrowDown", async ({ page }) => {
    await resetBoard(page);
    await page.locator('[data-card-id="card-2"]').focus();
    await page.keyboard.press("ArrowUp");
    expect(await order(page, "todo")).toEqual(["card-2", "card-1", "card-3"]);
    await page.keyboard.press("ArrowDown");
    expect(await order(page, "todo")).toEqual(["card-1", "card-2", "card-3"]);
    await page.reload();
    expect(await order(page, "todo")).toEqual(["card-1", "card-2", "card-3"]);
  });

  test("T6 空状态：清空一列后显示提示且计数为 0", async ({ page }) => {
    await resetBoard(page);
    for (const cardId of ["card-1", "card-2", "card-3"]) {
      await dragToColumnEnd(page, cardId, "done");
    }
    await expect(page.locator('[data-testid="empty-state-todo"]')).toBeVisible();
    await expect(page.locator('[data-testid="card-count-todo"]')).toHaveText("0");
    await expect(page.locator('[data-testid="empty-state-done"]')).toBeHidden();
    await expect(page.locator('[data-testid="card-count-done"]')).toHaveText("5");
  });

  test("T7 暗色模式：切换生效且刷新后保持", async ({ page }) => {
    await resetBoard(page);
    const before = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
    await page.locator('[data-testid="theme-toggle"]').click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
    const after = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
    expect(after).not.toBe(before);
    await page.reload();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  });

  test("T8 计数与卡片数量始终一致", async ({ page }) => {
    await resetBoard(page);
    await dragToCard(page, "card-2", "card-5");
    for (const column of COLUMNS) {
      const count = await page.locator('[data-testid="list-' + column + '"] [data-card-id]').count();
      await expect(page.locator('[data-testid="card-count-' + column + '"]')).toHaveText(String(count));
    }
  });

  test("T9 可访问性钩子：卡片可聚焦、主题按钮有 aria-label", async ({ page }) => {
    await resetBoard(page);
    await expect(page.locator('[data-card-id="card-1"]')).toHaveAttribute("tabindex", "0");
    await expect(page.locator('[data-testid="theme-toggle"]')).toHaveAttribute("aria-label", "切换主题");
    await page.keyboard.press("Tab");
    const focused = await page.evaluate(() => document.activeElement?.dataset?.testid || "");
    expect(typeof focused).toBe("string");
  });

  test("T10 数据属性契约未破坏", async ({ page }) => {
    await resetBoard(page);
    for (const column of COLUMNS) {
      await expect(page.locator('[data-column="' + column + '"]')).toHaveCount(1);
      await expect(page.locator('[data-testid="list-' + column + '"]')).toHaveCount(1);
    }
    await expect(page.locator("[data-card-id]")).toHaveCount(6);
  });
});
