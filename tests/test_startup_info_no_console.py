import logging
import sys

from sshpilot import startup_info


def test_verbose_startup_info_without_console_logs_summary(monkeypatch, caplog):
    # A windowed Windows build runs with sys.stdout set to None.
    monkeypatch.setattr(sys, "stdout", None)

    with caplog.at_level(logging.INFO, logger=startup_info.logger.name):
        startup_info.print_startup_info(verbose=True)

    assert any("starting" in record.getMessage() for record in caplog.records)
