// web-001 参考解 B：React 函数组件实现（思路与 A 不同：声明式状态 + hooks）。
// 与 A 共享同一 DOM 契约（data-testid / data-column / data-card-id），
// 使用与原生实现相同的「拖到卡片前插入、拖到列区域末尾追加」语义。
//
// 运行（需 Node 工具链；题包 answers/ 只交付组件源码）：
//   npm create vite@latest kanban -- --template react
//   把本文件内容覆盖 src/App.jsx，再 npm run dev
import { useCallback, useEffect, useRef, useState } from "react";

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
const NEXT = { todo: "doing", doing: "done", done: null };
const PREV = { todo: null, doing: "todo", done: "doing" };
const STORAGE_KEY = "kanban-state-v1";
const THEME_KEY = "kanban-theme";

function loadState() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (COLUMNS.every((column) => Array.isArray(parsed[column]))) return parsed;
    }
  } catch (error) {
    /* 忽略损坏数据 */
  }
  return JSON.parse(JSON.stringify(SEED));
}

function initialTheme() {
  return localStorage.getItem(THEME_KEY) === "dark" ? "dark" : "light";
}

export default function App() {
  const [state, setState] = useState(loadState);
  const [theme, setTheme] = useState(initialTheme);
  const dragRef = useRef(null);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  const persist = useCallback((next) => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
  }, []);

  const moveCard = useCallback(
    (cardId, targetColumn, targetCardId) => {
      setState((current) => {
        let card = null;
        const next = {};
        for (const column of COLUMNS) {
          next[column] = current[column].filter((item) => {
            if (item.id === cardId) {
              card = item;
              return false;
            }
            return true;
          });
        }
        if (!card) return current;
        const list = next[targetColumn];
        const index = targetCardId
          ? list.findIndex((item) => item.id === targetCardId)
          : -1;
        if (index >= 0) list.splice(index, 0, card);
        else list.push(card);
        persist(next);
        return next;
      });
    },
    [persist]
  );

  useEffect(() => {
    const onPointerUp = (event) => {
      const dragged = dragRef.current;
      if (!dragged) return;
      dragRef.current = null;
      const hit = document.elementFromPoint(event.clientX, event.clientY);
      const targetCard = hit && hit.closest(".card");
      const column = hit && hit.closest("[data-column]");
      if (targetCard) {
        moveCard(dragged, targetCard.closest("[data-column]").dataset.column, targetCard.dataset.cardId);
      } else if (column) {
        moveCard(dragged, column.dataset.column, null);
      }
    };
    document.addEventListener("pointerup", onPointerUp);
    return () => document.removeEventListener("pointerup", onPointerUp);
  }, [moveCard]);

  const onCardKeyDown = (event, cardId, column) => {
    const items = state[column];
    const index = items.findIndex((item) => item.id === cardId);
    if (event.key === "ArrowRight" && NEXT[column]) {
      event.preventDefault();
      moveCard(cardId, NEXT[column], null);
    } else if (event.key === "ArrowLeft" && PREV[column]) {
      event.preventDefault();
      moveCard(cardId, PREV[column], null);
    } else if (event.key === "ArrowUp" && index > 0) {
      event.preventDefault();
      const ids = items.map((item) => item.id);
      [ids[index - 1], ids[index]] = [ids[index], ids[index - 1]];
      const reordered = ids.map((id) => items.find((item) => item.id === id));
      setState((current) => {
        const next = { ...current, [column]: reordered };
        persist(next);
        return next;
      });
    } else if (event.key === "ArrowDown" && index < items.length - 1) {
      event.preventDefault();
      const ids = items.map((item) => item.id);
      [ids[index + 1], ids[index]] = [ids[index], ids[index + 1]];
      const reordered = ids.map((id) => items.find((item) => item.id === id));
      setState((current) => {
        const next = { ...current, [column]: reordered };
        persist(next);
        return next;
      });
    }
  };

  const toggleTheme = () => {
    const next = theme === "dark" ? "light" : "dark";
    localStorage.setItem(THEME_KEY, next);
    setTheme(next);
  };

  return (
    <>
      <header className="topbar">
        <h1>团队看板</h1>
        <div className="topbar__actions">
          <span className="hint">拖动卡片可在列内排序、跨列移动</span>
          <button
            data-testid="theme-toggle"
            className="btn"
            type="button"
            aria-label="切换主题"
            onClick={toggleTheme}
          >
            {theme === "dark" ? "切换到浅色" : "切换到暗色"}
          </button>
        </div>
      </header>
      <main className="board" data-testid="board">
        {COLUMNS.map((column) => (
          <section
            key={column}
            className="column"
            data-column={column}
            data-testid={"column-" + column}
            aria-label={column}
          >
            <header className="column__header">
              <h2>{column}</h2>
              <span className="column__count" data-testid={"card-count-" + column}>
                {state[column].length}
              </span>
            </header>
            <ul className="column__list" data-testid={"list-" + column} role="list">
              {state[column].map((card) => (
                <li
                  key={card.id}
                  className="card"
                  data-card-id={card.id}
                  data-dragging="false"
                  tabIndex={0}
                  onPointerDown={(event) => {
                    if (event.button !== 0) return;
                    dragRef.current = card.id;
                    event.currentTarget.dataset.dragging = "true";
                  }}
                  onKeyDown={(event) => onCardKeyDown(event, card.id, column)}
                >
                  <p className="card__title">{card.title}</p>
                  <p className="card__meta">{card.meta}</p>
                </li>
              ))}
            </ul>
            <p className="empty" data-testid={"empty-state-" + column} hidden={state[column].length !== 0}>
              这一列还没有卡片
            </p>
          </section>
        ))}
      </main>
    </>
  );
}
