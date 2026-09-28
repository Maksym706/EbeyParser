"""Setup wizard (CLI), comment-preserving config writers, local AI detection, CLI hooks, 24/7 runtime."""

from __future__ import annotations

import logging
import os
import sys
import types
from pathlib import Path
from typing import Any

import pytest
import yaml

from ebeyparser import cli
from ebeyparser.config import SearchConfig, load_config
from ebeyparser.runtime import AlreadyRunningError, InstanceLock, file_logging, keep_awake, onedrive_warning
from ebeyparser.scraper.categories import builtin_categories, merge_categories, parse_category_links
from ebeyparser.web.configfile import read_env_file, save_searches_block, update_yaml_values, write_env_values
from ebeyparser.web.localai import detect_local_ai

TOKEN = "123456789:AAHfakeTokenFakeTokenFake_12345"
LMSTUDIO_MODELS = {"data": [{"id": "text-embedding-nomic-embed-text-v1.5"}, {"id": "google/gemma-3-4b"},
                            {"id": "qwen/qwen2.5-vl-7b"}]}


@pytest.fixture(autouse=True)
def _restore_environ():
    """load_config() loads the .env next to the config into os.environ: keep tests isolated."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


class Script:
    """input() replacement: scripted answers, then EOF (= Enter / default everywhere)."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)


def lmstudio_only(url: str) -> Any:
    return LMSTUDIO_MODELS if url.startswith("http://localhost:1234") else None


def nothing(url: str) -> Any:
    return None


def run_wizard(path: Path, script: Script, probe=lmstudio_only, categories=None) -> tuple[int, str]:
    out: list[str] = []
    rc = cli.run_setup(path, input_fn=script, print_fn=lambda *a, **k: out.append(" ".join(map(str, a))),
                       probe_fn=probe, categories_fn=categories or (lambda loc, r: builtin_categories()))
    return rc, "\n".join(out)


