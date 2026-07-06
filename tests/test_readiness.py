"""Pre-deployment readiness coverage for safety-critical bot behavior."""

from datetime import date, datetime
from pathlib import Path
import sys
import types
from zoneinfo import ZoneInfo

import pytest

import config
import main
from config import Settings, load_settings
from broker import BrokerClient, BrokerSnapshot
from notifications.discord_notifier import DiscordNotifier
from notifications.notifier import DailySummaryNotifier
from openai_logic import AIDecision, AIDecisionError, OpenAIDecisionClient, TradingContext
from openai_test import build_mock_trading_context
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


def test_hold_with_null_exit_percentages_validates():
    decision = AIDecision.model_validate(
        {
            "symbol": "SPY",
            "action": "HOLD",
            "confidence": 0.42,
            "suggested_allocation_percent": 0,
            "reason": "Insufficient data.",
            "stop_loss_percent": None,
            "take_profit_percent": None,
        }
    )

    assert decision.action == "HOLD"
    assert decision.stop_loss_percent is None
    assert decision.take_profit_percent is None


def test_hold_confidence_must_be_numeric():
    decision = AIDecision.model_validate(
        {
            "symbol": "SPY",
            "action": "HOLD",
            "confidence": 0.3,
            "suggested_allocation_percent": 0,
            "reason": "Insufficient data.",
            "stop_loss_percent": None,
            "take_profit_percent": None,
        }
    )

    assert decision.action == "HOLD"
    assert decision.confidence == 0.3


def test_null_confidence_is_rejected_safely():
    client = object.__new__(OpenAIDecisionClient)

    with pytest.raises(AIDecisionError):
        client._parse_decision(
            '{"symbol":"SPY","action":"HOLD","confidence":null,'
            '"suggested_allocation_percent":0,"reason":"Insufficient data.",'
            '"stop_loss_percent":null,"take_profit_percent":null}'
        )


def test_missing_reason_is_rejected_safely():
    client = object.__new__(OpenAIDecisionClient)

    with pytest.raises(AIDecisionError):
        client._parse_decision(
            '{"symbol":"SPY","action":"HOLD","confidence":0.3,'
            '"suggested_allocation_percent":0,'
            '"stop_loss_percent":null,"take_profit_percent":null}'
        )


def test_cash_symbol_is_rejected_safely():
    client = object.__new__(OpenAIDecisionClient)

    with pytest.raises(AIDecisionError):
        client._parse_decision(
            '{"symbol":"CASH","action":"HOLD","confidence":0.3,'
            '"suggested_allocation_percent":0,"reason":"Insufficient data.",'
            '"stop_loss_percent":null,"take_profit_percent":null}'
        )


def test_valid_hold_uses_watchlist_symbol_with_reason():
    decision = AIDecision.model_validate(
        {
            "symbol": "SPY",
            "action": "HOLD",
            "confidence": 0.3,
            "suggested_allocation_percent": 0,
            "reason": "Insufficient signal strength across the watchlist.",
            "stop_loss_percent": None,
            "take_profit_percent": None,
        }
    )

    assert decision.symbol == "SPY"
    assert decision.action == "HOLD"
    assert decision.reason


def test_hold_with_zero_exit_percentages_is_normalized_to_null():
    client = object.__new__(OpenAIDecisionClient)

    decision = client._parse_decision(
        '{"symbol":"SPY","action":"HOLD","confidence":0.42,'
        '"suggested_allocation_percent":0,"reason":"Insufficient data.",'
        '"stop_loss_percent":0,"take_profit_percent":0}'
    )

    assert decision.action == "HOLD"
    assert decision.stop_loss_percent is None
    assert decision.take_profit_percent is None


def test_buy_with_positive_exit_percentages_validates():
    decision = AIDecision.model_validate(
        {
            "symbol": "AAPL",
            "action": "BUY",
            "confidence": 0.81,
            "suggested_allocation_percent": 5,
            "reason": "Test setup.",
            "stop_loss_percent": 3,
            "take_profit_percent": 6,
        }
    )

    assert decision.action == "BUY"
    assert decision.stop_loss_percent == 3
    assert decision.take_profit_percent == 6


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


