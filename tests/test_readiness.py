"""Pre-deployment readiness coverage for safety-critical bot behavior."""

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import config
from config import Settings, load_settings
from broker import BrokerSnapshot
from notifications.discord_notifier import DiscordNotifier
from notifications.notifier import DailySummaryNotifier
from openai_logic import AIDecision, AIDecisionError, OpenAIDecisionClient, TradingContext
from risk_manager import RiskManager
from scheduler import MarketScheduler
from storage import TradingJournal
from strategy import TradingStrategy


def make_settings(**overrides):
    values = {
        "bot_enabled": True,
        "paper_trading": True,
        "dry_run": False,
        "trading_interval_minutes": 15,
        "market_timezone": "America/New_York",
        "openai_api_key": "test-key",
        "openai_model": "test-model",
        "alpaca_api_key": "test-alpaca-key",
        "alpaca_secret_key": "test-alpaca-secret",
        "alpaca_paper_base_url": "https://paper-api.alpaca.markets",
        "max_position_allocation_percent": 5,
        "min_confidence": 0.7,
        "allowed_symbols": ["AAPL", "MSFT"],
        "prompts_dir": Path("prompts"),
        "data_dir": Path("data"),
        "discord_webhook_url": "",
        "discord_daily_summary_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


def test_config_loading_uses_safe_defaults(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda dotenv_path=None: False)
    for key in [
        "BOT_ENABLED",
        "PAPER_TRADING",
        "DRY_RUN",
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "DISCORD_WEBHOOK_URL",
        "DISCORD_DAILY_SUMMARY_ENABLED",
    ]:
        monkeypatch.delenv(key, raising=False)

    settings = load_settings()

    assert settings.bot_enabled is False
    assert settings.paper_trading is True
    assert settings.dry_run is True
    assert settings.discord_daily_summary_enabled is False
    assert settings.openai_model == "gpt-5-mini"


def test_config_loading_reads_environment(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda dotenv_path=None: False)
    monkeypatch.setenv("BOT_ENABLED", "true")
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("DISCORD_DAILY_SUMMARY_ENABLED", "true")
    monkeypatch.setenv("ALLOWED_SYMBOLS", "aapl, msft")

    settings = load_settings()

    assert settings.bot_enabled is True
    assert settings.paper_trading is True
    assert settings.dry_run is False
    assert settings.discord_daily_summary_enabled is True
    assert settings.allowed_symbols == ["AAPL", "MSFT"]


def test_prompt_files_are_loaded_and_rendered_at_runtime(tmp_path):
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "system_prompt.md").write_text("System prompt", encoding="utf-8")
    (prompts_dir / "user_prompt_template.md").write_text(
        "Now: {{current_datetime}}\nWatchlist: {{watchlist}}",
        encoding="utf-8",
    )

    client = object.__new__(OpenAIDecisionClient)
    client.prompts_dir = prompts_dir
    context = TradingContext(
        current_datetime=datetime(2026, 7, 6, 10, 0, tzinfo=ZoneInfo("America/New_York")),
        market_status="open",
        watchlist_symbols=["aapl"],
    )

    assert client._load_prompt("system_prompt.md") == "System prompt"
    rendered = client._render_user_prompt(context)
    assert "2026-07-06" in rendered
    assert "AAPL" in rendered


def test_ai_response_validation_accepts_supported_decision():
    decision = AIDecision.model_validate(
        {
            "symbol": "aapl",
            "action": "buy",
            "confidence": 0.72,
            "suggested_allocation_percent": 5,
            "reason": "Test reason.",
        }
    )

    assert decision.symbol == "AAPL"
    assert decision.action == "BUY"


def test_invalid_ai_json_is_rejected():
    client = object.__new__(OpenAIDecisionClient)

    with pytest.raises(AIDecisionError):
        client._parse_decision("not json")


def test_invalid_ai_response_schema_is_rejected():
    client = object.__new__(OpenAIDecisionClient)

    with pytest.raises(AIDecisionError):
        client._parse_decision(
            '{"symbol":"AAPL","action":"WAIT","confidence":1.2,'
            '"suggested_allocation_percent":5,"reason":"Invalid."}'
        )


def test_risk_manager_rejects_when_bot_disabled():
    manager = RiskManager(make_settings(bot_enabled=False))

    approved, reason = manager.validate(
        {
            "symbol": "AAPL",
            "action": "BUY",
            "confidence": 0.9,
            "suggested_allocation_percent": 1,
        }
    )

    assert approved is False
    assert "BOT_ENABLED" in reason


def test_risk_manager_rejects_dry_run_mode():
    manager = RiskManager(make_settings(dry_run=True))

    approved, reason = manager.validate(
        {
            "symbol": "AAPL",
            "action": "BUY",
            "confidence": 0.9,
            "suggested_allocation_percent": 1,
        }
    )

    assert approved is False
    assert "DRY_RUN" in reason


