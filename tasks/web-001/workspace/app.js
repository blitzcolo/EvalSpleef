// 看板起始代码：能渲染，但交互基本没做。
// 约定：不要改 data-testid / data-column / data-card-id 这些钩子（验收依赖）。
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

const state = {
  todo: [...SEED.todo],
  doing: [...SEED.doing],
  done: [...SEED.done],
};

function renderCard(card) {
  const li = document.createElement("li");
  li.className = "card";
  li.dataset.cardId = card.id;
  li.dataset.dragging = "false";
  li.tabIndex = 0;
  li.innerHTML =
    '<p class="card__title"></p><p class="card__meta"></p>';
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
    empty.hidden = true; // TODO: 空列时才显示
  }
}

// 拖拽：目前只做「按下时半透明」，松手不落位、不保存
let dragging = null;

document.addEventListener("pointerdown", (event) => {
  const card = event.target.closest(".card");
  if (!card) return;
  dragging = card;
  card.dataset.dragging = "true";
});

document.addEventListener("pointerup", () => {
  if (!dragging) return;
  dragging.dataset.dragging = "false";
  dragging = null; // TODO: 找到落点、更新 state、保存、重渲染
});

// 主题切换：按钮存在但没接逻辑
document.querySelector('[data-testid="theme-toggle"]').addEventListener("click", () => {
  // TODO: 切换 data-theme、保存到 localStorage
});

render();
