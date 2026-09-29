// E-mail (Gmail app password mini-guide, POST /email/test) and eBay keys (POST /ebay/test).
import { html, useState } from "../lib/html.js";
import { api } from "../lib/api.js";
import { LINKS } from "../lib/links.js";
import { Button, Field, Input, SecretInput, TestResult, ExternalLink, Select, NumberInput, HelpTip } from "../ui/index.js";

const MAIL_PRESETS = {
  gmail: { label: "Gmail", smtp_host: "smtp.gmail.com", smtp_port: 587 },
  gmx: { label: "GMX", smtp_host: "mail.gmx.net", smtp_port: 587 },
  webde: { label: "WEB.DE", smtp_host: "smtp.web.de", smtp_port: 587 },
  outlook: { label: "Outlook / Hotmail", smtp_host: "smtp-mail.outlook.com", smtp_port: 587 },
  other: { label: "Другая почта", smtp_host: "", smtp_port: 587 },
};

/** Guided e-mail setup: address + app password → «Проверить» sends a real test mail and saves. */
export function EmailConnect({ initial = {}, onDone }) {
  const [provider, setProvider] = useState("gmail");
  const [email, setEmail] = useState(initial.username || "");
  const [password, setPassword] = useState("");
  const [to, setTo] = useState("");
  const [host, setHost] = useState("");
  const [port, setPort] = useState(587);
  const [state, setState] = useState({ state: "idle" });
  const preset = MAIL_PRESETS[provider];
  const run = async () => {
    setState({ state: "loading" });
    try {
      const res = await api.post(
        "/email/test",
        {
          smtp_host: provider === "other" ? host : preset.smtp_host,
          smtp_port: provider === "other" ? port : preset.smtp_port,
          username: email.trim() || null,
          password: password || null,
          to_addrs: (to.trim() || email.trim()) || null,
          save: true,
        },
        { timeout: 70000 },
      );
      setState({ state: "ok", message: res.message_ru });
      onDone && onDone(res);
    } catch (e) {
      const gmail = provider === "gmail" && /auth|password|535|534|Username/i.test(e.message);
      setState({ state: "fail", message: gmail ? "Gmail не пустил: нужен «пароль приложения», а не обычный пароль" : e.message, fields: e.fields || {} });
    }
  };
  const f = state.fields || {};
  return html`<div class="stack" style=${{ "--gap": "14px" }}>
    <${Field} label="Почта">
      ${(id) => html`<${Select} id=${id} value=${provider} onChange=${setProvider} options=${Object.entries(MAIL_PRESETS).map(([value, p]) => ({ value, label: p.label }))} />`}
    <//>
    ${provider === "gmail" &&
    html`<p class="substep__text">Gmail пускает программы только по «паролю приложения»: открой <${ExternalLink} href=${LINKS.googleAppPasswords}>страницу паролей приложений<//>, создай пароль с любым названием и скопируй 16 букв.</p>`}
    <div class="form-grid">
      <${Field} label="Твой адрес" error=${f.username}>
        ${(id) => html`<${Input} id=${id} type="email" value=${email} onChange=${setEmail} placeholder="me@gmail.com" autocomplete="email" />`}
      <//>
      <${Field} label=${provider === "gmail" ? "Пароль приложения" : "Пароль"} error=${f.password} help=${initial.password_set ? "Пароль уже сохранён — оставь пустым, чтобы не менять" : ""}>
        ${(id) => html`<${SecretInput} id=${id} value=${password} onChange=${setPassword} placeholder=${provider === "gmail" ? "abcd efgh ijkl mnop" : ""} saved=${initial.password_set} />`}
      <//>
    </div>
    ${provider === "other" &&
    html`<div class="form-grid">
      <${Field} label="SMTP-сервер" error=${f.smtp_host}>${(id) => html`<${Input} id=${id} value=${host} onChange=${setHost} placeholder="smtp.example.com" />`}<//>
      <${Field} label="Порт" help="587 — STARTTLS, 465 — SSL">${(id) => html`<${NumberInput} id=${id} value=${port} onChange=${(v) => setPort(v || 587)} />`}<//>
    </div>`}
    <${Field} label="Куда присылать" optional help="По умолчанию — на этот же адрес">
      ${(id) => html`<${Input} id=${id} value=${to} onChange=${setTo} placeholder=${email || "me@gmail.com"} />`}
    <//>
    <div class="row">
      <${Button} variant="primary" icon="mail-check" loading=${state.state === "loading"} disabled=${!email.trim() || (!password && !initial.password_set)} onClick=${run}>Проверить и сохранить<//>
    </div>
    ${state.state === "loading" && html`<${TestResult} state="loading" title="Отправляю тестовое письмо…" />`}
    ${state.state === "ok" && html`<${TestResult} state="ok" title=${state.message} detail="Почта включена — уведомления будут приходить и туда" />`}
    ${state.state === "fail" && html`<${TestResult} state="fail" title=${state.message} />`}
  </div>`;
}

/** eBay Browse API keys: guide + App ID / Cert ID + «Проверить» (keys are saved when they work). */
export function EbayConnect({ initial = {}, onDone }) {
  const [clientId, setClientId] = useState("");
  const [secret, setSecret] = useState("");
  const [state, setState] = useState({ state: "idle" });
  const run = async () => {
    setState({ state: "loading" });
    try {
      const res = await api.post("/ebay/test", { client_id: clientId.trim() || null, client_secret: secret.trim() || null, save: true }, { timeout: 40000 });
      setState({ state: "ok", message: res.message_ru, warning: res.warning_ru });
      onDone && onDone(res);
    } catch (e) {
      setState({ state: "fail", message: e.message, fields: e.fields || {} });
    }
  };
  const f = state.fields || {};
  return html`<div class="stack" style=${{ "--gap": "14px" }}>
    <details class="guide" open=${!initial.configured}>
      <summary>Как получить ключи — 3 шага, бесплатно</summary>
      <ol class="guide__steps">
        <li>Зарегистрируйся на <${ExternalLink} href=${LINKS.ebayRegister}>developer.ebay.com<//></li>
        <li>Открой <${ExternalLink} href=${LINKS.ebayDevelopers}>My Keys<//> → создай <b>Production</b> keyset</li>
        <li>
          На вопрос про <i>Marketplace Account Deletion</i> выбери исключение <b>«I do not persist eBay data»</b>
          <${HelpTip} title="Почему это правда">EbeyParser только смотрит объявления и не хранит данные пользователей eBay — поэтому исключение подходит.<//>
        </li>
      </ol>
    </details>
    <div class="form-grid">
      <${Field} label="App ID (Client ID)" error=${f.client_id}>
        ${(id) => html`<${SecretInput} id=${id} value=${clientId} onChange=${setClientId} placeholder="MaksimP-Ebey-PRD-…" saved=${initial.client_id_set} class="mono" />`}
      <//>
      <${Field} label="Cert ID (Client Secret)" error=${f.client_secret}>
        ${(id) => html`<${SecretInput} id=${id} value=${secret} onChange=${setSecret} placeholder="PRD-…" saved=${initial.client_secret_set} class="mono" />`}
      <//>
    </div>
    <div class="row">
      <${Button} variant="primary" icon="key-round" loading=${state.state === "loading"} disabled=${!(clientId && secret) && !initial.configured} onClick=${run}>Проверить<//>
    </div>
    ${state.state === "loading" && html`<${TestResult} state="loading" title="Спрашиваю eBay…" />`}
    ${state.state === "ok" && html`<${TestResult} state="ok" title=${state.message} detail=${state.warning || "Ключи сохранены"} />`}
    ${state.state === "fail" && html`<${TestResult} state="fail" title=${state.message} />`}
  </div>`;
}
