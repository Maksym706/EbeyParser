// Component gallery for design QA: /dev/ui (not linked from the navigation).
import { html, useState } from "../lib/html.js";
import {
  Button,
  IconButton,
  Card,
  Badge,
  Chip,
  Glyph,
  Money,
  Tooltip,
  StatusDot,
  Field,
  Input,
  NumberInput,
  SecretInput,
  Select,
  Toggle,
  Checkbox,
  Slider,
  Segmented,
  NumberStepper,
  ChipInput,
  TestResult,
  Tabs,
  Steps,
  Checklist,
  Progress,
  Ring,
  Meter,
  Banner,
  EmptyState,
  Skeleton,
  Stat,
  KeyValue,
  HelpTip,
  Modal,
  Drawer,
  confirm,
  toast,
  PageHeader,
  Section,
} from "../ui/index.js";

export default function Gallery() {
  const [seg, setSeg] = useState("grid");
  const [tab, setTab] = useState("all");
  const [on, setOn] = useState(true);
  const [radius, setRadius] = useState(30);
  const [budget, setBudget] = useState(400);
  const [chips, setChips] = useState(["buy"]);
  const [words, setWords] = useState(["defekt", "bastler"]);
  const [n, setN] = useState(3);
  const [modal, setModal] = useState(false);
  const [drawer, setDrawer] = useState(false);
  const toggleChip = (c) => setChips(chips.includes(c) ? chips.filter((x) => x !== c) : [...chips, c]);
  return html`<div class="gallery">
    <${PageHeader} title="Дизайн-система" subtitle="Компоненты EbeyParser — токены из brief §6" />

    <${Section} title="Кнопки">
      <div class="row">
        <${Button} variant="primary" icon="send">Написать продавцу<//>
        <${Button} variant="secondary" icon="external-link">Открыть объявление<//>
        <${Button} variant="ghost">Пропустить<//>
        <${Button} variant="danger">Удалить поиск<//>
        <${Button} variant="danger-ghost" icon="trash">Удалить<//>
        <${Button} variant="tinted" tone="amber" icon="hand-coins">Предложить 300 €<//>
        <${Button} variant="primary" loading>Сохраняю<//>
        <${Button} variant="secondary" disabled>Недоступно<//>
      </div>
      <div class="row mt-4">
        <${Button} size="sm" variant="primary">sm 32<//>
        <${Button} size="md" variant="primary">md 40<//>
        <${Button} size="lg" variant="primary" iconRight="arrow-right">lg 48<//>
        <${IconButton} icon="star" label="В избранное" />
        <${IconButton} icon="bell" label="Уведомления" badge=${4} variant="secondary" />
        <${Tooltip} text="87 из 100 — отличная находка"><${Badge} tone="green" icon="trending-up">Покупай · 87<//><//>
      </div>
    <//>

    <${Section} title="Бейджи, чипы, суммы">
      <div class="row">
        <${Badge} tone="green">Покупай<//>
        <${Badge} tone="amber" icon="hand-coins">Торг<//>
        <${Badge} tone="violet" icon="gavel">Аукцион<//>
        <${Badge} tone="red" icon="shield-alert">Развод?<//>
        <${Badge} tone="blue" icon="scan-eye">ИИ ✓<//>
        <${Badge}>Подумай<//>
        <${Badge} tone="green" variant="solid">новое<//>
        <${Badge} tone="amber" variant="outline" size="sm">VB<//>
      </div>
      <div class="row mt-4">
        ${["buy", "haggle", "bid", "personal"].map((c) => html`<${Chip} key=${c} selected=${chips.includes(c)} onClick=${() => toggleChip(c)} count=${c === "haggle" ? 4 : null}>${{ buy: "Купить сразу", haggle: "Торг", bid: "Аукционы", personal: "Для себя" }[c]}<//>`)}
      </div>
      <div class="row mt-4">
        <${Money} value=${110} sign tone="auto" size="lg" />
        <${Money} value=${-35} sign tone="auto" size="lg" />
        <${Money} value=${1250} size="xl" />
        <${Glyph} icon="smartphone" />
        <${Glyph} icon="trending-up" tone="green" />
        <${Glyph} icon="hand-coins" tone="amber" />
        <${Glyph} icon="gavel" tone="violet" />
        <${Glyph} icon="shield-alert" tone="red" />
        <${Glyph} icon="scan-eye" tone="blue" />
        <${StatusDot} tone="green" pulse /><${StatusDot} tone="amber" /><${StatusDot} tone="red" />
      </div>
    <//>

    <${Section} title="Поля ввода">
      <div class="form-grid">
        <${Field} label="Город" help="Можно почтовый индекс">${(id) => html`<${Input} id=${id} icon="map-pin" value="Berlin" />`}<//>
        <${Field} label="Цена до" error="Нужно число больше нуля">${(id) => html`<${NumberInput} id=${id} value=${null} suffix="€" invalid />`}<//>
        <${Field} label="Ключ бота">${(id) => html`<${SecretInput} id=${id} value="" saved />`}<//>
        <${Field} label="Площадка" aside=${html`<${HelpTip} title="Площадка">Где искать на eBay<//>`}>${(id) => html`<${Select} id=${id} value="de" options=${[{ value: "de", label: "eBay.de" }]} />`}<//>
      </div>
      <div class="form-grid mt-4">
        <div>
          <${Toggle} checked=${on} onChange=${setOn} label="Сообщать о проблемах" description="Нейросеть упала, сайт заблокировал" />
          <${Checkbox} checked=${on} onChange=${setOn} label="Включить ключи" description="Храни файл в надёжном месте" />
        </div>
        <div class="stack">
          <${Segmented} value=${seg} onChange=${setSeg} options=${[{ value: "grid", label: "Сетка", icon: "layout-grid" }, { value: "rows", label: "Список", icon: "rows-3" }]} />
          <${NumberStepper} value=${n} onChange=${setN} min=${1} max=${5} label="Фото" />
        </div>
      </div>
      <div class="form-grid mt-4">
        <${Slider} value=${radius} steps=${[0, 5, 10, 20, 30, 50, 100, 150, 200]} bubble="always" tone="green" format=${(v, s) => (s ? String(v) : `${v} км`)} onChange=${setRadius} />
        <${Slider} value=${budget} min=${50} max=${1500} step=${10} format=${(v) => `${v} €`} onChange=${setBudget} marks=${[{ value: 50, label: "50 €" }, { value: 1500, label: "1 500 €" }]} />
      </div>
      <div class="mt-4"><${ChipInput} value=${words} onChange=${setWords} suggestions=${["suche", "tausch", "ersatzteil"]} /></div>
    <//>

    <${Section} title="Навигация и прогресс">
      <${Tabs} value=${tab} onChange=${setTab} items=${[{ value: "all", label: "Все", count: 34 }, { value: "buy", label: "Купить", count: 12 }, { value: "haggle", label: "Торг", count: 8 }]} />
      <div class="mt-4"><${Tabs} variant="pills" value=${tab} onChange=${setTab} items=${[{ value: "all", label: "Все" }, { value: "buy", label: "Купить" }, { value: "haggle", label: "Торг" }]} /></div>
      <div class="mt-6"><${Steps} steps=${[{ label: "Где" }, { label: "Что" }, { label: "Деньги" }, { label: "Готово" }]} current=${2} /></div>
      <div class="form-grid mt-6">
        <${Checklist} items=${[{ label: "Открываю объявление", state: "done" }, { label: "Ищу цены похожих", state: "active", description: "3 из 8 поисков" }, { label: "Нейросеть смотрит фото", state: "todo" }]} />
        <div class="stack">
          <${Progress} value=${0.6} />
          <${Progress} value=${null} tone="blue" />
          <${Meter} label="Нагрузка на Kleinanzeigen" value=${64} max=${150} marker=${0.4} hint="Проверка каждые 30 мин — безопасно" />
          <${Meter} label="eBay сегодня" value=${4400} max=${5000} />
          <div class="row"><${Ring} value=${0.6}>3/5<//><${Ring} value=${0.9} tone="amber">90<//></div>
        </div>
      </div>
    <//>

    <${Section} title="Сообщения">
      <div class="stack">
        <${Banner} tone="green" title="Всё работает">Последняя проверка 8 мин назад: 212 новых, 3 выгодных.<//>
        <${Banner} tone="amber" title="Нейросеть не отвечает с 13:05" action=${html`<${Button} size="sm">Проверить<//>`}>Уведомления идут с пометкой «фото не проверены».<//>
        <${Banner} tone="red">Kleinanzeigen попросил отдохнуть — пауза до 14:10.<//>
        <${Banner} tone="blue">Первая проверка только изучает цены.<//>
        <${TestResult} state="ok" title="Бот @maks_deals_bot найден" />
        <${TestResult} state="warn" title="Медленно: ответ за 94 с" detail="На слабом ПК выбери модель поменьше" />
        <${TestResult} state="fail" title="Ключ не подошёл" detail="Скопируй его у @BotFather ещё раз" />
        <${TestResult} state="loading" title="Жду ответа от LM Studio…" />
      </div>
      <div class="row mt-4">
        <${Button} onClick=${() => toast({ kind: "deal", title: "Новая находка: iPhone 12 · +95 €", action: { label: "Открыть", href: "/" } })}>Тост «находка»<//>
        <${Button} onClick=${() => toast.success("Настройки сохранены")}>Тост «успех»<//>
        <${Button} onClick=${() => toast.error("Не получилось сохранить: нет доступа")}>Тост «ошибка»<//>
        <${Button} onClick=${() => setModal(true)}>Модальное окно<//>
        <${Button} onClick=${() => setDrawer(true)}>Боковая панель<//>
        <${Button} variant="danger-ghost" onClick=${() => confirm({ title: "Удалить поиск «RTX 3090»?", message: "Найденные объявления останутся в ленте.", confirmLabel: "Удалить поиск", tone: "danger" })}>Подтверждение<//>
      </div>
    <//>

    <${Section} title="Карточки и состояния">
      <div class="grid" style=${{ "--min": "240px" }}>
        <${Card}><${Stat} label="Заработано за месяц" value="+310 €" tone="green" hint="3 продажи" /><//>
        <${Card}><${Stat} label="Вложено сейчас" value="640 €" hint="в 3 вещах" icon="wallet" /><//>
        <${Card} interactive><${KeyValue} rows=${[["Рынок", "~420 €"], ["Прибыль", "+110 €"], ["ROI", "38 %"]]} /><//>
        <${Skeleton} variant="card" />
      </div>
      <${Card} class="mt-6"><${EmptyState} icon="radar" tone="green" title="Здесь появятся выгодные находки" message="Сначала расскажи, что искать — это 3 минуты" action=${html`<${Button} variant="primary">Настроить<//>`} secondary=${html`<${Button} variant="ghost">Посмотреть демо<//>`} /><//>
    <//>

    <${Modal} open=${modal} onClose=${() => setModal(false)} title="Купил за…" subtitle="Запишу цену, чтобы посчитать реальную прибыль" icon="package-check" footer=${html`<${Button} variant="ghost" onClick=${() => setModal(false)}>Отмена<//><${Button} variant="primary" onClick=${() => setModal(false)}>Сохранить<//>`}>
      <${Field} label="Цена покупки">${(id) => html`<${NumberInput} id=${id} value=${290} suffix="€" />`}<//>
    <//>
    <${Drawer} open=${drawer} onClose=${() => setDrawer(false)} title="iPhone 13 128GB" subtitle="Kleinanzeigen · Neukölln · 7 мин назад" footer=${html`<${Button} variant="primary" icon="send" block>Написать продавцу<//>`}>
      <${KeyValue} rows=${[["Цена", "290 € VB"], ["Рынок", "~420 €"], ["Прибыль", "+110 €"]]} />
    <//>
  </div>`;
}
