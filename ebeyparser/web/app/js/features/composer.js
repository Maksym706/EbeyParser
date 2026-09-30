// «Сообщение продавцу» — German message composer with a live Russian translation (brief §4.3.11).
import { html, cx, useState, useEffect, useMemo, useRef, useLayoutEffect } from "../lib/html.js";
import { Icon, Button, Toggle, NumberInput, toast } from "../ui/index.js";
import { TEMPLATES, ADDONS, DAYS, buildMessage, defaultTemplate, defaultOffer, defaultAddons, shortTitle, copyText, openAd } from "./messages.js";
import { setStatus } from "./deal-actions.js";
import { humanOffer } from "./deal-model.js";

/**
 * <Composer deal={detail} offer={fromSlider} />
 * `offer` (optional) follows the haggle slider; the user can still type their own.
 */
export function Composer({ deal, offer: externalOffer }) {
  const [template, setTemplate] = useState(() => defaultTemplate(deal));
  const [offer, setOffer] = useState(() => defaultOffer(deal));
  const [day, setDay] = useState("heute");
  const [cash, setCash] = useState(true);
  const [addons, setAddons] = useState(() => defaultAddons(deal));
  const [edited, setEdited] = useState(null); // user's own text, null = generated
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (externalOffer != null) {
      setOffer(humanOffer(externalOffer));
      if (TEMPLATES.find((t) => t.key === template && !t.usesOffer)) setTemplate("offer");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [externalOffer]);

  const title = useMemo(() => shortTitle(deal), [deal && deal.id, deal && deal.ai && deal.ai.product]);
  const msg = buildMessage({ template, title, offer, day, cash, addons });
  const text = edited ?? msg.de;
  const box = useRef(null);
  useLayoutEffect(() => {
    // grow with the text (no inner scrollbar)
    const el = box.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight + 2}px`;
  }, [text]);
  const available = Object.keys(ADDONS).filter((k) => addons.includes(k) || defaultAddons(deal).includes(k));

  const pickTemplate = (key) => {
    setTemplate(key);
    setEdited(null);
  };

  const afterCopy = (ok, opened) => {
    if (!ok) {
      toast.warning("Не получилось скопировать", { message: "Выдели текст и скопируй вручную (Ctrl+C)." });
      return;
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
    const later = ["contacted", "bought", "sold"].includes(deal.status);
    toast({
      kind: "success",
      icon: "copy",
      title: opened ? "Скопировано — вставь сообщение в чат продавцу" : "Скопировано",
      duration: 6000,
      action: later ? null : { label: "Отметить «Написал»", onClick: () => setStatus(deal, "contacted", { message: "Отмечено: написал продавцу" }) },
    });
  };

  const copy = () => copyText(text).then((ok) => afterCopy(ok, false));
  const copyOpen = () => {
    const p = copyText(text);
    openAd(deal.url);
    p.then((ok) => afterCopy(ok, true));
  };

  return html`<div class="composer">
    <div class="composer__tabs" role="tablist" aria-label="Шаблон сообщения">
      ${TEMPLATES.map(
        (t) => html`<button
          type="button"
          role="tab"
          aria-selected=${template === t.key}
          class=${cx("pill-tab", template === t.key && "is-on")}
          onClick=${() => pickTemplate(t.key)}
        >
          ${t.label}
        </button>`,
      )}
    </div>

    <div class="composer__vars">
      ${msg.usesOffer &&
      html`<label class="composer__var">
        <span>Предложение</span>
        <${NumberInput} value=${offer} onChange=${(v) => (setOffer(v), setEdited(null))} suffix="€" size="sm" min=${1} aria-label="Сумма предложения" />
      </label>`}
      <div class="composer__var">
        <span>Когда заберу</span>
        <div class="mini-seg" role="radiogroup" aria-label="Когда заберу">
          ${DAYS.map(
            (d) => html`<button type="button" role="radio" aria-checked=${day === d.value} class=${cx(day === d.value && "is-on")} onClick=${() => (setDay(d.value), setEdited(null))}>
              ${d.label}
            </button>`,
          )}
        </div>
      </div>
      <div class="composer__var composer__var--toggle">
        <${Toggle} checked=${cash} onChange=${(v) => (setCash(v), setEdited(null))} label="Наличными" size="sm" />
      </div>
    </div>

    ${available.length > 0 &&
    html`<div class="composer__addons">
      ${available.map((k) => {
        const on = addons.includes(k);
        return html`<button
          type="button"
          class=${cx("addon", on && "is-on")}
          aria-pressed=${on}
          onClick=${() => {
            setAddons(on ? addons.filter((a) => a !== k) : [...addons, k]);
            setEdited(null);
          }}
        >
          <${Icon} name=${on ? "check" : "plus"} size=${14} />Вопросы: ${ADDONS[k].label}${on && html`<${Icon} name="x" size=${14} class="addon__x" />`}
        </button>`;
      })}
    </div>`}

    <div class="composer__box">
      <textarea
        ref=${box}
        class="composer__text"
        lang="de"
        spellcheck="false"
        rows="6"
        value=${text}
        aria-label="Текст сообщения на немецком"
        onInput=${(e) => setEdited(e.currentTarget.value)}
      ></textarea>
      ${edited != null &&
      html`<button type="button" class="composer__reset" onClick=${() => setEdited(null)}><${Icon} name="undo-2" size=${14} />Вернуть шаблон</button>`}
    </div>
    <p class="composer__ru">
      <span class="composer__ru-label">По-русски:</span>
      ${edited != null ? html`<i>перевод недоступен после правки</i>` : msg.ru}
    </p>

    <div class="composer__actions">
      <${Button} variant="primary" icon=${copied ? "check" : "copy"} onClick=${copy}>${copied ? "Скопировано" : "Скопировать"}<//>
      <${Button} variant="secondary" icon="external-link" onClick=${copyOpen} disabled=${!deal.url}>Скопировать и открыть объявление<//>
    </div>
    <p class="composer__hint">Один вежливый торг — ок. Повторные попытки сбить цену продавцы не любят.</p>
  </div>`;
}
