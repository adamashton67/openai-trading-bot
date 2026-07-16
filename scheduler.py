"""US market-hours helpers and sleep cadence logic."""

from datetime import datetime, time
import fcntl
import logging
from pathlib import Path
import threading
import time as time_module
from typing import Any
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

from config import Settings


logger = logging.getLogger(__name__)


class MarketScheduler:
    """Controls when the bot may run trading cycles."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.market_calendar = mcal.get_calendar("NYSE")
        self.market_tz = ZoneInfo(settings.market_timezone)
        self._next_trading_cycle = 0.0
        self._next_position_management_cycle = 0.0

    def now(self) -> datetime:
        """Return the current time in the configured market timezone."""
        return datetime.now(self.market_tz)

    def is_market_open(self, current_time: datetime | None = None) -> bool:
        """Check whether the NYSE regular session is open right now."""
        current_time = current_time or self.now()
        market_hours = self.market_hours(current_time)
        if market_hours is None:
            return False

        market_open, market_close = market_hours
        return market_open <= current_time <= market_close

    def market_hours(
        self,
        current_time: datetime | None = None,
    ) -> tuple[datetime, datetime] | None:
        """Return regular market open and close for the current date."""
        current_time = current_time or self.now()
        schedule = self.market_calendar.schedule(
            start_date=current_time.date(),
            end_date=current_time.date(),
        )
        if schedule.empty:
            return None

        market_open = schedule.iloc[0]["market_open"].tz_convert(self.market_tz)
        market_close = schedule.iloc[0]["market_close"].tz_convert(self.market_tz)
        return market_open, market_close

    def is_after_market_close(self, current_time: datetime | None = None) -> bool:
        """Return whether regular US trading has closed for the day."""
        current_time = current_time or self.now()
        market_hours = self.market_hours(current_time)
        if market_hours is None:
            return False

        _, market_close = market_hours
        return current_time > market_close

    def seconds_until_next_market_check(self) -> int:
        """Return a conservative sleep interval while the market is closed."""
        now = self.now()

        if time(4, 0) <= now.time() <= time(9, 30):
            return 5 * 60
        return 30 * 60

    def sleep_between_cycles(self) -> None:
        """Sleep for the configured trading interval."""
        logger.info("Sleeping for %s seconds before next cycle.", self.settings.trading_interval_seconds)
        time_module.sleep(self.settings.trading_interval_seconds)

    def trading_cycle_due(self, monotonic_time: float | None = None) -> bool:
        """Return whether the existing OpenAI cycle is due."""
        return (monotonic_time if monotonic_time is not None else time_module.monotonic()) >= self._next_trading_cycle

    def position_management_due(self, monotonic_time: float | None = None) -> bool:
        """Return whether the independent deterministic cycle is due."""
        if not self.settings.position_management_enabled:
            return False
        return (monotonic_time if monotonic_time is not None else time_module.monotonic()) >= self._next_position_management_cycle

    def mark_trading_cycle_run(self, monotonic_time: float | None = None) -> None:
        """Schedule the next OpenAI cycle from a monotonic clock."""
        current = monotonic_time if monotonic_time is not None else time_module.monotonic()
        self._next_trading_cycle = current + self.settings.trading_interval_seconds

    def mark_position_management_run(self, monotonic_time: float | None = None) -> None:
        """Schedule the next position-only cycle from a monotonic clock."""
        current = monotonic_time if monotonic_time is not None else time_module.monotonic()
        self._next_position_management_cycle = current + self.settings.position_management_interval_seconds

    def sleep_until_next_cycle(self) -> None:
        """Sleep until the earliest enabled cycle is due."""
        current = time_module.monotonic()
        deadlines = [self._next_trading_cycle]
        if self.settings.position_management_enabled:
            deadlines.append(self._next_position_management_cycle)
        seconds = max(0.1, min(deadlines) - current)
        logger.info("Sleeping for %.1f seconds before next scheduled cycle.", seconds)
        time_module.sleep(seconds)

    def sleep_while_market_closed(self) -> None:
        """Sleep while avoiding unnecessary OpenAI calls outside market hours."""
        seconds = self.seconds_until_next_market_check()
        logger.info("Market closed. Sleeping for %s seconds before rechecking.", seconds)
        time_module.sleep(seconds)


class CycleLock:
    """Non-blocking in-memory and process-wide file lock for trading cycles."""

    _thread_lock = threading.Lock()

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._file = None
        self.acquired = False

    def __enter__(self) -> "CycleLock":
        if not self._thread_lock.acquire(blocking=False):
            return self
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self.path.open("a+")
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.acquired = True
        except (OSError, BlockingIOError):
            if self._file is not None:
                self._file.close()
                self._file = None
            self._thread_lock.release()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if not self.acquired:
            return
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
        finally:
            self.acquired = False
            self._thread_lock.release()
