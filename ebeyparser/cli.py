"""Command line entry point: `python -m ebeyparser <command>` (or `ebeyparser <command>`)."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import logging
import re
import shutil
import signal
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from .config import DEFAULT_CONFIG_PATH, AppConfig, ConfigError, load_config
from .db import Database
from .models import DealView
from .web.configfile import save_searches_block, update_yaml_values, write_env_values
from .web.localai import (
    DEFAULT_LMSTUDIO_MODEL,
    LMSTUDIO_URL,
    LocalAIServer,
    detect_local_ai,
    probe_json,
)

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


@contextmanager
def _long_running(config: AppConfig) -> Iterator[bool]:
    """24/7 modes: log file in data/logs, one instance at a time, no sleep (Windows).
    Yields False (after printing why) when another instance already runs."""
    from .runtime import (
        AlreadyRunningError,
        InstanceLock,
        file_logging,
        keep_awake,
        onedrive_warning,
    )

    warning = onedrive_warning(config.data_path)
    if warning:
        print(warning)
    lock = InstanceLock(config.data_path / "ebeyparser.lock")
    try:
        lock.acquire()
    except AlreadyRunningError as exc:
        print(f"✖ {exc}")
        yield False
        return
    except OSError as exc:  # read-only folder etc.: run anyway, just unguarded
        logging.getLogger(__name__).warning("Instance lock unavailable: %s", exc)
    try:
        with file_logging(config.data_path / "logs") as log_path, keep_awake():
            if log_path is not None:
                logging.getLogger(__name__).info("Log file: %s", log_path)
            yield True
    finally:
        lock.release()


def cmd_run(args: argparse.Namespace) -> int:
    """Web UI + background monitoring (main mode)."""
    import uvicorn

    from .monitor import Monitor
    from .notify.base import build_notifiers
    from .web.app import create_app
    from .web.security import TOKEN_PARAM, ensure_token, is_loopback, lan_urls

    config_path = Path(args.config)
    with _long_running(load_config(config_path)) as ok:
        if not ok:
            return 3
        config, db = _open(config_path)
        host = args.host or config.web.host
        port = args.port or config.web.port
        base_url = _web_base_url(config)
        token = None if is_loopback(host) else ensure_token(config.data_path)
        monitor = Monitor(config, db, web_base_url=base_url)
        app = create_app(
            config, db,
            config_path=config_path,
            monitor=monitor,
            notifiers_factory=lambda: build_notifiers(app.state.config.notifications, web_base_url=base_url),
            start_monitor=config.web.run_monitor and not args.no_monitor,
            bind_host=host,
            access_token=token,
        )
        if token is None:
            print(f"🚀 EbeyParser: открой http://localhost:{port}")
        else:
            everywhere = host in ("0.0.0.0", "::")
            print(f"⚠ Панель открыта {'для всей сети' if everywhere else 'по адресу ' + host}"
                  " — вход только по ссылке с ключом.")
            if everywhere:
                print("   Не делай так в общественном Wi-Fi (общежитие, кафе); для телефона лучше Tailscale.")
                print(f"🚀 На этом компьютере: http://localhost:{port}/?token={token}")
            urls = lan_urls(port, token) if everywhere else [f"http://{host}:{port}/?{TOKEN_PARAM}={token}"]
            for url in urls:
                print(f"📱 С телефона: {url}")
            print(f"   Ключ хранится в {config.data_path / 'web_token.txt'} — удали файл, чтобы сменить ключ.")
        print(f"   Лог: {config.data_path / 'logs' / 'ebeyparser.log'}. Ctrl+C — остановить.")
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
    config_path = Path(args.config)
    with _long_running(load_config(config_path)) as ok:
        if not ok:
            return 3
        config, db = _open(config_path)
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
    if args.reevaluate:
        print(f"♻  Сбросил старые оценки ({db.clear_evaluations()} шт.) — всё найденное оценю заново.")

    async def go():
        monitor = Monitor(config, db, web_base_url=_web_base_url(config))
        try:
            return await monitor.run_once()
        finally:
            await monitor.aclose()

    print("⏳ Проверяю поиски. Каждое новое объявление: страница, цены аналогов, фото и нейросеть —"
          " первый запуск может занять 10–20 минут, прогресс ниже.")
    from .runtime import file_logging

    with file_logging(config.data_path / "logs"):
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


def _site_client(config: AppConfig) -> Any:
    """PoliteClient sharing data/http_state.json with the monitor: diagnostics count against the
    same hourly page budget and respect block cooldowns."""
    from .runtime import http_state_path
    from .scraper.http import PoliteClient

    return PoliteClient.from_config(config.general, state_path=http_state_path(config.data_path))


def _site_refusal(exc: BaseException) -> str | None:
    """Russian explanation for our own budget / a block cooldown (no request was sent)."""
    from .scraper.categories import describe_error
    from .scraper.http import BlockedError, RateBudgetExceeded

    if isinstance(exc, RateBudgetExceeded):
        return (describe_error(exc) + ". Это защита от блокировки: запрос не отправлялся"
                " (лимит general.max_requests_per_hour).")
    if isinstance(exc, BlockedError):
        text = describe_error(exc)
        return text + (". Запрос не отправлялся — пауза растёт с каждой блокировкой." if exc.cooling_down else
                       f" ({exc}).")
    return None


def cmd_debug_search(args: argparse.Namespace) -> int:
    """Fetch page 1 of each Kleinanzeigen search and show what the parser sees."""
    from .scraper.kleinanzeigen import (
        KleinanzeigenScraper,
        build_search_url,
        page_diagnostics,
    )

    config = load_config(Path(args.config))
    searches = [s for s in config.searches if s.source == "kleinanzeigen" and (s.enabled or args.name)]
    if args.name:
        searches = [s for s in searches if s.name == args.name]
    if not searches:
        print("✖ Нет поисков Kleinanzeigen" + (f" с именем «{args.name}»" if args.name else ""))
        return 2

    async def go() -> int:
        client = _site_client(config)
        scraper = KleinanzeigenScraper(client, debug_dir=config.data_path / "debug")
        problems = 0
        try:
            for search in searches:
                url = build_search_url(search, 1)
                print(f"\n=== {search.name}")
                print(f"запрос:      {url}")
                try:
                    html = await client.get_text(url)
                except Exception as exc:
                    refusal = _site_refusal(exc)
                    problems += 1
                    if refusal is not None:  # every further request would be refused too
                        print(f"✖ {refusal}")
                        break
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
    from .scraper.kleinanzeigen import KleinanzeigenScraper, parse_ad_detail

    config = load_config(Path(args.config))

    from .monitor import ad_id_from_url

    if not ad_id_from_url(args.url) or "ebay." in args.url:
        print("✖ Нужна полная ссылка на объявление Kleinanzeigen, например\n"
              "  https://www.kleinanzeigen.de/s-anzeige/gigabyte-rtx-3080/3525778616-225-3331")
        return 2

    async def go() -> int:
        client = _site_client(config)
        scraper = KleinanzeigenScraper(client, debug_dir=config.data_path / "debug")
        try:
            html = await client.get_text(args.url)
        except Exception as exc:
            print(f"✖ {_site_refusal(exc) or f'Не удалось открыть объявление: {exc}'}")
            return 1
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


def _discover_categories_sync(config: AppConfig, location: str, radius_km: int) -> Any:
    """Live category list for a region (falls back to the built-in list, never raises)."""
    from .scraper.categories import (
        CategoryList,
        builtin_categories,
        describe_error,
        discover_categories,
        discovery_client,
    )

    async def go() -> Any:
        from .runtime import http_state_path

        client = discovery_client(config.general, state_path=http_state_path(config.data_path))
        try:
            return await discover_categories(client, location, radius_km)
        finally:
            await client.aclose()

    try:
        return asyncio.run(go())
    except Exception as exc:
        return CategoryList(builtin_categories(), error=describe_error(exc))


# ------------------------------------------------------------ setup: wizard
_YES = {"д", "да", "y", "yes", "j", "ja", "1", "+"}
_NO = {"н", "нет", "n", "no", "nein", "0", "-"}
_TG_TOKEN_RE = re.compile(r"^\d{5,}:[\w-]{20,}$")
_TG_CHAT_RE = re.compile(r"^(-?\d{3,}|@\w{4,})$")


def parse_money(text: str) -> float | None:
    """'400', '400 €', '1.000', '99,50' -> float; None if not a number."""
    t = text.replace("€", "").replace("\xa0", " ").strip().replace(" ", "")
    if re.fullmatch(r"\d{1,3}(\.\d{3})+", t):
        t = t.replace(".", "")
    t = t.replace(",", ".")
    try:
        value = float(t)
    except ValueError:
        return None
    return value if value >= 0 and value == value and value != float("inf") else None


def parse_selection(text: str, count: int) -> list[int] | None:
    """'1,3,5' / '2-4' / '1 2' -> 0-based indices in order; None when invalid."""
    t = re.sub(r"\s*-\s*", "-", text.strip().lower())
    out: list[int] = []
    for part in re.split(r"[,;\s]+", t):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", part)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo > hi:
                lo, hi = hi, lo
            numbers = list(range(lo, hi + 1))
        elif part.isdigit():
            numbers = [int(part)]
        else:
            return None
        if any(n < 1 or n > count for n in numbers):
            return None
        out.extend(n - 1 for n in numbers)
    return list(dict.fromkeys(out))


class _Prompter:
    """input()/print() wrapper: defaults on Enter / end of input, bounded re-asking."""

    def __init__(self, input_fn: Callable[[str], str], print_fn: Callable[..., None]) -> None:
        self.input_fn = input_fn
        self.say = print_fn

    def ask(self, question: str, default: str = "") -> str:
        hint = f" [{default}]" if default else ""
        try:
            answer = self.input_fn(f"{question}{hint}: ")
        except EOFError:
            answer = ""
        return (answer or "").strip() or default

    def yes(self, question: str, default: bool = True) -> bool:
        for _ in range(3):
            answer = self.ask(f"{question} [{'Д/н' if default else 'д/Н'}]").lower()
            if not answer:
                return default
            if answer in _YES:
                return True
            if answer in _NO:
                return False
            self.say("   Ответь «д» (да) или «н» (нет).")
        return default

    def number(self, question: str, default: float | None, *, integer: bool = False, allow_empty: bool = False,
               minimum: float = 0) -> float | None:
        shown = "" if default is None else (str(int(default)) if float(default).is_integer() else f"{default:g}")
        for _ in range(3):
            answer = self.ask(question, shown)
            if not answer:
                if allow_empty or default is not None:
                    return default
                self.say("   Нужно число.")
                continue
            value = parse_money(answer)
            if value is None or (integer and not value.is_integer()):
                self.say("   Нужно " + ("целое число" if integer else "число") + ", например 30.")
                continue
            if value < minimum:
                self.say(f"   Не меньше {minimum:g}.")
                continue
            return value
        return default


def _fmt_count(count: int | None) -> str:
    return f"{count:,}".replace(",", " ") if count is not None else ""


def run_setup(
    config_path: Path,
    *,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[..., None] = print,
    probe_fn: Callable[[str], Any] = probe_json,
    categories_fn: Callable[[str, int], Any] | None = None,
) -> int:
    """Interactive wizard (Russian): region, categories, budget, wishlist, interval, AI, Telegram.
    Makes no requests to Kleinanzeigen (built-in / cached category list) and writes nothing
    until the final confirmation; config.yaml comments are preserved."""
    from .scraper.categories import (
        RADIUS_CHOICES,
        SetupAnswers,
        answers_from_searches,
        available_categories,
        estimate_requests,
        merge_searches,
        searches_from_answers,
        snap_radius,
        suggest_interval,
    )

    p = _Prompter(input_fn, print_fn)
    say = p.say
    existed = config_path.is_file()
    config = load_config(config_path) if existed else AppConfig()
    prev = answers_from_searches(config.searches, config.pricing.min_profit) if existed else SetupAnswers(
        min_profit=config.pricing.min_profit)
    answers = SetupAnswers(min_profit=prev.min_profit)

    say("EbeyParser — настройка за 2 минуты.")
    say("Enter — взять значение в [скобках]. Ctrl+C — выйти, ничего не меняя.\n")
    try:
        # 1-2. region
        for _ in range(3):
            answers.location = p.ask("1/8 Город или почтовый индекс, где искать", prev.location or "Berlin")
            if answers.location:
                break
        if answers.location.islower():  # "berlin" -> "Berlin"
            answers.location = answers.location[0].upper() + answers.location[1:]
        radius = p.number(f"2/8 Радиус вокруг, км ({', '.join(map(str, RADIUS_CHOICES[1:]))})", prev.radius_km,
                          integer=True)
        answers.radius_km = snap_radius(int(radius if radius is not None else 30))
        if radius is not None and answers.radius_km != int(radius):
            say(f"   Kleinanzeigen ищет только с шагами радиуса — беру {answers.radius_km} км.")

        # 3. categories (no requests: cached live list or the built-in one)
        if categories_fn is None:
            categories = available_categories(config.data_path, answers.location, answers.radius_km)
        else:
            categories = categories_fn(answers.location, answers.radius_km)
        cats = list(categories)
        preselected = set(prev.category_ids)
        say("\n3/8 Какие категории сканировать? [x] — выбраны по умолчанию.")
        fetched = getattr(categories, "fetched_at", None)
        if getattr(categories, "live", False) and fetched is not None:
            say(f"   (список и число объявлений — с сайта, проверено {fetched.astimezone():%d.%m.%Y})")
        for n, cat in enumerate(cats, 1):
            if n > 1 and not cat.name_ru and cats[n - 2].name_ru:
                say("   — другие категории на сайте —")
            count = f"  · {_fmt_count(cat.count)} объявл." if cat.count is not None else ""
            say(f"   {n:>2}. [{'x' if cat.id in preselected else ' '}] {cat.label}{count}")
        chosen: list[int] = [c.id for c in cats if c.id in preselected]
        for _ in range(3):
            raw = p.ask("   Номера через запятую (1,3,5 или 2-4), «все» — все подходящие для перепродажи,"
                        " Enter — отмеченные")
            if not raw:
                break
            if raw.lower() in ("все", "all", "*"):
                chosen = [c.id for c in cats if c.resale_friendly]
                break
            if raw.lower() in ("0", "нет", "никакие"):
                chosen = []
                break
            picked = parse_selection(raw, len(cats))
            if picked is not None:
                chosen = [cats[i].id for i in picked]
                break
            say(f"   Не понял. Пиши номера от 1 до {len(cats)} через запятую.")
        answers.category_ids = chosen
        if chosen:
            say("   Выбрано: " + ", ".join(c.name_de for c in cats if c.id in chosen))

        # 4. purpose + budget
        purpose = p.ask("\n4/8 Для чего? 1 — перепродажа (считать прибыль), 2 — для себя (считать экономию)",
                        "2" if prev.purpose == "personal" else "1")
        answers.purpose = "personal" if purpose.strip().lower() in ("2", "для себя", "personal") else "resale"
        answers.max_price = p.number("   Максимальная цена за одну вещь, €", prev.max_price or 400.0, minimum=1)
        if answers.purpose == "resale":
            answers.min_profit = p.number("   С какой чистой прибыли сообщать о сделке, €", prev.min_profit) or 0.0

        # 5. wishlist
        answers.wishlist = _ask_wishlist(p, prev.wishlist)

        # 6. interval + request estimate
        n_searches = answers.search_count
        suggested = suggest_interval(n_searches, config.general)
        default = max(suggested, int(config.general.interval_minutes)) if existed else suggested
        say(f"\n6/8 Как часто проверять? Для {n_searches} {_plural(n_searches, 'поиска', 'поисков', 'поисков')}"
            f" безопасно — раз в {suggested} мин или реже (чаще — выше риск блокировки на Kleinanzeigen).")
        interval = p.number("   Интервал, минут", float(default), minimum=5) or float(default)
        answers.interval_minutes = interval
        estimate = estimate_requests(n_searches, interval, config.general)
        say(f"   {'⚠' if estimate.tight else 'ℹ'} {estimate.describe()}")

        # 7. local AI
        say("\n7/8 Локальная нейросеть (смотрит фото и описание). Ищу LM Studio и Ollama…")
        ai_updates = _ask_ai(p, detect_local_ai(probe_fn))

        # 8. Telegram
        env_updates, tg_updates = _ask_telegram(p, config)
    except KeyboardInterrupt:
        say("\nОтменено — ничего не записал.")
        return 1

    generated = searches_from_answers(answers, cats, global_min_profit=config.pricing.min_profit)
    if not generated:
        say("\n✖ Не выбрано ни одной категории и ничего «для себя» — поисков не будет. Запусти setup ещё раз.")
        return 1
    scans = sum(1 for s in generated if s.category_id and not s.query)
    say(f"\nИтого: {scans} {_plural(scans, 'поиск', 'поиска', 'поисков')} по категориям"
        f" и {len(generated) - scans} «для себя», проверка раз в {interval:g} мин.")
    replace_all = True
    if existed and config.searches:
        replace_all = p.yes(f"В {config_path.name} уже есть поиски ({len(config.searches)}). Заменить их новыми?"
                            f" (старый файл сохраню как {config_path.name}.bak)")
    if not p.yes(f"Записать настройки в {config_path}?"):
        say("Ничего не записал.")
        return 1

    # ---- write (only now)
    if existed:
        shutil.copyfile(config_path, config_path.with_name(config_path.name + ".bak"))
    elif EXAMPLE_CONFIG.is_file():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EXAMPLE_CONFIG, config_path)
    else:
        config_path.write_text("", encoding="utf-8")
    base = config.searches if existed else []
    save_searches_block(config_path, merge_searches(base, generated, replace_all=replace_all))
    updates: dict[str, Any] = {"general.interval_minutes": interval, **ai_updates, **tg_updates}
    update_yaml_values(config_path, updates)
    env_path = config_path.parent / ".env"
    if env_updates:
        write_env_values(env_path, env_updates, set_environ=False)
    try:
        saved = load_config(config_path)
    except ConfigError as exc:  # should not happen; say it plainly instead of a traceback
        say(f"⚠ Проверь {config_path}: {exc}")
        return 1

    say(f"\n✔ Сохранено в {config_path}: {len(generated)} "
        f"{_plural(len(generated), 'поиск', 'поиска', 'поисков')}"
        + (f" (копия старого — {config_path.name}.bak)" if existed else ""))
    if env_updates:
        say(f"✔ Токен Telegram записан в {env_path} (в config.yaml только ссылка на него).")
    if getattr(saved.general, "baseline_first_run", False):
        say("ℹ Первая проверка нового поиска только изучает цены в категории — уведомления начнутся со следующей.")
    say("\nДальше:")
    if ai_updates.get("ai.enabled"):
        say("  python -m ebeyparser ai-check      — проверить нейросеть")
    if env_updates:
        say("  python -m ebeyparser test-notify   — тестовое сообщение в Telegram")
    say("  python -m ebeyparser once          — первая проверка прямо сейчас (может занять 10–20 минут)")
    say("  python -m ebeyparser run           — панель http://localhost:8000 + проверки 24/7")
    say("Поиски потом можно поменять в панели: страница «Настройка» (http://localhost:8000/setup).")
    return 0


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _ask_ai(p: _Prompter, servers: list[LocalAIServer]) -> dict[str, Any]:
    """Pick server + vision model -> config updates for the `ai` section."""
    for s in servers:
        vision = s.vision_models
        p.say(f"   ✔ {s.name} ({s.base_url}): моделей {len(s.models)}, видят фото: "
              + (", ".join(vision[:3]) if vision else "ни одной"))
    if not servers:
        p.say("   Не нашёл: LM Studio (localhost:1234) и Ollama (localhost:11434) не отвечают.")
        if p.yes("   Настроить под LM Studio с моделью Qwen2.5-VL-7B? Сервер можно запустить позже"):
            p.say("   В LM Studio: скачай Qwen2.5-VL-7B (вкладка Discover) → Developer → Start Server.")
            return {"ai.enabled": True, "ai.provider": "openai", "ai.base_url": LMSTUDIO_URL,
                    "ai.model": DEFAULT_LMSTUDIO_MODEL}
        p.say("   Хорошо, без нейросети: оценка только по ценам. Включить позже — python -m ebeyparser setup.")
        return {"ai.enabled": False}

    server = next((s for s in servers if s.vision_models), servers[0])
    if len(servers) > 1:
        options = ", ".join(f"{i} — {s.name}" for i, s in enumerate(servers, 1))
        answer = p.ask(f"   Какую использовать? {options}, 0 — без нейросети", str(servers.index(server) + 1))
        if answer.strip() == "0":
            return {"ai.enabled": False}
        if answer.strip().isdigit() and 1 <= int(answer) <= len(servers):
            server = servers[int(answer) - 1]
    vision = server.vision_models
    if not vision:
        model = DEFAULT_LMSTUDIO_MODEL if server.provider == "openai" else "qwen2.5vl:7b"
        p.say(f"   ⚠ В {server.name} нет модели, которая видит фото. Скачай Qwen2.5-VL-7B"
              + (" (вкладка Discover)." if server.provider == "openai" else " командой: ollama pull qwen2.5vl:7b")
              + f" Пока впишу {model}.")
    elif len(vision) == 1:
        model = vision[0]
    else:
        for i, name in enumerate(vision, 1):
            p.say(f"   {i}. {name}")
        answer = p.ask("   Какую модель использовать", "1")
        model = vision[int(answer) - 1] if answer.isdigit() and 1 <= int(answer) <= len(vision) else vision[0]
    if len(servers) == 1 and not p.yes(f"   Использовать {server.name} с моделью {model}?"):
        return {"ai.enabled": False}
    return {"ai.enabled": True, "ai.provider": server.provider, "ai.base_url": server.base_url, "ai.model": model}


def _ask_telegram(p: _Prompter, config: AppConfig) -> tuple[dict[str, str], dict[str, Any]]:
    """-> (.env values, config updates). Secrets go to .env only."""
    p.say("\n8/8 Уведомления в Telegram (Enter — пропустить).")
    tg = config.notifications.telegram
    if tg.enabled and tg.bot_token and tg.chat_id and not p.yes("   Telegram уже настроен. Изменить?", False):
        return {}, {}
    p.say("   1) В Telegram напиши @BotFather → /newbot → он пришлёт токен бота.")
    p.say("   2) Напиши своему боту любое сообщение, а свой chat_id узнай у @userinfobot.")
    token = ""
    for _ in range(3):
        token = p.ask("   Токен бота")
        if not token or _TG_TOKEN_RE.match(token):
            break
        p.say("   Не похоже на токен (он выглядит как 123456789:AAH…). Ещё раз или Enter — пропустить.")
        token = ""
    if not token:
        return {}, {}
    chat_id = ""
    for _ in range(3):
        chat_id = p.ask("   Твой chat_id (число)")
        if not chat_id or _TG_CHAT_RE.match(chat_id):
            break
        p.say("   chat_id — это число, например 123456789. Его пишет @userinfobot.")
        chat_id = ""
    if not chat_id:
        p.say("   Без chat_id бот не знает, кому писать — Telegram пропускаю.")
        return {}, {}
    return (
        {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": chat_id},
        {"notifications.telegram.enabled": True,
         "notifications.telegram.bot_token": "${TELEGRAM_BOT_TOKEN}",
         "notifications.telegram.chat_id": "${TELEGRAM_CHAT_ID}"},
    )


def _ask_wishlist(p: _Prompter, current: list[tuple[str, float | None]]) -> list[tuple[str, float | None]]:
    p.say("\n5/8 Хочешь купить что-то для себя? Например, детали для AI-сервера.")
    p.say("   Программа будет искать эти вещи и сообщит, когда цена ниже твоего лимита.")
    items: list[tuple[str, float | None]] = []
    if current:
        listed = "; ".join(f"{q} (до {_money(t)})" if t else q for q, t in current)
        if p.yes(f"   Сейчас в списке: {listed}. Оставить?"):
            items = list(current)
    for _ in range(20):
        item = p.ask("   Что ищешь (Enter — закончить)")
        if not item:
            break
        price = p.number("   Сколько готов заплатить максимум, € (Enter — без лимита)", None, allow_empty=True,
                         minimum=1)
        items.append((item, price))
        p.say(f"   ✔ «{item}»" + (f" до {_money(price)}" if price else "") + " — добавлено")
    return items


def cmd_setup(args: argparse.Namespace) -> int:
    return run_setup(Path(args.config))


def cmd_categories(args: argparse.Namespace) -> int:
    """Category IDs for search configs: built-in list (instant) or, with --live, the site's
    list for the region (1–2 requests, cached for 7 days in data/categories.json)."""
    from .scraper.categories import (
        CACHE_MAX_AGE_DAYS,
        available_categories,
        save_cached_categories,
        snap_radius,
    )

    config = load_config(Path(args.config))
    radius = snap_radius(args.radius)
    where = f"{args.location} + {radius} км" if radius else args.location
    if args.live:
        print(f"⏳ Спрашиваю у Kleinanzeigen категории: {where}…")
        cats = _discover_categories_sync(config, args.location, radius)
        if cats.live:
            save_cached_categories(config.data_path, args.location, radius, cats)
            print(f"✔ Категории с сайта ({cats.url}) — запомнил на {CACHE_MAX_AGE_DAYS} дней")
        else:
            print(f"⚠ С сайта получить не удалось: {cats.error}.\n  Показываю встроенный справочник.")
    else:
        cats = available_categories(config.data_path, args.location, radius)
        if cats.live and cats.fetched_at is not None:
            print(f"Категории для {where} — с сайта, проверено {cats.fetched_at.astimezone():%d.%m.%Y}"
                  " (обновить: --live)")
        else:
            print("Встроенный справочник категорий (без запросов к сайту)."
                  f"\nСверить с сайтом и узнать число объявлений в {where}: python -m ebeyparser categories --live")
    print(f"\n{'ID':>6}  {'объявлений':>10}  категория")
    for cat in cats:
        mark = "  ★ для перепродажи" if cat.resale_friendly else ""
        check = "  (на странице региона не видно)" if cats.live and not cat.verified else ""
        print(f"{cat.id:>6}  {_fmt_count(cat.count):>10}  {cat.label}{mark}{check}")
    print("\nСканировать категории проще всего мастером: python -m ebeyparser setup"
          "\n(или добавь в config.yaml поиск с category_id: <ID>, location и radius_km).")
    return 1 if args.live and not cats.live else 0


def cmd_benchmark(args: argparse.Namespace) -> int:
    """Hook for the optional `ebeyparser.benchmark` module (run_cli(args))."""
    try:
        module = importlib.import_module("ebeyparser.benchmark")
    except ModuleNotFoundError as exc:
        if exc.name not in (None, "ebeyparser.benchmark"):
            raise
        print("✖ Бенчмарк не установлен в этой версии (нет модуля ebeyparser.benchmark).")
        return 2
    add_arguments = getattr(module, "add_cli_arguments", None) or getattr(module, "add_arguments", None)
    if add_arguments is not None:  # let the module declare its own options
        sub = argparse.ArgumentParser(prog="ebeyparser benchmark")
        add_arguments(sub)
        for key, value in vars(sub.parse_args(args.benchmark_args)).items():
            setattr(args, key, value)
    return int(module.run_cli(args) or 0)


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
    p.add_argument("--reevaluate", action="store_true",
                   help="переоценить и уже виденные объявления (после изменения настроек)")
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

    sub.add_parser("setup", help="мастер настройки: город, категории, бюджет, нейросеть, Telegram").set_defaults(
        func=cmd_setup)
    p = sub.add_parser("categories", help="номера категорий Kleinanzeigen (--live — сверить с сайтом и узнать число объявлений)")
    p.add_argument("--location", default="Berlin", help="город или почтовый индекс (по умолчанию Berlin)")
    p.add_argument("--radius", type=int, default=30, help="радиус, км (по умолчанию 30)")
    p.add_argument("--live", action="store_true",
                   help="спросить у сайта (1–2 запроса; результат запоминается на 7 дней)")
    p.set_defaults(func=cmd_categories)
    # '+' prefix: every argument after `benchmark` (even --flags) is passed through to the module
    p = sub.add_parser("benchmark", help="бенчмарк оценки сделок (если модуль установлен)",
                       prefix_chars="+", add_help=False)
    p.add_argument("benchmark_args", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_benchmark)
    return parser


def main(argv: list[str] | None = None) -> int:
    from .runtime import force_utf8_console

    force_utf8_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args([*(argv or sys.argv[1:]), "run"])
    for name, default in (("host", None), ("port", None), ("no_monitor", False)):
        if not hasattr(args, name):
            setattr(args, name, default)
    _setup_logging(args.verbose)
    try:
        return args.func(args) or 0
    except ConfigError as exc:
        print(f"✖ {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
