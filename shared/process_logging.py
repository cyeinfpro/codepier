"""Small process-log policy; audit records remain separately authorized."""
from __future__ import annotations

import logging
import os

from shared.config import ConfigurationError

LOG_LEVELS = frozenset({"debug", "info", "warning", "error", "critical"})


def process_log_level(value: str | None = None) -> str:
    level = (os.getenv("CODEPIER_LOG_LEVEL", "info") if value is None else value).strip().lower()
    if level not in LOG_LEVELS:
        raise ConfigurationError("CODEPIER_LOG_LEVEL must be debug, info, warning, error or critical")
    return level


def configure_logging(level: str | None = None) -> None:
    """Configure our namespace once, without changing dependency/root verbosity."""
    value = process_log_level(level)
    logger = logging.getLogger("codepier")
    if not any(handler.get_name() == "codepier-process" for handler in logger.handlers):
        handler = logging.StreamHandler()
        handler.set_name("codepier-process")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(value.upper())
    logger.propagate = False
