"""Logging setup for console and file output."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


LOG_DIR = Path("logs")
LOG_FILE = LOG_DIR / "bot.log"
_UNSUPPORTED_LOG_KWARGS = {"color"}
_LOGGER_PATCHED = False


def install_logging_compatibility_shim() -> None:
    """Allow third-party log calls with presentation-only kwargs."""
    global _LOGGER_PATCHED
    if _LOGGER_PATCHED:
        return

    original_log = logging.Logger._log

    def compatible_log(
        self: logging.Logger,
        level: int,
        msg: object,
        args: tuple[Any, ...],
        exc_info: Any = None,
        extra: Any = None,
        stack_info: bool = False,
        stacklevel: int = 1,
        **kwargs: Any,
    ) -> None:
        for key in _UNSUPPORTED_LOG_KWARGS:
            kwargs.pop(key, None)
        return original_log(
            self,
            level,
            msg,
            args,
            exc_info=exc_info,
            extra=extra,
            stack_info=stack_info,
            stacklevel=stacklevel,
            **kwargs,
        )

    logging.Logger._log = compatible_log
    _LOGGER_PATCHED = True


def configure_logging() -> None:
    """Configure application logging once at startup."""
    install_logging_compatibility_shim()
    LOG_DIR.mkdir(exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=2_000_000,
        backupCount=5,
    )
    file_handler.setFormatter(formatter)

    logging.basicConfig(
        level=logging.INFO,
        handlers=[console_handler, file_handler],
        force=True,
    )
