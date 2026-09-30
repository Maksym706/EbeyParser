// Settings → Уведомления, eBay, Доступ с телефона, Данные и бэкап, О программе (brief §4.7.4–4.7.8).
import { html, cx, useEffect, useState } from "../../lib/html.js";
import { api } from "../../lib/api.js";
import { navigate } from "../../lib/router.js";
import { loadApp } from "../../lib/app.js";
import { appStore, useStore } from "../../lib/store.js";
import { themeStore, setTheme } from "../../lib/theme.js";
import { bytes, number, count } from "../../lib/format.js";
import { useDebounced } from "../../lib/hooks.js";
import { LINKS } from "../../lib/links.js";
import {
  Icon,
  Button,
  Toggle,
  NumberInput,
  Select,
  Segmented,
  Slider,
  SettingRow,
  Chip,
  TestResult,
  Banner,
  KeyValue,
  Skeleton,
  ChoiceCards,
  QrCode,
  CopyButton,
  ChipInput,
  Modal,
  Input,
  ExternalLink,
  confirm,
  toast,
  Checkbox,
} from "../../ui/index.js";
import { TelegramConnect } from "../../setup/telegram.js";
import { EmailConnect, EbayConnect } from "../../setup/mail-ebay.js";
import { Group } from "./form.js";
import { AlertTiersGroups } from "./scout.js";

// ============================================================ 4. Уведомления
function ChannelCard({ icon, title, connected, detail, children }) {
  return html`<div class=${cx("channel", connected && "is-on")}>
    <span class=${cx("glyph", connected ? "glyph--green" : "glyph--neutral")}><${Icon} name=${icon} size=${20} /></span>
    <div class="channel__main">
      <div class="channel__title">${title}${connected ? html`<span class="badge badge--green badge--soft badge--sm">подключён</span>` : html`<span class="badge badge--neutral badge--soft badge--sm">не подключён</span>`}</div>
      ${detail && html`<div class="channel__detail">${detail}</div>`}
    </div>
    <div class="channel__actions">${children}</div>
  </div>`;
}

