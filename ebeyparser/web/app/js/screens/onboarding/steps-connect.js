// Onboarding steps 5–7: Нейросеть, Telegram (or e-mail), eBay (brief §4.1.6–4.1.8).
import { html, useEffect, useState } from "../../lib/html.js";
import { api } from "../../lib/api.js";
import { Icon, TestResult } from "../../ui/index.js";
import { AiConnect } from "../../setup/ai.js";
import { TelegramConnect } from "../../setup/telegram.js";
import { EmailConnect, EbayConnect } from "../../setup/mail-ebay.js";
import { updateDraft } from "./draft.js";

/** Secret flags ({set, masked}) from GET /settings — to resume half-finished integrations. */
function useSecrets() {
  const [secrets, setSecrets] = useState(null);
  useEffect(() => {
    api
      .get("/settings")
      .then((s) => setSecrets(s.secrets || {}))
      .catch(() => setSecrets({}));
  }, []);
  return secrets;
}

export function AiStep({ draft }) {
  const [wasDone] = useState(draft.ai.done); // returning to a finished step
  return html`<div class="stack" style=${{ "--gap": "16px" }}>
    ${wasDone &&
    html`<${TestResult} state="ok" title=${`Нейросеть подключена${draft.ai.model ? ` · ${draft.ai.model}` : ""}`} detail="Можно проверить ещё раз или сменить модель ниже." />`}
    <${AiConnect}
      save=${true}
      onDone=${(r) => updateDraft({ ai: { ...draft.ai, done: true, skipped: false, model: r.resolved_model || r.model, seconds: r.seconds } })}
    />
  </div>`;
}

export function TelegramStep({ draft }) {
  const secrets = useSecrets();
  const [mode, setMode] = useState(draft.email && draft.email.done && !draft.telegram.linked ? "email" : "telegram");
  if (!secrets) return html`<div class="ai-card ai-card--loading"><span class="spinner" style=${{ width: 18, height: 18 }}></span></div>`;
  const tg = draft.telegram;
  return html`<div class="stack" style=${{ "--gap": "16px" }}>
    ${mode === "telegram"
      ? html`<${TelegramConnect}
            initial=${{
              token_set: secrets.telegram_bot_token && secrets.telegram_bot_token.set,
              chat_set: secrets.telegram_chat_id && secrets.telegram_chat_id.set && tg.linked,
              bot: tg.bot,
              chat: tg.chat,
            }}
            onLinked=${({ bot, chat }) => updateDraft({ telegram: { ...tg, linked: true, skipped: false, bot, chat } })}
            onDone=${({ bot, chat }) => updateDraft({ telegram: { ...tg, linked: true, done: true, skipped: false, bot, chat } })}
          />
          <button type="button" class="alt-link" onClick=${() => setMode("email")}><${Icon} name="mail" size=${16} />Лучше на почту</button>`
      : html`<${EmailConnect}
            initial=${{ username: "", password_set: secrets.smtp_password && secrets.smtp_password.set }}
            onDone=${() => updateDraft({ email: { done: true } })}
          />
          <button type="button" class="alt-link" onClick=${() => setMode("telegram")}><${Icon} name="send" size=${16} />Вернуться к Telegram</button>`}
  </div>`;
}

export function EbayStep({ draft }) {
  const secrets = useSecrets();
  const [wasDone] = useState(draft.ebay.done); // returning to a finished step
  if (!secrets) return null;
  const configured = Boolean(secrets.ebay_client_id && secrets.ebay_client_id.set && secrets.ebay_client_secret && secrets.ebay_client_secret.set);
  return html`<div class="stack" style=${{ "--gap": "16px" }}>
    <${EbayConnect}
      initial=${{ configured, client_id_set: configured, client_secret_set: configured }}
      onDone=${() => updateDraft({ ebay: { done: true, skipped: false } })}
    />
    ${wasDone && html`<${TestResult} state="ok" title="eBay подключён" detail="Ключи сохранены — можно идти дальше" />`}
  </div>`;
}
