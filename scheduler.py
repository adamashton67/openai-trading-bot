"""US market-hours helpers and sleep cadence logic."""

from datetime import datetime, time
import logging
import time as time_module
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

    def sleep_while_market_closed(self) -> None:
        """Sleep while avoiding unnecessary OpenAI calls outside market hours."""
        seconds = self.seconds_until_next_market_check()
        logger.info("Market closed. Sleeping for %s seconds before rechecking.", seconds)
        time_module.sleep(seconds)