export function NotificationsSection({ form }) {
  const d = form.draft;
  const n = d.notifications;
  const s = form.settings;
  const secrets = s.secrets || {};
  const channels = s.channels || {};
  const tgOn = channels.telegram && channels.telegram.enabled && !(channels.telegram.missing || []).length;
  const mailOn = channels.email && channels.email.enabled && !(channels.email.missing || []).length;
  const [flow, setFlow] = useState(null); // "telegram" | "email"
  const [test, setTest] = useState({});
  const [preview, setPreview] = useState(null);
  const key = useDebounced(`${n.min_score}|${(n.verdicts || []).join(",")}`, 300);

  useEffect(() => {
    api
      .get("/notify/preview", { params: { min_score: n.min_score, verdicts: (n.verdicts || []).join(",") } })
      .then(setPreview)
      .catch(() => setPreview(null));
  }, [key]);

  const sendTest = async (channel) => {
    setTest({ [channel]: { state: "loading" } });
    try {
      const res = await api.post("/notify/test", { channel }, { timeout: 70000, params: { channel } });
      setTest({ [channel]: { state: "ok", title: channel === "telegram" ? "Тест отправлен — проверь Telegram" : (res.results && Object.values(res.results)[0] && Object.values(res.results)[0].message_ru) || "Письмо отправлено" } });
    } catch (e) {
      setTest({ [channel]: { state: "fail", title: e.message, details: e.details } });
    }
  };
  const disable = async (channel) => {
    try {
      const res = await api.patch("/settings/notifications", { [channel]: { enabled: false } });
      form.setSettings(res);
      toast.success(channel === "telegram" ? "Telegram отключён" : "Почта отключена");
    } catch (e) {
      toast.error(e.message);
    }
  };
  const toggleVerdict = (v) => {
    const cur = n.verdicts || [];
    form.set("notifications.verdicts", cur.includes(v) ? cur.filter((x) => x !== v) : [...cur, v]);
  };

  return html`
    <${Group} title="Куда присылать" description="Telegram — самый быстрый способ: сообщение с фото прямо в телефон." icon="send">
      <${ChannelCard} icon="send" title="Telegram" connected=${tgOn} detail=${secrets.telegram_chat_id && secrets.telegram_chat_id.set ? `Чат ${secrets.telegram_chat_id.masked}` : "Бот пришлёт находку за секунды"}>
        ${tgOn && html`<${Button} size="sm" icon="send" loading=${test.telegram && test.telegram.state === "loading"} onClick=${() => sendTest("telegram")}>Тест<//>`}
        <${Button} size="sm" variant=${tgOn ? "ghost" : "primary"} onClick=${() => setFlow("telegram")}>${tgOn ? "Переподключить" : "Подключить"}<//>
        ${tgOn && html`<${Button} size="sm" variant="danger-ghost" onClick=${() => disable("telegram")}>Отключить<//>`}
      <//>
      ${test.telegram && test.telegram.state !== "loading" && html`<${TestResult} state=${test.telegram.state} title=${test.telegram.title} details=${test.telegram.details} />`}
      <${ChannelCard} icon="mail" title="Почта" connected=${mailOn} detail=${secrets.notify_email && secrets.notify_email.set ? `На ${secrets.notify_email.masked}` : "Письмо с фото и ценой"}>
        ${mailOn && html`<${Button} size="sm" icon="mail" loading=${test.email && test.email.state === "loading"} onClick=${() => sendTest("email")}>Тест<//>`}
        <${Button} size="sm" variant=${mailOn ? "ghost" : "secondary"} onClick=${() => setFlow("email")}>${mailOn ? "Изменить" : "Подключить"}<//>
        ${mailOn && html`<${Button} size="sm" variant="danger-ghost" onClick=${() => disable("email")}>Отключить<//>`}
      <//>
      ${test.email && test.email.state !== "loading" && html`<${TestResult} state=${test.email.state} title=${test.email.title} details=${test.email.details} />`}
    <//>

    <${Group} title="Что присылать" icon="list-filter">
      <${SettingRow} label="Какие находки" help="«Подумай» — больше уведомлений, но и больше лишних" error=${form.err("notifications.verdicts")}>
        <div class="row" style=${{ "--gap": "6px" }}>
          <${Chip} selected=${(n.verdicts || []).includes("buy")} onClick=${() => toggleVerdict("buy")}>Покупай<//>
          <${Chip} selected=${(n.verdicts || []).includes("maybe")} onClick=${() => toggleVerdict("maybe")}>Подумай<//>
        </div>
      <//>
      <${SettingRow} label="Минимальный балл" wide help=${preview ? preview.message_ru : "Чем выше — тем меньше, но точнее уведомления"}>
        <${Slider} value=${n.min_score} min=${0} max=${100} step=${5} bubble="always" tone="green" label="Минимальный балл" format=${(v) => `${v} из 100`} onChange=${(v) => form.set("notifications.min_score", v)} />
      <//>
      <${SettingRow} label="Как присылать">
        <${Segmented}
          value=${n.mode}
          onChange=${(v) => form.set("notifications.mode", v)}
          options=${[
            { value: "instant", label: "Сразу" },
            { value: "digest", label: "Сводкой" },
          ]}
          block
        />
      <//>
      <${SettingRow} label="Не больше в час" help="Остальное придёт одним сообщением-сводкой" error=${form.err("notifications.max_alerts_per_hour")}>
        ${(id) => html`<${NumberInput} id=${id} ...${form.bind("notifications.max_alerts_per_hour")} min=${0} max=${100} />`}
      <//>
      <${SettingRow} kind="switch" label="Присылать, даже если фото не проверены" help="Когда нейросеть недоступна — с пометкой ⚠">
        ${(id) => html`<${Toggle} id=${id} checked=${n.unchecked_deals} onChange=${(v) => form.set("notifications.unchecked_deals", v)} />`}
      <//>
    <//>

    <${AlertTiersGroups} form=${form} />

    <${Group} title="Служебные сообщения" icon="heart-pulse">
      <${SettingRow} kind="switch" label="Сообщать о проблемах" help="Нейросеть упала, сайт заблокировал, парсинг сломался">
        ${(id) => html`<${Toggle} id=${id} checked=${n.health_alerts} onChange=${(v) => form.set("notifications.health_alerts", v)} />`}
      <//>
      <${SettingRow} label="Утренний отчёт «я жив»" help="Нет отчёта — значит, компьютер или программа выключены">
        ${(id) => html`<${Select}
          id=${id}
          value=${n.heartbeat_hour == null ? "off" : String(n.heartbeat_hour)}
          onChange=${(v) => form.set("notifications.heartbeat_hour", v === "off" ? null : Number(v))}
          options=${[{ value: "off", label: "Не присылать" }, ...Array.from({ length: 24 }, (_, h) => ({ value: String(h), label: `в ${String(h).padStart(2, "0")}:00` }))]}
        />`}
      <//>
    <//>

    <${Modal} open=${flow === "telegram"} onClose=${() => setFlow(null)} title="Подключить Telegram" icon="send" size="md">
      <${TelegramConnect}
        initial=${{ token_set: secrets.telegram_bot_token && secrets.telegram_bot_token.set }}
        onLinked=${() => form.reload()}
        onDone=${() => {
          form.reload();
          loadApp().catch(() => {});
        }}
      />
    <//>
    <${Modal} open=${flow === "email"} onClose=${() => setFlow(null)} title="Уведомления на почту" icon="mail" size="md">
      <${EmailConnect}
        initial=${{ password_set: secrets.smtp_password && secrets.smtp_password.set }}
        onDone=${() => {
          form.reload();
          setTimeout(() => setFlow(null), 1200);
        }}
      />
    <//>
  `;
}

// ============================================================ 5. eBay
export function EbaySection({ form }) {
  const s = form.settings;
  const secrets = s.secrets || {};
  const configured = Boolean(s.ebay && s.ebay.configured);
  const d = form.draft.ebay;
  return html`
    <${Group} title="Ключи eBay" description="Аукционы и «Sofort-Kaufen» с eBay.de через официальный API — бесплатно." icon="key-round">
      ${configured && html`<${TestResult} state="ok" title="Ключи сохранены" detail=${secrets.ebay_client_id && secrets.ebay_client_id.masked ? `App ID ${secrets.ebay_client_id.masked}` : ""} />`}
      <${EbayConnect}
        initial=${{ configured, client_id_set: secrets.ebay_client_id && secrets.ebay_client_id.set, client_secret_set: secrets.ebay_client_secret && secrets.ebay_client_secret.set }}
        onDone=${() => form.reload()}
      />
    <//>
    <${Group} title="Где искать на eBay" icon="globe">
      <${SettingRow} label="Площадка">
        ${(id) => html`<${Select}
          id=${id}
          value=${d.marketplace_id}
          onChange=${(v) => form.set("ebay.marketplace_id", v)}
          options=${[
            { value: "EBAY_DE", label: "eBay.de (Германия)" },
            { value: "EBAY_AT", label: "eBay.at (Австрия)" },
            { value: "EBAY_GB", label: "eBay.co.uk" },
          ]}
        />`}
      <//>
      <${SettingRow} kind="switch" label="Искать только в Германии" help="Товары, которые находятся в DE — без долгой доставки и таможни">
        ${(id) => html`<${Toggle} id=${id} checked=${d.item_location_country === "DE"} onChange=${(v) => form.set("ebay.item_location_country", v ? "DE" : "")} />`}
      <//>
    <//>
  `;
}

// ============================================================ 6. Доступ с телефона
const MODES = [
  { value: "local", label: "Только этот компьютер", description: "Самый безопасный вариант. С телефона не открыть.", icon: "monitor" },
  { value: "tailscale", label: "Через Tailscale", description: "Рекомендуем: телефон и компьютер в личной сети, работает везде.", icon: "shield-check" },
  { value: "lan", label: "Домашний Wi-Fi", description: "Любой в этой сети со ссылкой сможет открыть панель.", icon: "wifi" },
];

export function AccessSection() {
  const [access, setAccess] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [hosts, setHosts] = useState(null);
  const load = () =>
    api.get("/access").then(
      (a) => {
        setAccess(a);
        setHosts(a.allowed_hosts || []);
      },
      (e) => setError(e),
    );
  useEffect(() => {
    load();
  }, []);
  if (error) return html`<${Banner} tone="red" details=${error.details}>${error.message}<//>`;
  if (!access) return html`<div class="sgroup"><${Skeleton} h=${120} radius="16px" /></div>`;

  const changeMode = async (mode) => {
    if (mode === access.mode) return;
    const lan = mode === "lan";
    const ok = await confirm({
      title: lan ? "Открыть панель для всей домашней сети?" : mode === "local" ? "Закрыть доступ с телефона?" : "Открыть доступ через Tailscale?",
      message: lan
        ? "Любой в этом Wi-Fi со ссылкой сможет её открыть. Не делай так в общежитии или кафе."
        : mode === "local"
          ? "Панель будет открываться только на этом компьютере."
          : "Панель откроется на телефоне с Tailscale по ссылке с ключом.",
      confirmLabel: lan ? "Открыть для сети" : mode === "local" ? "Только этот компьютер" : "Включить Tailscale",
      tone: lan ? "danger" : "primary",
      icon: MODES.find((m) => m.value === mode).icon,
    });
    if (!ok) return;
    setBusy(true);
    try {
      setAccess(await api.put("/access", { mode }));
      toast.success("Сохранено — нужно перезапустить программу");
    } catch (e) {
      toast.error(e.message);
    } finally {
      setBusy(false);
    }
  };
  const rotate = async () => {
    const ok = await confirm({ title: "Сменить ключ доступа?", message: "Все телефоны нужно будет подключить заново — по новой ссылке или QR-коду.", confirmLabel: "Сменить ключ", tone: "danger", icon: "key-round" });
    if (!ok) return;
    try {
      setAccess(await api.post("/access/rotate-token", {}));
      toast.success("Новый ключ создан");
    } catch (e) {
      toast.error(e.message);
    }
  };
  const restart = async () => {
    try {
      const res = await api.post("/system/restart", {});
      toast.info(res.message_ru || "Перезапускаю…");
    } catch (e) {
      toast.error(e.message);
    }
  };
  const saveHosts = async () => {
    try {
      setAccess(await api.put("/access", { mode: access.mode === "custom" ? "tailscale" : access.mode, allowed_hosts: hosts }));
      toast.success("Сохранено");
    } catch (e) {
      toast.error(e.message);
    }
  };
  const url = access.qr_payload;
  return html`
    ${access.restart_required &&
    html`<${Banner} tone="amber" title="Нужен перезапуск" action=${html`<${Button} size="sm" icon="refresh-cw" onClick=${restart}>Перезапустить<//>`}>
      Новый режим заработает после перезапуска программы.
    <//>`}
    <${Group} title="Откуда можно открыть панель" icon="smartphone">
      <${ChoiceCards} value=${access.mode === "custom" ? "tailscale" : access.mode} onChange=${changeMode} options=${MODES.map((m) => ({ ...m, tone: m.value === "lan" ? "amber" : undefined }))} columns=${3} />
      ${busy && html`<p class="muted-line">Сохраняю…</p>`}
      ${access.mode === "tailscale" && !access.tailscale_ips.length && html`<p class="muted-line">Tailscale не найден на этом компьютере — <${ExternalLink} href=${LINKS.tailscale}>установи его<//> на компьютер и телефон.</p>`}
    <//>
    ${access.mode !== "local" &&
    html`<${Group} title="Ссылка для телефона" description="Отсканируй QR-код камерой телефона — ключ запомнится, вводить ничего не нужно." icon="qr-code">
      ${url
        ? html`<div class="phone-link">
            <${QrCode} text=${url} size=${184} label="QR-код ссылки для телефона" />
            <div class="phone-link__side">
              ${(access.urls || []).map(
                (u) => html`<div class="phone-link__url" key=${u.url}>
                  <span class="phone-link__label">${u.label}</span>
                  <code class="phone-link__code">${u.url.replace(/token=[^&]+/, "token=••••")}</code>
                  <${CopyButton} text=${u.url} label="Скопировать ссылку" />
                </div>`,
              )}
              <${Button} variant="danger-ghost" size="sm" icon="key-round" onClick=${rotate}>Сменить ключ доступа<//>
            </div>
          </div>`
        : html`<p class="muted-line">Ссылка появится после перезапуска программы.</p>`}
    <//>`}
    <${Group} title="Дополнительные имена" description="Если открываешь панель по имени компьютера, например my-pc.tail1234.ts.net." icon="globe">
      <${ChipInput} value=${hosts || []} onChange=${setHosts} placeholder="имя компьютера" />
      ${JSON.stringify(hosts) !== JSON.stringify(access.allowed_hosts || []) && html`<div class="row mt-4"><${Button} size="sm" variant="primary" onClick=${saveHosts}>Сохранить имена<//></div>`}
    <//>
  `;
}

// ============================================================ 7. Данные и бэкап
function TypedConfirm({ open, onClose, title, message, word, action, onConfirm }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => setText(""), [open]);
  return html`<${Modal}
    open=${open}
    onClose=${onClose}
    title=${title}
    icon="triangle-alert"
    size="sm"
    footer=${html`<${Button} variant="ghost" onClick=${onClose}>Отмена<//>
      <${Button}
        variant="danger"
        loading=${busy}
        disabled=${text.trim().toLowerCase() !== word}
        onClick=${async () => {
          setBusy(true);
          await onConfirm();
          setBusy(false);
          onClose();
        }}
        >${action}<//
      >`}
  >
    <p class="confirm__message" style=${{ textAlign: "left", marginBottom: "16px" }}>${message}</p>
    <label class="mini-field">
      <span class="mini-field__label">Напиши «${word}», чтобы подтвердить</span>
      <${Input} value=${text} onChange=${setText} placeholder=${word} data-autofocus />
    </label>
  <//>`;
}

