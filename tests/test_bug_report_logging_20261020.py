"""Process logging controls and safe Agent failure diagnostics."""
import asyncio
import logging
from types import SimpleNamespace

import pytest

from hub.config import HubConfig
from shared.config import ConfigurationError


@pytest.mark.parametrize("value,expected", [("debug", "debug"), ("WARNING", "warning"), (" error ", "error")])
def test_hub_accepts_one_validated_process_log_level(monkeypatch, value, expected):
    monkeypatch.setenv("CODEPIER_LOG_LEVEL", value)
    assert getattr(HubConfig.from_env(), "log_level", None) == expected


def test_invalid_log_level_does_not_echo_the_setting_value(monkeypatch):
    monkeypatch.setenv("CODEPIER_LOG_LEVEL", "private-invalid-level")
    with pytest.raises(ConfigurationError, match="CODEPIER_LOG_LEVEL") as error:
        HubConfig.from_env()
    assert "private-invalid-level" not in str(error.value)


@pytest.mark.asyncio
async def test_config_failure_logs_type_without_untrusted_exception_body(monkeypatch, caplog, capsys):
    from agent import runner

    stopped = asyncio.Event()
    def invalid_config():
        stopped.set()
        raise ValueError("private-fixture-secret")

    async def no_delay(_):
        pass

    monkeypatch.setattr(runner.asyncio, "sleep", no_delay)
    obj = SimpleNamespace(stop_event=stopped, config_path=SimpleNamespace(stat=invalid_config))
    with caplog.at_level(logging.WARNING, logger="codepier.agent"):
        await runner.Agent.watch_config(obj)
    captured = capsys.readouterr()
    assert "private-fixture-secret" not in captured.out + captured.err + caplog.text
    assert "ValueError" in caplog.text


def test_configure_logging_is_idempotent_and_does_not_enable_dependency_debug(monkeypatch, capsys):
    from shared.process_logging import configure_logging

    logger = logging.getLogger("codepier")
    prior = logger.handlers[:], logger.level, logger.propagate
    root_level = logging.getLogger().level
    try:
        monkeypatch.setenv("CODEPIER_LOG_LEVEL", "warning")
        configure_logging()
        configure_logging()
        logging.getLogger("codepier.agent").info("filtered-status")
        logging.getLogger("codepier.agent").warning("visible-status")
        captured = capsys.readouterr()
        assert "filtered-status" not in captured.err
        assert captured.err.count("visible-status") == 1
        assert logging.getLogger().level == root_level
    finally:
        for handler in logger.handlers:
            if handler not in prior[0]:
                handler.close()
        logger.handlers, logger.level, logger.propagate = prior
