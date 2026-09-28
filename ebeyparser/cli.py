"""Command line entry point: `python -m ebeyparser <command>` (or `ebeyparser <command>`)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import shutil
import signal
import sys
from pathlib import Path

from .config import DEFAULT_CONFIG_PATH, AppConfig, load_config
from .db import Database
from .models import DealView

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG = ROOT / "config.example.yaml"
EXAMPLE_ENV = ROOT / ".env.example"


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _open(config_path: Path) -> tuple[AppConfig, Database]:
    config = load_config(config_path)
    if not config_path.is_file():
        print(f"⚠  {config_path} не найден — работаю с настройками по умолчанию. "
              "Создай конфиг командой: python -m ebeyparser init")
    return config, Database(config.db_path)


def _web_base_url(config: AppConfig) -> str:
    host = "localhost" if config.web.host in ("0.0.0.0", "127.0.0.1", "::") else config.web.host
    return f"http://{host}:{config.web.port}"


def _money(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.0f} €".replace(",", ".")


def _print_deals(deals: list[DealView]) -> None:
    if not deals:
        print("Пока ничего выгодного не найдено.")
        return
    for deal in deals:
        ev, listing = deal.evaluation, deal.listing
        if ev is None:
            continue
        icon = {"buy": "🟢", "maybe": "🟡", "skip": "⚪"}[ev.verdict]
        profit_label = "экономия" if ev.purpose == "personal" else "прибыль"
        print(f"{icon} [{ev.score:5.1f}] {listing.title[:70]}")
        print(f"     цена {_money(ev.buy_price)} · рынок ~{_money(ev.estimate.market_price)}"
              f" · {profit_label} {_money(ev.expected_profit)} · {listing.location}")
        for reason in ev.reasons[:3]:
            print(f"     • {reason}")
        if ev.ai and ev.ai.reasoning:
            print(f"     🤖 {ev.ai.reasoning[:220]}")
        print(f"     {listing.url}")


# ------------------------------------------------------------------ commands
def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.config)
    if target.exists():
        print(f"{target} уже существует — не трогаю.")
    else:
        shutil.copyfile(EXAMPLE_CONFIG, target)
        print(f"✔ Создан {target}. Открой его и настрой поиски (регион, категории, цены).")
    env = target.parent / ".env"
    if not env.exists() and EXAMPLE_ENV.exists():
        shutil.copyfile(EXAMPLE_ENV, env)
        print(f"✔ Создан {env} — впиши туда пароль от почты / токен Telegram.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Web UI + background monitoring (main mode)."""
    import uvicorn

    from .monitor import Monitor
    from .notify.base import build_notifiers
    from .web.app import create_app

    config_path = Path(args.config)
    config, db = _open(config_path)
    host = args.host or config.web.host
    port = args.port or config.web.port
    base_url = _web_base_url(config)
    monitor = Monitor(config, db, web_base_url=base_url)
    app = create_app(
        config, db,
        config_path=config_path,
        monitor=monitor,
        notifiers_factory=lambda: build_notifiers(app.state.config.notifications, web_base_url=base_url),
        start_monitor=config.web.run_monitor and not args.no_monitor,
    )
    print(f"🚀 EbeyParser: открой http://{'localhost' if host in ('0.0.0.0', '127.0.0.1') else host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info" if args.verbose else "warning")
    return 0


async def _monitor_loop(config: AppConfig, db: Database) -> None:
    from .monitor import Monitor

    monitor = Monitor(config, db, web_base_url=_web_base_url(config))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass
    try:
        await monitor.run_forever(stop)
    finally:
        await monitor.aclose()


def cmd_monitor(args: argparse.Namespace) -> int:
    """Monitoring loop without the web UI."""
    config, db = _open(Path(args.config))
    print(f"🔎 Мониторинг: {len([s for s in config.searches if s.enabled])} поисков, "
          f"каждые {config.general.interval_minutes:g} мин. Ctrl+C — остановить.")
    try:
        asyncio.run(_monitor_loop(config, db))
    except KeyboardInterrupt:
        pass
    return 0


def cmd_once(args: argparse.Namespace) -> int:
    from .monitor import Monitor

    config, db = _open(Path(args.config))

    async def go():
        monitor = Monitor(config, db, web_base_url=_web_base_url(config))
        try:
            return await monitor.run_once()
        finally:
            await monitor.aclose()

    summary = asyncio.run(go())
    print(f"\nПоисков: {summary.searches} · объявлений: {summary.listings_seen} · новых: "
          f"{summary.new_listings} · оценено: {summary.evaluated} · выгодных: {summary.deals_found}"
          f" · уведомлений: {summary.notified}")
    for err in summary.errors:
        print(f"⚠  {err}")
    print()
    _print_deals(db.list_deals(verdict=["buy", "maybe"], limit=args.top))
    return 1 if summary.errors and not summary.evaluated else 0