def test_gpt_5_family_request_omits_temperature():
    client = object.__new__(OpenAIDecisionClient)
    client.model = "gpt-5-mini"

    request = client._build_chat_completion_request("system", "user")

    assert request["model"] == "gpt-5-mini"
    assert "temperature" not in request
    assert request["response_format"] == {"type": "json_object"}


def test_gpt_4o_family_request_includes_temperature():
    client = object.__new__(OpenAIDecisionClient)
    client.model = "gpt-4o-mini"

    request = client._build_chat_completion_request("system", "user")

    assert request["model"] == "gpt-4o-mini"
    assert request["temperature"] == 0.2
    assert request["response_format"] == {"type": "json_object"}


def test_test_openai_route_exists():
    parser_args = ["main.py", "--test-openai"]

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("sys.argv", parser_args)
        args = main.parse_args()

    assert args.test_openai is True


def test_test_execution_route_exists():
    parser_args = ["main.py", "--test-execution", "--dry-run"]

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("sys.argv", parser_args)
        args = main.parse_args()

    assert args.test_execution is True
    assert args.dry_run is True


def test_mock_openai_context_generation_uses_safe_fake_data():
    context = build_mock_trading_context(
        make_settings(allowed_symbols=["AAPL", "MSFT", "SPY"])
    )

    assert context.market_status == "TEST_MODE_MARKET_OPEN"
    assert context.portfolio_value == 100000
    assert context.account_cash == 25000
    assert context.buying_power == 25000
    assert context.watchlist_symbols == ["AAPL", "MSFT", "SPY"]
    assert context.risk_rules["dry_run_enabled"] is True
    assert context.current_positions[0]["symbol"] == "MSFT"
    assert "AAPL" in context.recent_price_data


