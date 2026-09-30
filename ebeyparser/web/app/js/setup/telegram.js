// Telegram, guided (brief §4.1.7): create a bot → paste its key → press Start (chat id is detected
// automatically via /telegram/link + /telegram/detect) → test message. Also used in Settings.
import { html, cx, useEffect, useRef, useState } from "../lib/html.js";
import { api } from "../lib/api.js";
import { LINKS } from "../lib/links.js";
import { Icon, Button, SecretInput, Input, TestResult, CopyButton, Banner, ExternalLink } from "../ui/index.js";

const TOKEN_RE = /^\d{6,12}:[A-Za-z0-9_-]{30,}$/;
const WAIT_SECONDS = 120;

function SubStep({ n, title, state, open, onOpen, children }) {
  return html`<li class=${cx("substep", `is-${state}`, open && "is-open")}>
    <button type="button" class="substep__head" onClick=${onOpen} aria-expanded=${open} disabled=${state === "todo" && !open}>
      <span class="substep__mark">${state === "done" ? html`<${Icon} name="check" size=${14} stroke=${3} />` : n}</span>
      <span class="substep__title">${title}</span>
      ${state === "done" && !open && html`<${Icon} name="chevron-down" size=${16} class="substep__chev" />`}
    </button>
    ${open && html`<div class="substep__body">${children}</div>`}
  </li>`;
}

/**
 * Props: initial = { token_set, chat_set, bot } (resume), onDone({ bot, chat }) after the test succeeds,
 * onLinked({ bot, chat }) when the chat is detected (settings are saved server-side at that moment).
 */