export function DataSection() {
  const app = useStore(appStore, (s) => s.app);
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [withKeys, setWithKeys] = useState(false);
  const [dialog, setDialog] = useState(null);
  const load = () => api.get("/data").then(setData, setError);
  useEffect(() => {
    load();
  }, []);
  const run = async (path, word, done) => {
    try {
      await api.post(path, { confirm: word });
      toast.success(done);
      load();
      loadApp().catch(() => {});
    } catch (e) {
      toast.error(e.message);
    }
  };
  const removeDemo = async () => {
    try {
      await api.del("/demo");
      toast.success("Демо-данные убраны");
      load();
      loadApp().catch(() => {});
    } catch (e) {
      toast.error(e.message);
    }
  };
  const rerun = async () => {
    const ok = await confirm({ title: "Запустить мастер настройки заново?", message: "Твои поиски и находки останутся. В конце мастер спросит, заменить поиски новыми или добавить к текущим.", confirmLabel: "Открыть мастер", icon: "wand-sparkles" });
    if (!ok) return;
    await api.post("/onboarding/complete", { completed: false }).catch(() => {});
    await loadApp().catch(() => {});
    navigate("/welcome");
  };
  const restartApp = async () => {
    const ok = await confirm({ title: "Перезапустить программу?", message: "Страница переподключится сама через несколько секунд.", confirmLabel: "Перезапустить", icon: "refresh-cw" });
    if (!ok) return;
    try {
      const res = await api.post("/system/restart", {});
      toast.info(res.message_ru || "Перезапускаю… страница переподключится сама");
    } catch (e) {
      toast.error(e);
    }
  };
  if (error && !data) return html`<${Banner} tone="red" details=${error.details}>${error.message}<//>`;
  const c = (data && data.counts) || {};
  const demoCount = (data && data.demo && data.demo.count) || (app && app.demo && app.demo.count) || 0;
  return html`
    ${demoCount > 0 &&
    html`<${Banner} tone="violet" icon="flask-conical" title="Показаны демо-данные" action=${html`<${Button} size="sm" onClick=${removeDemo}>Убрать демо<//>`}>
      ${count(demoCount, "пример", "примера", "примеров")} сделок для знакомства с интерфейсом.
    <//>`}
    <${Group} title="База данных" icon="database">
      ${!data
        ? html`<${Skeleton} count=${3} />`
        : html`<div class="data-stats">
            <div class="data-stat"><span>Размер базы</span><b class="num">${bytes((data.db_bytes || 0) + (data.wal_bytes || 0))}</b></div>
            <div class="data-stat"><span>Объявлений</span><b class="num">${number(c.listings)}</b></div>
            <div class="data-stat"><span>Цен в истории</span><b class="num">${number(c.price_points)}</b></div>
            <div class="data-stat"><span>Проверок</span><b class="num">${number(c.runs)}</b></div>
          </div>
          <p class="muted-line">Объявления хранятся ${data.listing_retention_days || 60} дней · свободно на диске ${bytes(data.free_bytes)}</p>`}
    <//>
    <${Group} title="Резервная копия" description="База и настройки одним zip-файлом." icon="hard-drive-download">
      <${Checkbox} checked=${withKeys} onChange=${setWithKeys} label="Включить ключи и пароли" description="Файл с ключами храни в надёжном месте — в нём токен Telegram и пароли" />
      <div class="row">
        <${Button} variant="primary" icon="download" href=${api.url("/backup", withKeys ? { include_secrets: 1 } : null)} download>Скачать резервную копию<//>
        <${Button} variant="secondary" icon="file-text" href=${api.url("/logs/download")} download>Скачать лог<//>
      </div>
    <//>
    <${Group} title="Опасная зона" icon="triangle-alert" tone="red">
      <${SettingRow} label="Сбросить историю цен" help="Оценки станут менее точными, пока программа снова не накопит цены (несколько дней)">
        <${Button} variant="danger-ghost" onClick=${() => setDialog("history")}>Сбросить<//>
      <//>
      <${SettingRow} label="Удалить все данные" help="Объявления, сделки и история. Настройки и ключи останутся">
        <${Button} variant="danger-ghost" onClick=${() => setDialog("all")}>Удалить<//>
      <//>
      <${SettingRow} label="Мастер настройки" help="Пройти первые шаги заново: город, категории, деньги">
        <${Button} variant="secondary" icon="wand-sparkles" onClick=${rerun}>Запустить заново<//>
      <//>
    <//>
    ${app &&
    app.features &&
    app.features.restart &&
    html`<${Group} title="Перезапуск" icon="refresh-cw">
      <${SettingRow} label="Перезапустить программу" help="Если проверки выключены или что-то зависло — перезапуск всё включит заново. Настройки и данные сохранятся.">
        <${Button} variant="secondary" icon="refresh-cw" onClick=${restartApp}>Перезапустить<//>
      <//>
    <//>`}
    ${data &&
    html`<details class="guide">
      <summary>Для продвинутых</summary>
      <div class="guide__body">
        <${KeyValue}
          rows=${[
            ["Папка программы", html`<code>${folderOf(data.config_path || data.db_path)}</code>`],
            ["База", html`<code>${data.db_path}</code>`],
            ["Журнал", html`<code>${data.log_path}</code>`],
            ["Версия", data.version],
          ]}
        />
      </div>
    </details>`}
    <${TypedConfirm}
      open=${dialog === "history"}
      onClose=${() => setDialog(null)}
      title="Сбросить историю цен?"
      message="Оценки станут менее точными, пока программа снова не накопит цены (несколько дней)."
      word="сбросить"
      action="Сбросить историю"
      onConfirm=${() => run("/data/reset-history", "сбросить", "История цен сброшена")}
    />
    <${TypedConfirm}
      open=${dialog === "all"}
      onClose=${() => setDialog(null)}
      title="Удалить все данные?"
      message="Удалю все объявления, сделки и историю цен. Настройки, поиски и ключи останутся."
      word="удалить"
      action="Удалить всё"
      onConfirm=${() => run("/data/reset-all", "удалить", "Данные удалены")}
    />
  `;
}

/** "/home/me/ebp/settings-file" → "/home/me/ebp": the UI shows the folder, never the settings file name (§2.1). */
function folderOf(path) {
  const p = String(path || "");
  const i = Math.max(p.lastIndexOf("/"), p.lastIndexOf("\\"));
  return i > 0 ? p.slice(0, i) : p || "—";
}

// ============================================================ 8. О программе
export function AboutSection() {
  const app = useStore(appStore, (s) => s.app);
  const { pref } = useStore(themeStore);
  return html`
    <${Group} title="Оформление" icon="sun">
      <${SettingRow} label="Тема" wide>
        <${Segmented}
          value=${pref}
          onChange=${setTheme}
          block
          options=${[
            { value: "system", label: "Как в системе", icon: "monitor" },
            { value: "light", label: "Светлая", icon: "sun" },
            { value: "dark", label: "Тёмная", icon: "moon" },
          ]}
        />
      <//>
    <//>
    <${Group} title="EbeyParser" icon="info">
      <${KeyValue} rows=${[["Версия", app && app.version ? app.version : "—"]]} />
      <details class="guide">
        <summary>Для продвинутых</summary>
        <div class="guide__body">
          <${KeyValue} rows=${[["Старый интерфейс", html`<a href="/classic" data-native>Открыть классический вид</a>`]]} />
        </div>
      </details>
    <//>
    <${Group} title="Открытые библиотеки" description="Всё встроено в программу и работает без интернета." icon="book-open">
      <ul class="licenses">
        <li><b>Preact</b> · MIT</li>
        <li><b>htm</b> · Apache 2.0</li>
        <li><b>Lucide</b> (иконки) · ISC</li>
        <li><b>Inter</b> (шрифт) · SIL Open Font License 1.1</li>
        <li><b>uqr</b> (QR-коды) · MIT</li>
      </ul>
    <//>
  `;
}