def test_test_openai_command_exits_without_broker_execution(monkeypatch):
    called = {"openai_test": False}

    fake_openai_test = types.ModuleType("openai_test")

    def fake_run(settings):
        called["openai_test"] = True
        return 0

    fake_openai_test.run_openai_integration_test = fake_run
    monkeypatch.setitem(sys.modules, "openai_test", fake_openai_test)
    monkeypatch.setitem(sys.modules, "broker", None)
    monkeypatch.setattr(sys, "argv", ["main.py", "--test-openai"])
    monkeypatch.setattr(config, "load_dotenv", lambda dotenv_path=None: False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    with pytest.raises(SystemExit) as exc:
        main.main()

    assert exc.value.code == 0
    assert called["openai_test"] is True


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


def broker_with_snapshot(settings, price=100, fake_broker=None):
    broker = BrokerClient(
        settings,
        broker_factory=lambda config: fake_broker,
        order_factory=lambda **kwargs: types.SimpleNamespace(**kwargs),
    )
    broker._last_snapshot = BrokerSnapshot(
        account={"portfolio_value": 100000, "cash": 25000, "buying_power": 25000},
        positions=[],
        market_data={"prices": {"AAPL": {"last_price": price}, "MSFT": {"last_price": price}}},
    )
    return broker


def approved_decision(**overrides):
    decision = {
        "symbol": "AAPL",
        "action": "BUY",
        "confidence": 0.9,
        "suggested_allocation_percent": 5,
        "reason": "Approved test decision.",
    }
    decision.update(overrides)
    return decision


class MockLumibotBroker:
    def __init__(self, should_raise=False):
        self.submitted_orders = []
        self.should_raise = should_raise

    def submit_order(self, order):
        self.submitted_orders.append(order)
        if self.should_raise:
            raise RuntimeError("broker failed")
        return types.SimpleNamespace(identifier="paper-123", status="submitted")


class MockAlpacaApi:
    def get_account(self):
        return types.SimpleNamespace(
            cash="25000",
            buying_power="25000",
            portfolio_value="100000",
        )

    def get_all_positions(self):
        return [
            types.SimpleNamespace(
                symbol="MSFT",
                qty="2",
                market_value="869.60",
                avg_entry_price="431.21",
            )
        ]


class MockDataBroker:
    def __init__(self):
        self.api = MockAlpacaApi()

    def get_last_price(self, asset):
        prices = {
            "AAPL": 214.33,
            "MSFT": 434.80,
            "SPY": 550.25,
        }
        return prices.get(asset.symbol)


def test_dry_run_still_allows_broker_data_collection():
    broker = BrokerClient(
        make_settings(dry_run=True, allowed_symbols=["AAPL", "MSFT", "SPY"]),
        broker_factory=lambda config: MockDataBroker(),
    )

    broker.connect()
    snapshot = broker.collect_snapshot()

    assert snapshot.account["cash"] == 25000
    assert snapshot.account["buying_power"] == 25000
    assert snapshot.account["portfolio_value"] == 100000
    assert snapshot.positions == [
        {
            "symbol": "MSFT",
            "quantity": 2,
            "market_value": 869.6,
            "average_price": 431.21,
        }
    ]
    assert snapshot.market_data["prices"]["AAPL"]["last_price"] == 214.33
    assert snapshot.market_data["prices"]["MSFT"]["last_price"] == 434.8
    assert snapshot.market_data["prices"]["SPY"]["last_price"] == 550.25


def test_alpaca_config_uses_paper_without_deprecated_endpoint():
    broker = BrokerClient(make_settings())

    alpaca_config = broker._alpaca_config()

    assert alpaca_config["PAPER"] is True
    assert "ENDPOINT" not in alpaca_config
    assert alpaca_config["API_KEY"] == "test-alpaca-key"
    assert alpaca_config["API_SECRET"] == "test-alpaca-secret"


def test_strategy_context_includes_populated_broker_data():
    snapshot = BrokerSnapshot(
        account={"cash": 25000, "buying_power": 25000, "portfolio_value": 100000},
        positions=[{"symbol": "MSFT", "quantity": 2}],
        market_data={"prices": {"AAPL": {"last_price": 214.33}}},
    )
    strategy = TradingStrategy(
        settings=make_settings(allowed_symbols=["AAPL", "MSFT"]),
        broker=None,
        risk_manager=RiskManager(make_settings()),
    )

    context = strategy._build_ai_context(snapshot)

    assert context.account_cash == 25000
    assert context.buying_power == 25000
    assert context.portfolio_value == 100000
    assert context.current_positions == [{"symbol": "MSFT", "quantity": 2}]
    assert context.recent_price_data == {"prices": {"AAPL": {"last_price": 214.33}}}


def test_broker_dry_run_blocks_execution():
    fake_broker = MockLumibotBroker()
    broker = broker_with_snapshot(make_settings(dry_run=True), fake_broker=fake_broker)

    result = broker.execute_order(approved_decision())

    assert result["executed"] is False
    assert "DRY_RUN" in result["reason"]
    assert fake_broker.submitted_orders == []


def test_broker_paper_trading_false_blocks_execution():
    fake_broker = MockLumibotBroker()
    broker = broker_with_snapshot(make_settings(paper_trading=False), fake_broker=fake_broker)

    result = broker.execute_order(approved_decision())

    assert result["executed"] is False
    assert "PAPER_TRADING" in result["reason"]
    assert fake_broker.submitted_orders == []


def test_broker_bot_disabled_blocks_execution():
    fake_broker = MockLumibotBroker()
    broker = broker_with_snapshot(make_settings(bot_enabled=False), fake_broker=fake_broker)

    result = broker.execute_order(approved_decision())

    assert result["executed"] is False
    assert "BOT_ENABLED" in result["reason"]
    assert fake_broker.submitted_orders == []


def test_broker_connect_failure_is_caught_safely():
    def failing_broker_factory(config):
        raise RuntimeError("bad credentials")

    broker = BrokerClient(make_settings(), broker_factory=failing_broker_factory)

    broker.connect()

    assert broker._broker_available is False
    assert broker._broker is None


def test_broker_unavailable_blocks_execution_after_failed_connect():
    def failing_broker_factory(config):
        raise RuntimeError("bad credentials")

    broker = broker_with_snapshot(make_settings(), fake_broker=None)
    broker._broker_factory = failing_broker_factory
    broker.connect()

    result = broker.execute_order(approved_decision())

    assert result["executed"] is False
    assert result["reason"] == "Broker unavailable"


def test_lazy_broker_initialisation_failure_returns_broker_unavailable():
    def failing_broker_factory(config):
        raise RuntimeError("network down")

    broker = broker_with_snapshot(make_settings(), fake_broker=None)
    broker._broker_factory = failing_broker_factory

    result = broker.execute_order(approved_decision())

    assert result["executed"] is False
    assert result["reason"] == "Broker unavailable"


def test_broker_hold_does_not_call_lumibot():
    fake_broker = MockLumibotBroker()
    broker = broker_with_snapshot(make_settings(), fake_broker=fake_broker)

    result = broker.execute_order(approved_decision(action="HOLD", suggested_allocation_percent=0))

    assert result["executed"] is False
    assert result["reason"] == "HOLD decision. No order placed."
    assert fake_broker.submitted_orders == []


def test_broker_unsupported_symbol_does_not_call_lumibot():
    fake_broker = MockLumibotBroker()
    broker = broker_with_snapshot(make_settings(allowed_symbols=["AAPL"]), fake_broker=fake_broker)

    result = broker.execute_order(approved_decision(symbol="TSLA"))

    assert result["executed"] is False
    assert "ALLOWED_SYMBOLS" in result["reason"]
    assert fake_broker.submitted_orders == []


def test_broker_allocation_above_max_does_not_call_lumibot():
    fake_broker = MockLumibotBroker()
    broker = broker_with_snapshot(make_settings(max_position_allocation_percent=5), fake_broker=fake_broker)

    result = broker.execute_order(approved_decision(suggested_allocation_percent=10))

    assert result["executed"] is False
    assert "exceeds maximum" in result["reason"]
    assert fake_broker.submitted_orders == []


def test_broker_quantity_calculation_works():
    broker = broker_with_snapshot(make_settings(), price=100)

    quantity = broker._calculate_quantity(approved_decision(suggested_allocation_percent=5), 100)

    assert quantity == 50


def test_broker_zero_quantity_is_rejected():
    fake_broker = MockLumibotBroker()
    broker = broker_with_snapshot(make_settings(), price=1000000, fake_broker=fake_broker)

    result = broker.execute_order(approved_decision(suggested_allocation_percent=1))

    assert result["executed"] is False
    assert "quantity is 0" in result["reason"]
    assert fake_broker.submitted_orders == []


def test_broker_missing_price_is_rejected():
    fake_broker = MockLumibotBroker()
    broker = broker_with_snapshot(make_settings(), fake_broker=fake_broker)
    broker._last_snapshot = BrokerSnapshot(
        account={"portfolio_value": 100000},
        positions=[],
        market_data={"prices": {}},
    )

    result = broker.execute_order(approved_decision())

    assert result["executed"] is False
    assert "price" in result["reason"]
    assert fake_broker.submitted_orders == []


def test_broker_buy_calls_mocked_lumibot_execution():
    fake_broker = MockLumibotBroker()
    broker = broker_with_snapshot(make_settings(), price=100, fake_broker=fake_broker)

    result = broker.execute_order(approved_decision(action="BUY"))

    assert result["executed"] is True
    assert result["broker_order_id"] == "paper-123"
    assert result["quantity"] == 50
    assert fake_broker.submitted_orders[0].action == "BUY"


def test_broker_sell_calls_mocked_lumibot_execution():
    fake_broker = MockLumibotBroker()
    broker = broker_with_snapshot(make_settings(), price=100, fake_broker=fake_broker)

    result = broker.execute_order(approved_decision(action="SELL"))

    assert result["executed"] is True
    assert result["broker_order_id"] == "paper-123"
    assert fake_broker.submitted_orders[0].action == "SELL"


def test_broker_lumibot_exception_is_caught_safely():
    fake_broker = MockLumibotBroker(should_raise=True)
    broker = broker_with_snapshot(make_settings(), price=100, fake_broker=fake_broker)

    result = broker.execute_order(approved_decision())

    assert result["executed"] is False
    assert result["reason"] == "Broker order submission failed."
    assert result["raw_status"] == "RuntimeError"


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
