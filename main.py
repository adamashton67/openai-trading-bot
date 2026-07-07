"""Long-running entry point for the OpenAI-driven trading bot."""

import argparse
import logging
from dataclasses import replace

from config import load_settings, missing_required_values
from database import init_database
from logger_config import configure_logging
from notifications.discord_notifier import DiscordNotifier
from notifications.notifier import DailySummaryNotifier
from storage import TradingJournal


logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    """Parse local utility flags for summary testing."""
    parser = argparse.ArgumentParser(description="OpenAI Trading Bot")
    parser.add_argument(
        "--send-test-summary",
        action="store_true",
        help="Send a mock Discord daily summary immediately.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the summary instead of sending it to Discord.",
    )
    parser.add_argument(
        "--test-openai",
        action="store_true",
        help="Call OpenAI with mock trading context, validate the response, and exit.",
    )
    parser.add_argument(
        "--test-execution",
        action="store_true",
        help="Run a safe dry-run execution-path test and exit.",
    )
    parser.add_argument(
        "--single-cycle",
        action="store_true",
        help="Run exactly one trading cycle and exit. Respects configured safety gates.",
    )
    parser.add_argument(
        "--test-scanner",
        action="store_true",
        help="Run only the dynamic watchlist scanner and exit.",
    )
    parser.add_argument(
        "--scanner-max-symbols",
        type=int,
        default=None,
        help="Temporarily cap broad scanner candidates for fast scanner testing.",
    )
    return parser.parse_args()


def main() -> None:
    """Start the trading bot process and keep it running until stopped."""
    args = parse_args()
    configure_logging()
    settings = load_settings()

    logger.info("Starting OpenAI Trading Bot.")
    logger.info("Paper trading mode: %s", settings.paper_trading)
    logger.info("Dry-run mode: %s", settings.dry_run)
    init_database()

    journal = TradingJournal(settings.data_dir)
    discord_notifier = (
        DiscordNotifier(settings.discord_webhook_url)
        if settings.discord_webhook_url
        else None
    )
    summary_notifier = DailySummaryNotifier(
        journal=journal,
        discord_notifier=discord_notifier,
        enabled=settings.discord_daily_summary_enabled,
        dry_run=args.dry_run,
        paper_trading=settings.paper_trading,
        bot_dry_run=settings.dry_run,
        bot_version=settings.bot_version,
    )
    logger.info(
        "Discord daily summary enabled: %s",
        settings.discord_daily_summary_enabled,
    )

    if args.send_test_summary:
        summary_notifier.send_test_summary()
        return

    if args.test_openai:
        from openai_test import run_openai_integration_test

        raise SystemExit(run_openai_integration_test(settings))

    if args.test_execution:
        from execution_test import run_execution_test

        raise SystemExit(run_execution_test(settings))

    if args.scanner_max_symbols is not None:
        scanner_cap = max(1, args.scanner_max_symbols)
        settings = replace(
            settings,
            broad_market_max_symbols=min(settings.broad_market_max_symbols, scanner_cap),
            max_scanner_candidates_after_filters=min(
                settings.max_scanner_candidates_after_filters,
                scanner_cap,
            ),
        )
    if args.test_scanner and not settings.dynamic_watchlist_enabled:
        settings = replace(settings, dynamic_watchlist_enabled=True)

    if not settings.bot_enabled:
        logger.warning("BOT_ENABLED is false. Bot will monitor market hours but skip trading.")

    missing_values = missing_required_values(settings)
    if missing_values:
        logger.warning("Missing environment values: %s", ", ".join(missing_values))

    from broker import BrokerClient

    broker = BrokerClient(settings)
    broker.connect()

    if args.test_scanner:
        from scanner_test import run_scanner_test

        raise SystemExit(run_scanner_test(settings, broker))

    from risk_manager import RiskManager
    from scheduler import MarketScheduler
    from strategy import TradingStrategy

    scheduler = MarketScheduler(settings)
    risk_manager = RiskManager(settings)
    strategy = TradingStrategy(
        settings=settings,
        broker=broker,
        risk_manager=risk_manager,
        journal=journal,
    )

    if args.single_cycle:
        logger.info("Running one trading cycle via --single-cycle.")
        strategy.run_cycle()
        logger.info("Single trading cycle complete. Exiting.")
        return

    while True:
        try:
            if not scheduler.is_market_open():
                logger.info("US market is closed. Skipping trading cycle and OpenAI call.")
                if scheduler.is_after_market_close():
                    summary_notifier.send_daily_summary(
                        trading_day=scheduler.now().date(),
                        current_snapshot=broker.collect_snapshot(),
                    )
                scheduler.sleep_while_market_closed()
                continue

            if not settings.bot_enabled:
                logger.info("BOT_ENABLED is false. Skipping trading cycle.")
                scheduler.sleep_between_cycles()
                continue

            strategy.run_cycle()
            scheduler.sleep_between_cycles()
        except KeyboardInterrupt:
            logger.info("Shutdown requested. Stopping bot.")
            break
        except Exception:
            logger.exception("Unhandled error during bot loop.")
            scheduler.sleep_between_cycles()


if __name__ == "__main__":
    main()
