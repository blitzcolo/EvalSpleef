// web-001 参考解 A：原生 JS 单页实现（独立实现，无构建步骤）。
// 功能：跨列拖拽排序 + 列内排序 + 键盘操作 + 空状态 + 计数 + 暗色模式 + localStorage 持久化。
// 运行：python3 -m http.server 8080 后打开 index.html。
"use strict";

const SEED = {
  todo: [
    { id: "card-1", title: "整理需求清单", meta: "产品" },
    { id: "card-2", title: "设计空状态", meta: "设计" },
    { id: "card-3", title: "补验收用例", meta: "测试" },
  ],
  doing: [{ id: "card-4", title: "实现拖拽排序", meta: "前端" }],
  done: [
    { id: "card-5", title: "搭建页面骨架", meta: "前端" },
    { id: "card-6", title: "评审视觉稿", meta: "设计" },
  ],
};

const COLUMNS = ["todo", "doing", "done"];
const STORAGE_KEY = "kanban-state-v1";
const THEME_KEY = "kanban-theme";
const NEXT = { todo: "doing", doing: "done", done: null };
const PREV = { todo: null, doing: "todo", done: "doing" };

const clone = (value) => JSON.parse(JSON.stringify(value));

function loadState() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (COLUMNS.every((c) => Array.isArray(parsed[c]))) return parsed;
    }
  } catch (error) {
    /* 数据损坏则回退种子 */
  }
  return clone(SEED);
}

function saveState() {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme === "dark" ? "dark" : "light";
  document.querySelector('[data-testid="theme-toggle"]').textContent =
    theme === "dark" ? "切换到浅色" : "切换到暗色";
}

const state = loadState();

function renderCard(card) {
  const li = document.createElement("li");
  li.className = "card";
  li.dataset.cardId = card.id;
  li.dataset.dragging = "false";
  li.tabIndex = 0;
  li.innerHTML = '<p class="card__title"></p><p class="card__meta"></p>';
  li.querySelector(".card__title").textContent = card.title;
  li.querySelector(".card__meta").textContent = card.meta;
  return li;
}

function render() {
  for (const column of COLUMNS) {
    const list = document.querySelector('[data-testid="list-' + column + '"]');
    list.replaceChildren(...state[column].map(renderCard));
    document.querySelector('[data-testid="card-count-' + column + '"]').textContent =
      String(state[column].length);
    const empty = document.querySelector('[data-testid="empty-state-' + column + '"]');
    empty.hidden = state[column].length !== 0;
  }
}

/* ---------------------------------------------------------------- 拖拽 */
let drag = null; // {cardId, sourceColumn}

function cardById(cardId) {
  for (const column of COLUMNS) {
    const card = state[column].find((item) => item.id === cardId);
    if (card) return card;
  }
  return null;
}

function insertAt(column, card, index) {
  if (index == null) state[column].push(card);
  else state[column].splice(index, 0, card);
}

function moveCard(cardId, targetColumn, targetCardId) {
  const card = cardById(cardId);
  if (!card) return false;
  for (const column of COLUMNS) {
    state[column] = state[column].filter((item) => item.id !== cardId);
  }
  const targetList = state[targetColumn];
  const index = targetCardId
    ? targetList.findIndex((item) => item.id === targetCardId)
    : null;
  insertAt(targetColumn, card, index == null || index < 0 ? null : index);
  saveState();
  render();
  return true;
}

document.addEventListener("pointerdown", (event) => {
  const card = event.target.closest(".card");
  if (!card || event.button !== 0) return;
  drag = { cardId: card.dataset.cardId, sourceColumn: card.closest("[data-column]").dataset.column };
  card.dataset.dragging = "true";
});

document.addEventListener("pointerup", (event) => {
  if (!drag) return;
  const card = drag;
  drag = null;
  const hit = document.elementFromPoint(event.clientX, event.clientY);
  const targetCard = hit && hit.closest(".card");
  const column = hit && hit.closest("[data-column]");
  if (targetCard) {
    moveCard(card.cardId, targetCard.closest("[data-column]").dataset.column, targetCard.dataset.cardId);
  } else if (column) {
    moveCard(card.cardId, column.dataset.column, null);
  } else {
    render(); // 拖到空白处：回弹
  }
});

/* ---------------------------------------------------------------- 键盘 */
document.addEventListener("keydown", (event) => {
  const card = event.target.closest(".card");
  if (!card) return;
  const cardId = card.dataset.cardId;
  const column = card.closest("[data-column]").dataset.column;
  const index = state[column].findIndex((item) => item.id === cardId);
  if (event.key === "ArrowRight" && NEXT[column]) {
    event.preventDefault();
    moveCard(cardId, NEXT[column], null);
    focusCard(cardId);
  } else if (event.key === "ArrowLeft" && PREV[column]) {
    event.preventDefault();
    moveCard(cardId, PREV[column], null);
    focusCard(cardId);
  } else if (event.key === "ArrowUp" && index > 0) {
    event.preventDefault();
    const [item] = state[column].splice(index, 1);
    state[column].splice(index - 1, 0, item);
    saveState();
    render();
    focusCard(cardId);
  } else if (event.key === "ArrowDown" && index < state[column].length - 1) {
    event.preventDefault();
    const [item] = state[column].splice(index, 1);
    state[column].splice(index + 1, 0, item);
    saveState();
    render();
    focusCard(cardId);
  }
});

function focusCard(cardId) {
  const el = document.querySelector('[data-card-id="' + cardId + '"]');
  if (el) el.focus();
}

/* ---------------------------------------------------------------- 主题 */
document.querySelector('[data-testid="theme-toggle"]').addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  localStorage.setItem(THEME_KEY, next);
  applyTheme(next);
});

applyTheme(localStorage.getItem(THEME_KEY) || "light");
render();