# ------------------------------------------------------------------- wizard
def test_setup_wizard_end_to_end_creates_config_and_env(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    script = Script(
        "berlin",          # 1 city
        "25",              # 2 radius -> snapped to 30
        "1,2,4",           # 3 categories: Handy, Notebooks, PC-Zubehör
        "",                # 4 purpose: resale (default)
        "300",             #   budget
        "50",              #   min profit
        "RTX 3090", "550",  # 5 wishlist item
        "",                #   end of wishlist
        "",                # 6 interval: suggested
        "", "",            # 7 LM Studio: model #1 (qwen), use it? yes
        TOKEN, "987654",   # 8 Telegram
        "",                # write? yes
    )
    rc, out = run_wizard(path, script)
    assert rc == 0, out
    assert "беру 30 км" in out and "Выбрано: Handy & Telefon, Notebooks, PC-Zubehör & Software" in out
    assert "стр. выдачи в час" in out and "python -m ebeyparser once" in out
    config = load_config(path)
    assert [s.name for s in config.searches] == [
        "Handy & Telefon · Berlin 30 км", "Notebooks · Berlin 30 км", "PC-Zubehör & Software · Berlin 30 км",
        "Для себя: RTX 3090"]
    phone = config.searches[0]
    assert phone.category_id == 173 and phone.location == "Berlin" and phone.radius_km == 30
    assert phone.max_price == 300 and phone.min_profit == 50 and "ersatzteil" in phone.exclude_keywords
    wish = config.searches[-1]
    assert wish.purpose == "personal" and wish.target_price == 550 and wish.query == "RTX 3090"
    assert config.general.interval_minutes == 10  # 4 searches -> suggested 10 min
    assert (config.ai.enabled, config.ai.provider, config.ai.base_url, config.ai.model) == (
        True, "openai", "http://localhost:1234/v1", "qwen/qwen2.5-vl-7b")
    assert config.notifications.telegram.enabled
    text = path.read_text(encoding="utf-8")
    assert TOKEN not in text and "${TELEGRAM_BOT_TOKEN}" in text  # secrets only in .env
    assert "# ─── Как считать выгоду" in text and "# ── Ollama вместо LM Studio ──" in text  # comments kept
    env = read_env_file(tmp_path / ".env")
    assert env["TELEGRAM_BOT_TOKEN"] == TOKEN and env["TELEGRAM_CHAT_ID"] == "987654"
    assert not (tmp_path / "config.yaml.bak").exists()


def test_setup_wizard_updates_existing_config(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "# мой конфиг\ngeneral:\n  interval_minutes: 30   # не чаще\n  data_dir: data\n"
        "searches:\n  - name: Старый поиск\n    query: dyson\n"
        "notifications:\n  email:\n    password: ${SMTP_PASSWORD}\n",
        encoding="utf-8")
    script = Script("", "", "3", "2", "200", "", "", "n", "", "")  # keep Berlin/30, Konsolen, personal, no AI
    rc, out = run_wizard(path, script, probe=nothing)
    assert rc == 0, out
    assert "без нейросети" in out
    config = load_config(path)
    assert [s.name for s in config.searches] == ["Konsolen · Berlin 30 км"]  # replaced (default yes)
    assert config.searches[0].purpose == "personal" and config.searches[0].min_profit is None
    assert config.general.interval_minutes == 30  # user's slower interval kept
    assert config.ai.enabled is False
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["notifications"]["email"]["password"] == "${SMTP_PASSWORD}"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# мой конфиг") and "interval_minutes: 30   # не чаще" in text
    assert "dyson" in (tmp_path / "config.yaml.bak").read_text(encoding="utf-8")


def test_setup_wizard_keeps_manual_searches_when_asked(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    save_searches_block(path, [SearchConfig(name="Старый поиск", query="dyson")])
    # city, radius, Handy, resale, budget, profit, no wishlist, interval, model #1, AI "no", no Telegram,
    # replace existing? "no", write? yes
    script = Script("Leipzig", "20", "1", "", "", "", "", "", "", "н", "", "н", "")
    rc, out = run_wizard(path, script)
    assert rc == 0, out
    names = [s.name for s in load_config(path).searches]
    assert names == ["Старый поиск", "Handy & Telefon · Leipzig 20 км"]


def test_setup_wizard_cancel_and_nothing_selected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"

    def interrupt(prompt: str) -> str:
        raise KeyboardInterrupt

    out: list[str] = []
    assert cli.run_setup(path, input_fn=interrupt, print_fn=out.append, probe_fn=nothing,
                         categories_fn=lambda loc, r: builtin_categories()) == 1
    assert "ничего не записал" in out[-1] and not path.exists()
    rc, out_text = run_wizard(path, Script("", "", "нет"), probe=nothing)
    assert rc == 1 and "поисков не будет" in out_text and not path.exists()
    rc, out_text = run_wizard(path, Script("", "", "", "", "", "", "", "", "", "", "н"), probe=nothing)
    assert rc == 1 and "Ничего не записал" in out_text and not path.exists()


def test_setup_wizard_shows_cached_live_categories(tmp_path: Path) -> None:
    html = ('<a href="/s-berlin/handy-telefon/c173l3331r30">Handy &amp; Telefon</a> (1.234)'
            '<a href="/s-berlin/sammeln/c234l3331r30">Sammeln</a> (55)')
    live = merge_categories(parse_category_links(html))
    rc, out = run_wizard(tmp_path / "config.yaml", Script("", "", "17", "", "", "", "", "", "", "", "н"),
                         probe=nothing, categories=lambda loc, r: live)
    assert "1 234 объявл." in out and "другие категории на сайте" in out
    assert "Выбрано: Sammeln" in out and rc == 1  # declined writing at the end


def test_parse_helpers() -> None:
    assert cli.parse_selection("1,3, 5", 5) == [0, 2, 4]
    assert cli.parse_selection("2-4 1", 5) == [1, 2, 3, 0]
    assert cli.parse_selection("4 - 2", 5) == [1, 2, 3]
    assert cli.parse_selection("1,1,2", 3) == [0, 1]
    assert cli.parse_selection("7", 5) is None and cli.parse_selection("a", 5) is None
    assert cli.parse_money("1.000") == 1000 and cli.parse_money("99,50 €") == 99.5
    assert cli.parse_money("abc") is None and cli.parse_money("-5") is None


def test_detect_local_ai_prefers_lmstudio_and_vision_models() -> None:
    def both(url: str) -> Any:
        if "1234" in url:
            return LMSTUDIO_MODELS
        return {"models": [{"name": "llama3.1:8b"}, {"name": "qwen2.5vl:7b"}]}

    servers = detect_local_ai(both)
    assert [s.name for s in servers] == ["LM Studio", "Ollama"]
    assert servers[0].vision_models == ["qwen/qwen2.5-vl-7b", "google/gemma-3-4b"]
    assert servers[0].default_model == "qwen/qwen2.5-vl-7b" and servers[1].default_model == "qwen2.5vl:7b"
    assert detect_local_ai(nothing) == []
    assert detect_local_ai(lambda url: {"unexpected": True}) == []


# ------------------------------------------------------------ config writers
def test_update_yaml_values_keeps_comments_and_inserts(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "# header\ngeneral:\n  interval_minutes: 15   # как часто\n\n"
        "ai:\n  enabled: true\n  base_url: ${OLLAMA_URL:-http://localhost:11434}   # LM Studio: :1234\n"
        "  # model: commented-out\n  model: qwen2.5vl:7b\n  second_opinion:\n    model: claude-opus-5\n"
        "notifications:\n  min_score: 70\n",
        encoding="utf-8")
    update_yaml_values(path, {
        "general.interval_minutes": 20.0,
        "ai.base_url": "http://localhost:1234/v1",
        "ai.model": "qwen/qwen2.5-vl-7b",
        "notifications.telegram.enabled": True,
        "notifications.telegram.bot_token": "${TELEGRAM_BOT_TOKEN}",
        "pricing.max_capital": None,
        "web.port": 8080,
    })
    text = path.read_text(encoding="utf-8")
    assert "  interval_minutes: 20   # как часто\n" in text
    assert "  base_url: http://localhost:1234/v1   # LM Studio: :1234\n" in text
    assert "  # model: commented-out\n  model: qwen/qwen2.5-vl-7b\n" in text
    assert "    model: claude-opus-5" in text  # nested second_opinion untouched
    data = yaml.safe_load(text)
    assert data["notifications"]["telegram"] == {"enabled": True, "bot_token": "${TELEGRAM_BOT_TOKEN}"}
    assert data["notifications"]["min_score"] == 70 and data["web"]["port"] == 8080
    assert data["pricing"] == {"max_capital": None} and text.startswith("# header")


def test_update_yaml_values_falls_back_on_flow_style(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("ai: {enabled: true, model: x}\nsearches: []\n", encoding="utf-8")
    update_yaml_values(path, {"ai.model": "qwen/qwen2.5-vl-7b"})
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["ai"] == {"enabled": True, "model": "qwen/qwen2.5-vl-7b"}


def test_save_searches_block_only_touches_searches(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "general:\n  interval_minutes: 15   # comment A\n\n"
        "# ─── Что мониторить ───\nsearches:\n  - name: old\n    query: x   # gone with the block\n\n"
        "# ─── Как считать выгоду ───\npricing:\n  min_profit: 40   # comment B\n",
        encoding="utf-8")
    searches = [SearchConfig(name="Handy & Telefon · Berlin 30 км", category_id=173, location="Berlin",
                             radius_km=30, exclude_keywords=["defekt"]),
                SearchConfig(name="Для себя: RTX 3090", query="rtx 3090", purpose="personal", target_price=550)]
    save_searches_block(path, searches)
    text = path.read_text(encoding="utf-8")
    for kept in ("# comment A", "# ─── Что мониторить ───\nsearches:", "# ─── Как считать выгоду ───\npricing:",
                 "min_profit: 40   # comment B"):
        assert kept in text
    assert "old" not in text and "include_keywords" not in text  # compact: defaults left out
    config = load_config(path)
    assert [s.name for s in config.searches] == [s.name for s in searches]
    assert config.searches[1].target_price == 550 and config.pricing.min_profit == 40
    save_searches_block(path, [])
    assert load_config(path).searches == [] and "# comment B" in path.read_text(encoding="utf-8")
    fresh = tmp_path / "new.yaml"
    save_searches_block(fresh, searches[:1])
    assert load_config(fresh).searches[0].category_id == 173


def test_write_env_values(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# secrets\nSMTP_USER=me@example.com\nTELEGRAM_BOT_TOKEN=\n", encoding="utf-8")
    write_env_values(env, {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "42"})
    assert env.read_text(encoding="utf-8").splitlines() == [
        "# secrets", "SMTP_USER=me@example.com", f"TELEGRAM_BOT_TOKEN={TOKEN}", "TELEGRAM_CHAT_ID=42"]
    assert os.environ["TELEGRAM_CHAT_ID"] == "42"  # the running app sees it (restored by the fixture)


# ---------------------------------------------------------------- CLI hooks
def test_categories_command_offline_and_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"general:\n  data_dir: {tmp_path / 'data'}\n", encoding="utf-8")
    assert cli.main(["-c", str(cfg), "categories"]) == 0
    out = capsys.readouterr().out
    assert "без запросов к сайту" in out and "Handy & Telefon" in out and "173" in out

    html = '<a href="/s-berlin/handy-telefon/c173l3331r30">Handy &amp; Telefon</a> (1.234)'
    live = merge_categories(parse_category_links(html))
    monkeypatch.setattr(cli, "_discover_categories_sync", lambda config, loc, r: live)
    assert cli.main(["-c", str(cfg), "categories", "--live", "--location", "Berlin", "--radius", "30"]) == 0
    assert "запомнил на 7 дней" in capsys.readouterr().out
    assert cli.main(["-c", str(cfg), "categories"]) == 0  # now served from data/categories.json
    out = capsys.readouterr().out
    assert "с сайта, проверено" in out and "1 234" in out

    failed = builtin_categories()
    failed.error = "нет связи"
    monkeypatch.setattr(cli, "_discover_categories_sync", lambda config, loc, r: failed)
    assert cli.main(["-c", str(cfg), "categories", "--live", "--location", "Hamburg"]) == 1
    assert "нет связи" in capsys.readouterr().out


def test_benchmark_hook(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    calls: list[Any] = []
    fake = types.ModuleType("ebeyparser.benchmark")

    def add_cli_arguments(parser) -> None:
        parser.add_argument("-n", type=int, default=600)

    fake.add_cli_arguments = add_cli_arguments  # type: ignore[attr-defined]
    fake.run_cli = lambda args: calls.append(args) or 0  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ebeyparser.benchmark", fake)
    assert cli.main(["benchmark", "-n", "40"]) == 0
    assert calls and calls[0].n == 40 and calls[0].benchmark_args == ["-n", "40"]

    monkeypatch.setitem(sys.modules, "ebeyparser.benchmark", None)  # "not installed"
    assert cli.main(["benchmark"]) == 2
    assert "не установлен" in capsys.readouterr().out


# ------------------------------------------------------------------ runtime
def test_instance_lock_refuses_second_instance(tmp_path: Path) -> None:
    path = tmp_path / "data" / "ebeyparser.lock"
    first = InstanceLock(path)
    first.acquire()
    try:
        with pytest.raises(AlreadyRunningError) as err:
            InstanceLock(path).acquire()
        assert "уже работает" in str(err.value) and "PID" in str(err.value)
    finally:
        first.release()
    with InstanceLock(path):  # a lock file left behind is not "stale": free again
        pass
    path.write_text("99999999\n01.01.2026 10:00\n", encoding="utf-8")  # crashed process
    with InstanceLock(path):
        assert path.read_text(encoding="utf-8").split("\n")[0].isdigit()


def test_run_refuses_when_already_running(tmp_path: Path, capsys) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"general:\n  data_dir: {tmp_path / 'data'}\n", encoding="utf-8")
    with InstanceLock(tmp_path / "data" / "ebeyparser.lock"):
        assert cli.main(["-c", str(cfg), "monitor"]) == 3
    assert "уже работает" in capsys.readouterr().out


def test_file_logging_and_helpers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with file_logging(tmp_path / "logs") as path:
        logging.getLogger("ebeyparser.test").warning("привет из теста ✔")
    assert path is not None and "привет из теста ✔" in path.read_text(encoding="utf-8")
    assert not any(getattr(h, "baseFilename", "") == str(path) for h in logging.getLogger().handlers)
    with keep_awake() as awake:
        if sys.platform != "win32":
            assert awake is False
    monkeypatch.setenv("OneDrive", str(tmp_path / "OneDrive - Uni"))
    assert onedrive_warning(tmp_path / "OneDrive - Uni" / "EbeyParser" / "data") is not None
    assert onedrive_warning(tmp_path / "EbeyParser" / "data") is None


def test_debug_search_respects_shared_cooldown(tmp_path: Path, capsys) -> None:
    """Diagnostics use data/http_state.json: during a block cooldown no request is sent."""
    import asyncio

    import httpx

    from ebeyparser.scraper.http import BlockedError, PoliteClient

    data = tmp_path / "data"

    async def block() -> None:
        client = PoliteClient(delay_range=(0, 0), max_retries=0, state_path=data / "http_state.json",
                              transport=httpx.MockTransport(lambda request: httpx.Response(403)))
        with pytest.raises(BlockedError):
            await client.get_text("https://www.kleinanzeigen.de/s-foo/k0")
        await client.aclose()

    asyncio.run(block())
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"general:\n  data_dir: {data}\nsearches:\n  - name: Handy\n    category_id: 173\n"
                   "    location: Berlin\n", encoding="utf-8")
    assert cli.main(["-c", str(cfg), "debug-search"]) == 1
    out = capsys.readouterr().out
    assert "на паузе после блокировки" in out and "Запрос не отправлялся" in out