def cmd_check(args: argparse.Namespace) -> int:
    from .monitor import Monitor

    config, db = _open(Path(args.config))

    async def go():
        monitor = Monitor(config, db)
        try:
            return await monitor.evaluate_url(args.url, purpose=args.purpose, target_price=args.target)
        finally:
            await monitor.aclose()

    try:
        deal = asyncio.run(go())
    except ValueError as exc:
        print(f"✖ {exc}")
        return 2
    _print_deals([deal])
    ev = deal.evaluation
    if ev and ev.ai:
        print(f"\n🤖 {ev.ai.model}: товар «{ev.ai.product}», состояние {ev.ai.condition}, "
              f"фото совпадает: {ev.ai.photo_matches_description}")
        for flag in ev.red_flags:
            print(f"   ⚠ {flag}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    from .demo import seed_demo

    config, db = _open(Path(args.config))
    count = seed_demo(db)
    print(f"✔ Добавлено демо-объявлений: {count} (база {config.db_path}). "
          "Запусти `python -m ebeyparser run --no-monitor` и открой браузер.")
    return 0


def cmd_test_notify(args: argparse.Namespace) -> int:
    from .notify.base import build_notifiers, missing_settings
    from .notify.render import sample_deals

    config = load_config(Path(args.config))
    for channel, missing in missing_settings(config.notifications).items():
        if missing:
            print(f"⚠  {channel}: не заполнено {', '.join(missing)}")
    notifiers = build_notifiers(config.notifications, web_base_url=_web_base_url(config))
    if not notifiers:
        print("✖ Ни один канал уведомлений не включён (notifications.email / notifications.telegram).")
        return 2

    async def go() -> int:
        failed = 0
        for notifier in notifiers:
            try:
                await notifier.send(sample_deals(), title="Тестовое уведомление EbeyParser")
                print(f"✔ {notifier.name}: отправлено")
            except Exception as exc:
                failed += 1
                print(f"✖ {notifier.name}: {exc}")
        return failed

    return 1 if asyncio.run(go()) else 0


def _estimate_ebay_calls(config: AppConfig) -> tuple[int, int]:
    """Rough daily eBay API usage of the current config: (search calls, per-new-item calls)."""
    runs_per_day = int(24 * 60 / max(1.0, config.general.interval_minutes))
    searches = [s for s in config.searches if s.enabled and s.source == "ebay"]
    search_calls = sum(runs_per_day * (s.max_pages or config.general.max_pages) for s in searches)
    per_item = (1 if config.general.fetch_details else 0) + 1  # detail + comparables (cached 6 h)
    return search_calls, per_item


def cmd_ebay_limits(args: argparse.Namespace) -> int:
    from .scraper.ebay_api import EbayAPIError, EbayBrowseClient

    config = load_config(Path(args.config))
    if not config.ebay.configured:
        print("✖ Ключи eBay не заданы: впиши EBAY_CLIENT_ID и EBAY_CLIENT_SECRET (или EBAY_OAUTH_TOKEN) в .env")
        return 2

    async def go():
        client = EbayBrowseClient(config.ebay)
        try:
            return await client.rate_limits(api_context=None if args.all else "buy")
        finally:
            await client.aclose()

    try:
        rows = asyncio.run(go())
    except EbayAPIError as exc:
        print(f"✖ {exc}")
        return 1
    if not rows:
        print("eBay не вернул лимитов для этого ключа.")
        return 1
    print(f"{'API':<22} {'ресурс':<28} {'использовано':>12} {'лимит':>8} {'осталось':>9}  сброс")
    for r in rows:
        window = r["window_seconds"]
        per = "/день" if window == 86400 else f"/{window}с" if window else ""
        reset = r["reset"].astimezone().strftime("%d.%m %H:%M") if r["reset"] else "—"
        print(f"{r['api']:<22} {r['resource']:<28} {r['count'] if r['count'] is not None else '—':>12} "
              f"{str(r['limit']) + per:>8} {r['remaining'] if r['remaining'] is not None else '—':>9}  {reset}")
    search_calls, per_item = _estimate_ebay_calls(config)
    print(f"\nТвои настройки: ~{search_calls} поисковых запросов в день к eBay "
          f"+ до {per_item} запроса на каждое новое объявление.")
    return 0


def cmd_debug_search(args: argparse.Namespace) -> int:
    """Fetch page 1 of each Kleinanzeigen search and show what the parser sees."""
    from .scraper.http import BlockedError, PoliteClient
    from .scraper.kleinanzeigen import KleinanzeigenScraper, build_search_url, page_diagnostics

    config = load_config(Path(args.config))
    searches = [s for s in config.searches if s.source == "kleinanzeigen" and (s.enabled or args.name)]
    if args.name:
        searches = [s for s in searches if s.name == args.name]
    if not searches:
        print("✖ Нет поисков Kleinanzeigen" + (f" с именем «{args.name}»" if args.name else ""))
        return 2

    async def go() -> int:
        client = PoliteClient.from_config(config.general)
        scraper = KleinanzeigenScraper(client, debug_dir=config.data_path / "debug")
        problems = 0
        try:
            for search in searches:
                url = build_search_url(search, 1)
                print(f"\n=== {search.name}")
                print(f"запрос:      {url}")
                try:
                    html = await client.get_text(url)
                except BlockedError as exc:
                    problems += 1
                    print(f"✖ БЛОКИРОВКА: {exc}")
                    continue
                except Exception as exc:
                    problems += 1
                    print(f"✖ Ошибка: {type(exc).__name__}: {exc}")
                    continue
                print(f"итоговый URL: {client.last_url}  (HTTP {client.last_status})")
                info = page_diagnostics(html)
                for key, value in info.items():
                    if key != "sample":
                        print(f"{key + ':':<22} {value}")
                for ad_id, title, price, location in info["sample"]:  # type: ignore[union-attr]
                    print(f"   • {ad_id} | {title} | {price} | {location}")
                saved = scraper.save_debug_page(html, search.name)
                print(f"HTML сохранён: {saved}")
                if not info["parsed_ads"]:
                    problems += 1
        finally:
            await client.aclose()
        print("\nПришли этот вывод целиком — по нему видно, что отдаёт сайт.")
        return 1 if problems else 0

    return asyncio.run(go())


def cmd_debug_ad(args: argparse.Namespace) -> int:
    """Fetch one Kleinanzeigen ad page and show what the detail parser extracts."""
    from .scraper.http import PoliteClient
    from .scraper.kleinanzeigen import KleinanzeigenScraper, parse_ad_detail

    config = load_config(Path(args.config))

    async def go() -> int:
        client = PoliteClient.from_config(config.general)
        scraper = KleinanzeigenScraper(client, debug_dir=config.data_path / "debug")
        try:
            html = await client.get_text(args.url)
        finally:
            await client.aclose()
        print(f"итоговый URL: {client.last_url}  (HTTP {client.last_status}), {len(html) // 1024} КБ")
        try:
            ad = parse_ad_detail(html, url=args.url)
        except ValueError as exc:
            print(f"✖ {exc}")
            ad = None
        if ad is not None:
            print(f"id:          {ad.ad_id}")
            print(f"заголовок:   {ad.title}")
            print(f"цена:        {ad.price_text!r} -> {ad.price} (торг: {ad.negotiable}, бесплатно: {ad.is_free})")
            print(f"место:       {ad.location}")
            print(f"фото:        {len(ad.image_urls)}")
            print(f"параметры:   {ad.attributes}")
            print(f"продавец:    {ad.seller_name} ({ad.seller_type})")
            print(f"доставка:    {ad.shipping_possible} {ad.shipping_cost or ''}")
            print(f"описание:    {len(ad.description)} символов: {ad.description[:150]!r}")
        print(f"HTML сохранён: {scraper.save_debug_page(html, 'ad')}")
        return 0 if ad is not None and ad.description and ad.image_urls else 1

    return asyncio.run(go())


def set_ai_model_in_config(path: Path, model: str) -> bool:
    """Replace `model:` inside the top-level `ai:` block (not second_opinion), keeping comments."""
    if not path.is_file():
        return False
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    in_ai = False
    for i, line in enumerate(lines):
        if re.match(r"^ai:\s*(#.*)?$", line):
            in_ai = True
            continue
        if in_ai and re.match(r"^\S", line):  # next top-level section
            break
        if in_ai and re.match(r"^  model:", line):
            comment = re.search(r"\s+#.*$", line.rstrip("\n"))
            lines[i] = f"  model: {model}{comment.group(0) if comment else ''}\n"
            path.write_text("".join(lines), encoding="utf-8")
            return True
    return False


def cmd_ai_check(args: argparse.Namespace) -> int:
    from .ai.client import looks_like_vision_model
    from .monitor import Monitor

    config_path = Path(args.config)
    config = load_config(config_path)
    config.ai.enabled = True  # check even if not switched on yet
    monitor = Monitor(config, Database())
    health = asyncio.run(monitor.ai_health())
    lmstudio = config.ai.provider == "openai"
    if health.get("ok") and health.get("model_available"):
        resolved = health.get("resolved_model")
        print(f"✔ {health['provider']} на {health['base_url']} работает, модель {resolved or health['model']} найдена.")
        if resolved and resolved != health["model"]:
            print(f"   (в конфиге «{health['model']}», на сервере она называется «{resolved}» — буду использовать её)")
        return 0

    if not health.get("server_ok"):
        print(f"✖ Сервер модели недоступен: {health.get('error')}")
        if lmstudio:
            print("   LM Studio: вкладка Developer → Start Server (или команда `lms server start`),"
                  " адрес по умолчанию http://localhost:1234/v1")
        else:
            print("   Ollama: запусти приложение Ollama или команду `ollama serve`")
        return 1

    models = health.get("models") or []
    print(f"⚠  Сервер {health['base_url']} работает, но модели «{health['model']}» на нём нет.")
    if not models:
        print("   На сервере вообще нет моделей." + (
            " Скачай vision-модель в LM Studio (вкладка Discover), например Qwen2.5-VL-7B-Instruct."
            if lmstudio else f" Скачай: ollama pull {health['model']}"))
        return 1
    print("   Модели на сервере:")
    for name in models:
        print(f"     • {name}{'   ← умеет смотреть фото' if looks_like_vision_model(name) else ''}")
    vision = [m for m in models if looks_like_vision_model(m)]
    if not vision:
        print("   ⚠ Ни одна из них не похожа на vision-модель — а программе нужна модель, которая видит фото"
              " (Qwen2.5-VL, Gemma 3, MiniCPM-V, LLaVA...).")
        return 1
    best = vision[0]
    if args.fix:
        if set_ai_model_in_config(config_path, best):
            print(f"✔ Записал в {config_path}: model: {best}. Запусти ai-check ещё раз.")
            return 0
        print(f"✖ Не нашёл строку model: в разделе ai: файла {config_path} — впиши вручную: model: {best}")
        return 1
    print(f"   Впиши в {config_path} в раздел ai:   model: {best}")
    print("   или просто выполни:  python -m ebeyparser ai-check --fix")
    if not lmstudio:
        print(f"   (или скачай нужную: ollama pull {health['model']})")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ebeyparser",
        description="Охотник за выгодными объявлениями на Kleinanzeigen.",
    )
    parser.add_argument("-c", "--config", default=str(DEFAULT_CONFIG_PATH), help="путь к config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true", help="подробный лог")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", help="создать config.yaml и .env из примеров").set_defaults(func=cmd_init)

    p = sub.add_parser("run", help="веб-интерфейс + мониторинг в фоне (основной режим)")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--no-monitor", action="store_true", help="только веб-интерфейс, без проверок")
    p.set_defaults(func=cmd_run)

    sub.add_parser("monitor", help="мониторинг без веб-интерфейса").set_defaults(func=cmd_monitor)

    p = sub.add_parser("once", help="одна проверка всех поисков и вывод лучших находок")
    p.add_argument("--top", type=int, default=10)
    p.set_defaults(func=cmd_once)

    p = sub.add_parser("check", help="оценить одно объявление по ссылке")
    p.add_argument("url")
    p.add_argument("--purpose", choices=["resale", "personal"], default="resale")
    p.add_argument("--target", type=float, help="для себя: сколько готов заплатить")
    p.set_defaults(func=cmd_check)

    sub.add_parser("demo", help="заполнить базу демо-данными для просмотра интерфейса").set_defaults(func=cmd_demo)
    sub.add_parser("test-notify", help="отправить тестовое уведомление").set_defaults(func=cmd_test_notify)
    p = sub.add_parser("ai-check", help="проверить, доступна ли локальная модель")
    p.add_argument("--fix", action="store_true", help="сам вписать в config.yaml найденную vision-модель")
    p.set_defaults(func=cmd_ai_check)
    p = sub.add_parser("debug-search", help="показать, что парсер видит на странице поиска Kleinanzeigen")
    p.add_argument("--name", help="только поиск с этим именем")
    p.set_defaults(func=cmd_debug_search)

    p = sub.add_parser("debug-ad", help="показать, что парсер видит на странице объявления Kleinanzeigen")
    p.add_argument("url")
    p.set_defaults(func=cmd_debug_ad)

    p = sub.add_parser("ebay-limits", help="показать лимиты запросов твоего ключа eBay")
    p.add_argument("--all", action="store_true", help="все API, а не только Buy (Browse)")
    p.set_defaults(func=cmd_ebay_limits)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args([*(argv or sys.argv[1:]), "run"])
    for name, default in (("host", None), ("port", None), ("no_monitor", False)):
        if not hasattr(args, name):
            setattr(args, name, default)
    _setup_logging(args.verbose)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