export function TelegramConnect({ initial = {}, onLinked, onDone }) {
  const startAt = initial.token_set && initial.chat_set ? 3 : initial.token_set ? 2 : 0;
  const [active, setActive] = useState(startAt);
  const [done, setDone] = useState(() => new Set(Array.from({ length: startAt }, (_, i) => i)));
  const [token, setToken] = useState("");
  const [bot, setBot] = useState(initial.bot || null);
  const [tokenState, setTokenState] = useState({ state: "idle", message: "" });
  const [link, setLink] = useState(null);
  const [waiting, setWaiting] = useState(false);
  const [left, setLeft] = useState(WAIT_SECONDS);
  const [chat, setChat] = useState(initial.chat || null);
  const [manual, setManual] = useState(false);
  const [manualId, setManualId] = useState("");
  const [linkError, setLinkError] = useState(null);
  const [test, setTest] = useState({ state: "idle" });
  const [arrived, setArrived] = useState(null);
  const timer = useRef(null);

  const complete = (i, next = i + 1) => {
    setDone((d) => new Set([...d, i]));
    setActive(next);
  };

  // 2. validate the token as soon as it looks right
  useEffect(() => {
    const t = token.trim();
    if (!t) return setTokenState({ state: "idle", message: "" });
    if (!TOKEN_RE.test(t)) return setTokenState({ state: "warn", message: "Похоже, скопировалось не всё — ключ выглядит так: 123456789:AA…" });
    let alive = true;
    setTokenState({ state: "loading", message: "Проверяю ключ…" });
    api
      .post("/telegram/validate", { token: t })
      .then((res) => {
        if (!alive) return;
        setBot(res.bot);
        setTokenState({ state: "ok", message: `Бот @${res.bot.username} найден` });
      })
      .catch((e) => alive && setTokenState({ state: "fail", message: e.message, code: e.code }));
    return () => {
      alive = false;
    };
  }, [token]);

  const paste = async () => {
    try {
      setToken((await navigator.clipboard.readText()).trim());
    } catch {
      /* clipboard not allowed: the field stays for manual paste */
    }
  };

  // 3. link code + deep link, then wait for /start <code>
  const makeLink = async () => {
    setLinkError(null);
    try {
      const res = await api.post("/telegram/link", token.trim() ? { token: token.trim() } : {});
      setLink(res);
      if (res.bot) setBot(res.bot);
      return res;
    } catch (e) {
      setLinkError(e);
      return null;
    }
  };
  useEffect(() => {
    if (active === 2 && !link) makeLink();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active]);

  const startWaiting = () => {
    setWaiting(true);
    setLeft(WAIT_SECONDS);
  };
  useEffect(() => {
    if (!waiting || !link) return undefined;
    let tick = 0;
    timer.current = setInterval(async () => {
      tick += 1;
      setLeft((l) => Math.max(0, l - 1));
      if (tick % 2) return; // poll every 2 s
      try {
        const res = await api.get("/telegram/detect", { params: { code: link.code } });
        if (res.found) {
          clearInterval(timer.current);
          setWaiting(false);
          setChat(res.chat);
          onLinked && onLinked({ bot, chat: res.chat });
          complete(2);
        }
      } catch (e) {
        if (e.code === "telegram_webhook" || e.code === "telegram_code_unknown" || e.code === "telegram_code_expired") {
          clearInterval(timer.current);
          setWaiting(false);
          setLinkError(e);
        }
      }
    }, 1000);
    return () => clearInterval(timer.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [waiting, link]);
  useEffect(() => {
    if (waiting && left === 0) {
      setWaiting(false);
      setManual(true);
    }
  }, [left, waiting]);

  const saveManual = async () => {
    try {
      await api.put("/secrets", { telegram_chat_id: manualId.trim() });
      await api.patch("/settings/notifications", { telegram: { enabled: true } });
      const c = { name: "", type_ru: "чат", chat_id_masked: manualId.trim() };
      setChat(c);
      onLinked && onLinked({ bot, chat: c });
      complete(2);
    } catch (e) {
      setLinkError(e);
    }
  };

  const resetWebhook = async () => {
    try {
      await api.post("/telegram/reset-webhook", {});
      setLinkError(null);
      setLink(null);
      await makeLink();
    } catch (e) {
      setLinkError(e);
    }
  };

  // 4. test message
  const sendTest = async () => {
    setTest({ state: "loading" });
    setArrived(null);
    try {
      const res = await api.post("/telegram/test", { with_deal: true }, { timeout: 70000 });
      setTest({ state: "ok", message: res.message_ru || "Тест отправлен — проверь Telegram" });
    } catch (e) {
      setTest({ state: "fail", message: e.message });
    }
  };

  const stateOf = (i) => (done.has(i) ? "done" : i === active ? "active" : "todo");
  const botName = bot && bot.username ? `@${bot.username}` : "своего бота";
  return html`<ol class="substeps">
    <${SubStep} n=${1} title="Создай бота" state=${stateOf(0)} open=${active === 0} onOpen=${() => setActive(0)}>
      <p class="substep__text">Открой <b>@BotFather</b> в Telegram и отправь ему команду <code>/newbot</code>. Придумай любое имя, а username должен заканчиваться на <i>bot</i>, например <i>maks_deals_bot</i>. BotFather пришлёт длинный ключ — скопируй его.</p>
      <div class="row">
        <${Button} variant="primary" icon="send" href=${LINKS.botFather}>Открыть @BotFather<//>
        <${CopyButton} text="/newbot" label="Скопировать /newbot" />
      </div>
      <${Button} variant="ghost" size="sm" iconRight="arrow-right" onClick=${() => complete(0)}>Ключ у меня — дальше<//>
    <//>

    <${SubStep} n=${2} title=${bot && done.has(1) ? `Ключ вставлен · бот @${bot.username}` : "Вставь ключ бота"} state=${stateOf(1)} open=${active === 1} onOpen=${() => setActive(1)}>
      <div class="row row--nowrap">
        <div class="grow">
          <${SecretInput} value=${token} onChange=${setToken} placeholder="123456789:AAH…" saved=${initial.token_set} class="mono" aria-label="Ключ бота" invalid=${tokenState.state === "fail"} />
        </div>
        ${navigator.clipboard && navigator.clipboard.readText && html`<${Button} icon="clipboard-paste" onClick=${paste}>Вставить<//>`}
      </div>
      ${tokenState.state !== "idle" && html`<${TestResult} state=${tokenState.state} title=${tokenState.state === "ok" ? `✓ ${tokenState.message}` : tokenState.message} detail=${tokenState.state === "fail" && tokenState.code === "telegram_token_invalid" ? "Скопируй ключ у @BotFather ещё раз — целиком, без пробелов" : tokenState.state === "fail" && tokenState.code === "network" ? "Telegram открывается на этом компьютере? Проверь интернет и VPN" : ""} />`}
      <${Button} variant="primary" iconRight="arrow-right" disabled=${tokenState.state !== "ok" && !(initial.token_set && !token)} onClick=${() => complete(1)}>Дальше<//>
    <//>

    <${SubStep} n=${3} title=${chat && done.has(2) ? `Нашёл тебя: ${chat.name || chat.username || "чат"}` : "Напиши боту"} state=${stateOf(2)} open=${active === 2} onOpen=${() => setActive(2)}>
      ${linkError && html`<${Banner} tone="red" action=${linkError.code === "telegram_webhook" ? html`<${Button} size="sm" onClick=${resetWebhook}>Сбросить webhook бота<//>` : html`<${Button} size="sm" onClick=${makeLink}>Ещё раз<//>`}>${linkError.message}<//>`}
      ${!manual &&
      html`<p class="substep__text">Нажми кнопку — откроется чат с ${botName}. Нажми там <b>Start</b>, и я сам найду тебя.</p>
        <div class="row">
          <${Button} variant="primary" icon="send" href=${link ? link.deep_link : undefined} disabled=${!link} onClick=${startWaiting}>Открыть бота и нажать Start<//>
          <${Button} variant="ghost" size="sm" onClick=${() => setManual(true)}>Не получается<//>
        </div>
        ${waiting &&
        html`<div class="waiting" role="status">
          <span class="waiting__dots"><i></i><i></i><i></i></span>
          Жду твоё сообщение… <span class="num">(${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")})</span>
        </div>`}`}
      ${manual &&
      html`<div class="stack" style=${{ "--gap": "10px" }}>
        <p class="substep__text">Не дождался сообщения. Нажми <b>Start</b> в чате с ботом — или введи chat_id вручную. Узнать его можно у <${ExternalLink} href=${LINKS.telegramBot("userinfobot")}>@userinfobot<//>.</p>
        <div class="row row--nowrap">
          <div class="grow"><${Input} value=${manualId} onChange=${setManualId} placeholder="123456789" inputmode="numeric" aria-label="chat_id" /></div>
          <${Button} variant="primary" disabled=${!/^-?\d{3,}$/.test(manualId.trim())} onClick=${saveManual}>Сохранить<//>
        </div>
        <${Button} variant="ghost" size="sm" icon="rotate-ccw" onClick=${() => {
          setManual(false);
          startWaiting();
        }}>Попробовать ещё раз автоматически<//>
      </div>`}
    <//>

    <${SubStep} n=${4} title="Проверим" state=${stateOf(3)} open=${active === 3} onOpen=${() => setActive(3)}>
      <p class="substep__text">Пришлю пример находки с фото — так будут выглядеть уведомления.</p>
      <${Button} variant=${test.state === "ok" ? "secondary" : "primary"} icon="send" loading=${test.state === "loading"} onClick=${sendTest}>${test.state === "ok" ? "Отправить ещё раз" : "Отправить тестовое сообщение"}<//>
      ${test.state === "fail" && html`<${TestResult} state="fail" title=${test.message} />`}
      ${test.state === "ok" &&
      html`<div class="arrived">
        <div class="arrived__q">Пришло сообщение?</div>
        <div class="row">
          <${Button} variant=${arrived === true ? "primary" : "secondary"} onClick=${() => {
            setArrived(true);
            setDone((d) => new Set([...d, 3]));
            onDone && onDone({ bot, chat });
          }}>Да, пришло 🎉<//>
          <${Button} variant="ghost" onClick=${() => setArrived(false)}>Нет<//>
        </div>
        ${arrived === true && html`<${TestResult} state="ok" title="Готово! Уведомления будут приходить сюда" />`}
        ${arrived === false &&
        html`<ul class="tips">
          <li>Проверь, что уведомления Telegram для этого бота включены и чат не в «Без звука».</li>
          <li>На Android отключи экономию батареи для Telegram (Настройки → Приложения → Telegram → Батарея).</li>
          <li>Открой чат с ботом — сообщение могло прийти без звука.</li>
        </ul>`}
      </div>`}
    <//>
  </ol>`;
}