def test_risk_manager_rejects_when_paper_trading_disabled():
    manager = RiskManager(make_settings(paper_trading=False))

    approved, reason = manager.validate(
        {
            "symbol": "AAPL",
            "action": "BUY",
            "confidence": 0.9,
            "suggested_allocation_percent": 1,
        }
    )

    assert approved is False
    assert "Live trading is disabled" in reason


def test_risk_manager_rejects_unsupported_symbols():
    manager = RiskManager(make_settings(allowed_symbols=["AAPL"]))

    approved, reason = manager.validate(
        {
            "symbol": "TSLA",
            "action": "BUY",
            "confidence": 0.9,
            "suggested_allocation_percent": 1,
        }
    )

    assert approved is False
    assert "ALLOWED_SYMBOLS" in reason


def test_risk_manager_rejects_allocation_above_max_limit():
    manager = RiskManager(make_settings(max_position_allocation_percent=5))

    approved, reason = manager.validate(
        {
            "symbol": "AAPL",
            "action": "BUY",
            "confidence": 0.9,
            "suggested_allocation_percent": 10,
        }
    )

    assert approved is False
    assert "exceeds maximum" in reason


def test_invalid_or_missing_openai_decision_falls_back_to_hold():
    class FailingAIClient:
        def get_decision(self, context):
            raise AIDecisionError("missing response")

    strategy = TradingStrategy(
        settings=make_settings(),
        broker=None,
        risk_manager=RiskManager(make_settings()),
    )
    strategy.ai_client = FailingAIClient()
    snapshot = BrokerSnapshot(
        account={"cash": 1000, "buying_power": 1000, "portfolio_value": 1000},
        positions=[],
        market_data={},
    )

    decision = strategy.get_ai_decision(snapshot)

    assert decision["action"] == "HOLD"
    assert decision["suggested_allocation_percent"] == 0


def test_rejected_ai_decision_does_not_reach_broker_execution():
    class StubBroker:
        def __init__(self):
            self.execute_called = False

        def collect_snapshot(self):
            return BrokerSnapshot(
                account={"cash": 1000, "buying_power": 1000, "portfolio_value": 1000},
                positions=[],
                market_data={},
            )

        def execute_order(self, approved_decision):
            self.execute_called = True
            return {"executed": True}

    class HoldStrategy(TradingStrategy):
        def get_ai_decision(self, snapshot):
            return {
                "symbol": "CASH",
                "action": "HOLD",
                "confidence": 0,
                "suggested_allocation_percent": 0,
                "reason": "No trade.",
            }

    broker = StubBroker()
    strategy = HoldStrategy(
        settings=make_settings(),
        broker=broker,
        risk_manager=RiskManager(make_settings()),
    )

    strategy.run_cycle()

    assert broker.execute_called is False


def test_scheduler_identifies_closed_market_so_openai_can_be_skipped():
    scheduler = MarketScheduler(make_settings())
    sunday = datetime(2026, 7, 5, 12, 0, tzinfo=ZoneInfo("America/New_York"))

    assert scheduler.is_market_open(sunday) is False


def test_scheduler_identifies_open_market_window():
    scheduler = MarketScheduler(make_settings())
    monday_open = datetime(2026, 7, 6, 10, 0, tzinfo=ZoneInfo("America/New_York"))

    assert scheduler.is_market_open(monday_open) is True


def test_discord_summary_formatting_includes_required_sections(tmp_path):
    notifier = DailySummaryNotifier(
        journal=TradingJournal(tmp_path),
        discord_notifier=None,
        enabled=False,
        dry_run=True,
    )

    message = notifier.format_summary(date(2026, 7, 6), notifier._mock_day_data())

    assert "Daily Trading Summary - 2026-07-06" in message
    assert "Starting Balance" in message
    assert "Trades Completed" in message
    assert "AI Decisions" in message
    assert "Rejected Trades" in message


def test_discord_send_failure_returns_false_without_raising(monkeypatch):
    class FailingRequests:
        @staticmethod
        def post(*args, **kwargs):
            raise RuntimeError("network down")

    monkeypatch.setitem(__import__("sys").modules, "requests", FailingRequests)
    notifier = DiscordNotifier("https://discord.com/api/webhooks/test")

    assert notifier.send_message("test") is False


class FakeDiscordNotifier:
    def __init__(self):
        self.messages = []

    def send_message(self, content: str) -> bool:
        self.messages.append(content)
        return True


def test_duplicate_daily_summary_is_prevented(tmp_path):
    fake_discord = FakeDiscordNotifier()
    notifier = DailySummaryNotifier(
        journal=TradingJournal(tmp_path),
        discord_notifier=fake_discord,
        enabled=True,
    )
    trading_day = date(2026, 7, 6)

    first = notifier.send_daily_summary(trading_day)
    second = notifier.send_daily_summary(trading_day)

    assert first.sent is True
    assert second.skipped is True
    assert len(fake_discord.messages) == 1
