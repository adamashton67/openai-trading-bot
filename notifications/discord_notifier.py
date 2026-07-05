"""Discord webhook notification client."""

from __future__ import annotations

import logging


logger = logging.getLogger(__name__)


class DiscordNotifier:
    """Sends messages to Discord through an incoming webhook."""

    def __init__(self, webhook_url: str, timeout_seconds: int = 10) -> None:
        self.webhook_url = webhook_url
        self.timeout_seconds = timeout_seconds

    def send_message(self, content: str) -> bool:
        """Send a Discord message without exposing the webhook URL in logs."""
        if not self.webhook_url:
            logger.warning("Discord send skipped because DISCORD_WEBHOOK_URL is missing.")
            return False

        try:
            import requests

            response = requests.post(
                self.webhook_url,
                json={"content": content},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except ImportError:
            logger.warning(
                "Discord send failure: requests is not installed. "
                "Install dependencies from requirements.txt."
            )
            return False
        except Exception as exc:
            logger.warning("Discord send failure: %s", exc)
            return False

        logger.info("Discord summary sent successfully.")
        return True
