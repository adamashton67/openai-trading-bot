"""Application configuration loaded from environment variables."""

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
import os

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(dotenv_path: str | Path | None = None) -> bool:
        """No-op fallback when python-dotenv is not installed yet."""
        return False


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SCANNER_UNIVERSE = (
    "SPY,QQQ,AAPL,MSFT,NVDA,AMZN,GOOGL,META,TSLA,AMD,NFLX,AVGO,"
    "INTC,ORCL,CRM,ADBE,PYPL,UBER,SHOP,PLTR"
)


def _parse_bool(value: str | None, default: bool = False) -> bool:
    """Convert common environment variable strings into booleans."""
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_symbols(value: str | None) -> list[str]:
    """Return an uppercase symbol allowlist from a comma-separated string."""
    if not value:
        return []
    return [symbol.strip().upper() for symbol in value.split(",") if symbol.strip()]


@dataclass(frozen=True)
class Settings:
    """Runtime settings for the trading bot."""

    bot_enabled: bool
    paper_trading: bool
    dry_run: bool
    trading_interval_minutes: int
    market_timezone: str
    openai_api_key: str
    openai_model: str
    alpaca_api_key: str
    alpaca_secret_key: str
    alpaca_paper_base_url: str
    max_position_allocation_percent: float
    min_confidence: float
    allowed_symbols: list[str]
    dynamic_watchlist_enabled: bool
    broad_market_scan_enabled: bool
    broad_market_max_symbols: int
    max_scanner_candidates_after_filters: int
    broad_scan_batch_size: int
    broad_scan_max_requests_per_cycle: int
    min_stock_price: float
    min_average_volume: float
    exclude_etfs: bool
    watchlist_size: int
    scanner_universe: list[str]
    decision_history_limit: int
    execution_history_limit: int
    portfolio_history_limit: int
    include_history_context: bool
    prompts_dir: Path
    data_dir: Path
    discord_webhook_url: str
    discord_daily_summary_enabled: bool

    @property
    def trading_interval_seconds(self) -> int:
        """Return the polling interval in seconds."""
        return self.trading_interval_minutes * 60


def load_settings(env_file: str | Path | None = None) -> Settings:
    """Load settings from `.env` locally and environment variables in Railway."""
    load_dotenv(dotenv_path=env_file)

    return Settings(
        bot_enabled=_parse_bool(os.getenv("BOT_ENABLED"), default=False),
        paper_trading=_parse_bool(os.getenv("PAPER_TRADING"), default=True),
        dry_run=_parse_bool(os.getenv("DRY_RUN"), default=True),
        trading_interval_minutes=int(os.getenv("TRADING_INTERVAL_MINUTES", "15")),
        market_timezone=os.getenv("MARKET_TIMEZONE", "America/New_York"),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-5-mini"),
        alpaca_api_key=os.getenv("ALPACA_API_KEY", ""),
        alpaca_secret_key=os.getenv("ALPACA_SECRET_KEY", ""),
        alpaca_paper_base_url=os.getenv(
            "ALPACA_PAPER_BASE_URL",
            "https://paper-api.alpaca.markets",
        ),
        max_position_allocation_percent=float(
            os.getenv("MAX_POSITION_ALLOCATION_PERCENT", "5")
        ),
        min_confidence=float(os.getenv("MIN_CONFIDENCE", "0.70")),
        allowed_symbols=_parse_symbols(os.getenv("ALLOWED_SYMBOLS")),
        dynamic_watchlist_enabled=_parse_bool(
            os.getenv("DYNAMIC_WATCHLIST_ENABLED"),
            default=False,
        ),
        broad_market_scan_enabled=_parse_bool(
            os.getenv("BROAD_MARKET_SCAN_ENABLED"),
            default=False,
        ),
        broad_market_max_symbols=int(os.getenv("BROAD_MARKET_MAX_SYMBOLS", "1000")),
        max_scanner_candidates_after_filters=int(
            os.getenv("MAX_SCANNER_CANDIDATES_AFTER_FILTERS", "1000")
        ),
        broad_scan_batch_size=int(os.getenv("BROAD_SCAN_BATCH_SIZE", "100")),
        broad_scan_max_requests_per_cycle=int(
            os.getenv("BROAD_SCAN_MAX_REQUESTS_PER_CYCLE", "20")
        ),
        min_stock_price=float(os.getenv("MIN_STOCK_PRICE", "5")),
        min_average_volume=float(os.getenv("MIN_AVERAGE_VOLUME", "500000")),
        exclude_etfs=_parse_bool(os.getenv("EXCLUDE_ETFS"), default=True),
        watchlist_size=int(os.getenv("WATCHLIST_SIZE", "20")),
        scanner_universe=_parse_symbols(
            os.getenv("SCANNER_UNIVERSE", DEFAULT_SCANNER_UNIVERSE)
        ),
        decision_history_limit=int(os.getenv("DECISION_HISTORY_LIMIT", "20")),
        execution_history_limit=int(os.getenv("EXECUTION_HISTORY_LIMIT", "20")),
        portfolio_history_limit=int(os.getenv("PORTFOLIO_HISTORY_LIMIT", "20")),
        include_history_context=_parse_bool(
            os.getenv("INCLUDE_HISTORY_CONTEXT"),
            default=True,
        ),
        prompts_dir=BASE_DIR / "prompts",
        data_dir=BASE_DIR / "data",
        discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL", ""),
        discord_daily_summary_enabled=_parse_bool(
            os.getenv("DISCORD_DAILY_SUMMARY_ENABLED"),
            default=False,
        ),
    )


def missing_required_values(settings: Settings, names: Iterable[str] | None = None) -> list[str]:
    """Return missing environment variable names needed before live operation."""
    required = {
        "OPENAI_API_KEY": settings.openai_api_key,
        "ALPACA_API_KEY": settings.alpaca_api_key,
        "ALPACA_SECRET_KEY": settings.alpaca_secret_key,
    }
    if names is not None:
        required = {name: required[name] for name in names}
    return [name for name, value in required.items() if not value]
