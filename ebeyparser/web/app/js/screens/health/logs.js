// Журнал (brief §4.6 logs viewer): level chips, text search, live tail over SSE,
// copy the last 200 lines, download the whole log.
import { html, cx, useState, useEffect, useRef } from "../../lib/html.js";
import { useAsync, useDebounced } from "../../lib/hooks.js";
import { api, authToken, API_BASE } from "../../lib/api.js";
import { Icon, Button, Input, Toggle, EmptyState, ErrorState, Skeleton, toast } from "../../ui/index.js";
import { copyText } from "../../features/messages.js";

const LEVELS = [
  { value: "error", label: "Ошибки", icon: "circle-alert" },
  { value: "warning", label: "Предупреждения", icon: "triangle-alert" },
  { value: "", label: "Всё", icon: "list" },
];
const MAX_LINES = 2000;

export function LogsView() {
  const [level, setLevel] = useState("warning");
  const [text, setText] = useState("");
  const q = useDebounced(text.trim(), 300);
  const [follow, setFollow] = useState(false);
  const [lines, setLines] = useState([]);
  const box = useRef(null);
  const stick = useRef(true);
  const res = useAsync(() => api.get("/logs", { params: { lines: 500, level: level || undefined, q: q || undefined } }), [level, q]);

  useEffect(() => {
    if (res.data) setLines(res.data.items || []);
  }, [res.data]);

  // live tail
  useEffect(() => {
    if (!follow || !("EventSource" in window)) return undefined;
    const params = new URLSearchParams();
    if (level) params.set("level", level);
    if (q) params.set("q", q);
    if (authToken()) params.set("token", authToken());
    const es = new EventSource(`${API_BASE}/logs/stream?${params}`, { withCredentials: true });
    es.addEventListener("log", (ev) => {
      try {
        const entry = JSON.parse(ev.data);
        setLines((prev) => [...prev, entry].slice(-MAX_LINES));
      } catch {
        /* ignore */
      }
    });
    return () => es.close();
  }, [follow, level, q]);

  // keep the view at the bottom while following (unless the user scrolled up)
  useEffect(() => {
    const el = box.current;
    if (el && (stick.current || follow)) el.scrollTop = el.scrollHeight;
  }, [lines, follow]);

  const copy = () => {
    const last = lines.slice(-200).map((l) => `${l.time || ""} ${l.level.toUpperCase()} ${l.logger}: ${l.message}`.trim());
    copyText(last.join("\n")).then((ok) => (ok ? toast.success(`Скопировано строк: ${last.length}`) : toast.error("Не получилось скопировать")));
  };
  const download = api.url("/logs/download", authToken() ? { token: authToken() } : null);

  let body;
  if (res.error && !res.data) body = html`<${ErrorState} error=${res.error} onRetry=${() => res.reload()} compact />`;
  else if (!res.data) body = html`<div class="logbox">${Array.from({ length: 12 }, (_, i) => html`<${Skeleton} key=${i} w=${`${50 + ((i * 37) % 45)}%`} h=${12} />`)}</div>`;
  else if (res.data.exists === false)
    body = html`<${EmptyState} icon="scroll-text" title="Журнала ещё нет" message="Он появляется после запуска проверок." compact />`;
  else if (!lines.length)
    body = html`<${EmptyState}
      icon=${level === "error" ? "circle-check" : "search-x"}
      tone=${level === "error" ? "profit" : "neutral"}
      title=${level === "error" && !q ? "Ошибок нет" : "Ничего не нашлось"}
      message=${q ? "Попробуй другое слово или покажи всё." : "Всё спокойно. Можно посмотреть все записи."}
      action=${html`<${Button} variant="secondary" onClick=${() => (setLevel(""), setText(""))}>Показать всё<//>`}
      compact
    />`;
  else
    body = html`<div
      class="logbox"
      ref=${box}
      role="log"
      aria-live=${follow ? "polite" : "off"}
      onScroll=${(e) => {
        const el = e.currentTarget;
        stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
      }}
    >
      ${lines.map(
        (l, i) => html`<div key=${i} class=${cx("logline", `lv-${l.level}`)}>
          <span class="logline__t">${l.time_text || ""}</span>
          <span class="logline__l">${{ error: "ОШИБКА", critical: "ОШИБКА", warning: "ВНИМАНИЕ", info: "инфо", debug: "debug" }[l.level] || l.level}</span>
          <span class="logline__m">${l.logger && html`<span class="logline__src">${l.logger.replace(/^ebeyparser\./, "")}</span> `}${l.message}</span>
        </div>`,
      )}
    </div>`;

  return html`<section class="logs">
    <div class="logs__bar">
      <div class="chips-row" role="radiogroup" aria-label="Уровень">
        ${LEVELS.map(
          (l) => html`<button type="button" role="radio" aria-checked=${level === l.value} class=${cx("fchip", level === l.value && "is-on")} onClick=${() => setLevel(l.value)}>
            <${Icon} name=${level === l.value ? "check" : l.icon} size=${14} />${l.label}
          </button>`,
        )}
      </div>
      <div class="logs__search"><${Input} value=${text} onChange=${setText} icon="search" placeholder="Найти в журнале" size="sm" type="search" /></div>
      <div class="logs__tools">
        <${Toggle} checked=${follow} onChange=${setFollow} label="Следить" size="sm" />
        <${Button} size="sm" variant="ghost" icon="copy" onClick=${copy} disabled=${!lines.length}>Скопировать<//>
        <a class="btn btn--ghost btn--sm" href=${download} download data-native><${Icon} name="download" size=${16} /><span class="btn__label">Скачать лог</span></a>
      </div>
    </div>
    ${body}
    ${res.data && res.data.truncated && html`<p class="muted small">Показаны последние ${lines.length} записей — весь журнал можно скачать.</p>`}
  </section>`;
}
