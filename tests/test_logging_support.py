from __future__ import annotations

import logging
import time

from sshpilot.logging_support import (
    close_managed_handlers,
    configure_frontend_logging,
    ensure_daemon_log_forwarder,
    log_context,
    sanitize_log_text,
    stop_daemon_log_forwarder,
)


def test_frontend_configuration_is_idempotent_and_closes_replaced_files(tmp_path):
    root = logging.getLogger()
    close_managed_handlers(root)
    try:
        first = configure_frontend_logging(tmp_path, "debug")
        old_streams = [
            handler.stream
            for handler in first
            if getattr(handler, "_sshpilot_log_role", "").startswith("frontend.")
            and getattr(handler, "_sshpilot_log_role", "") != "frontend.console"
        ]
        second = configure_frontend_logging(tmp_path, "info")
        managed = [
            handler
            for handler in root.handlers
            if getattr(handler, "_sshpilot_managed", False)
        ]
        assert len(managed) == 4
        assert {getattr(handler, "_sshpilot_log_role", "") for handler in managed} == {
            "frontend.master",
            "frontend.app",
            "frontend.ssh",
            "frontend.console",
        }
        assert all(stream.closed for stream in old_streams if stream is not None)
        assert all(handler.level == logging.INFO for handler in second)
    finally:
        close_managed_handlers(root)


def test_categories_formatter_and_redaction_are_shared(tmp_path):
    root = logging.getLogger()
    close_managed_handlers(root)
    try:
        configure_frontend_logging(tmp_path, "debug")
        logging.getLogger("sshpilot.connection_manager").info("ssh record")
        logging.getLogger("sshpilot.window").info(
            "Authorization: Bearer TOKEN_SENTINEL_49293 password=PASSWORD_SENTINEL_49291"
        )
        logging.getLogger("daemon.forwarded.sshpilot.daemon.session_runtime").error(
            "daemon record"
        )
        for handler in root.handlers:
            handler.flush()
        assert "ssh record" in (tmp_path / "ssh.log").read_text()
        assert "ssh record" not in (tmp_path / "app.log").read_text()
        assert "daemon record" in (tmp_path / "sshpilot.log").read_text()
        assert "daemon record" not in (tmp_path / "app.log").read_text()
        master = (tmp_path / "sshpilot.log").read_text()
        assert "TOKEN_SENTINEL_49293" not in master
        assert "PASSWORD_SENTINEL_49291" not in master
        assert " - INFO - " in master
        assert "Authorization: Bearer ***REDACTED***" in master
    finally:
        close_managed_handlers(root)


def test_context_is_added_to_records_without_logging_payloads(tmp_path):
    root = logging.getLogger()
    close_managed_handlers(root)
    try:
        configure_frontend_logging(tmp_path, "debug")
        with log_context(request="request-17", client="client-4", method="sftp.list"):
            logging.getLogger("sshpilot.api.daemon_client").debug(
                "request lifecycle started"
            )
        for handler in root.handlers:
            handler.flush()
        content = (tmp_path / "sshpilot.log").read_text()
        assert "request lifecycle started" in content
        assert "[request=request-17 client=client-4 method=sftp.list]" in content
        assert "params=" not in content
    finally:
        close_managed_handlers(root)


def test_daemon_forwarder_starts_at_end_and_handles_rotation(tmp_path):
    path = tmp_path / "daemon.log"
    path.write_text("2026-08-09 12:00:00 - old - INFO - old\n")
    forwarder = ensure_daemon_log_forwarder(path, enabled=True)
    assert forwarder is not None
    logger = logging.getLogger("daemon.forwarded.sshpilot.test")
    logger.setLevel(logging.DEBUG)
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logger.addHandler(handler)
    try:
        with path.open("a") as stream:
            stream.write(
                "2026-08-09 12:00:01 - sshpilot.test - ERROR - new\n"
            )
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not records:
            time.sleep(0.02)
        assert [item.getMessage() for item in records] == ["new"]
        rotated = tmp_path / "daemon.log.1"
        path.rename(rotated)
        path.write_text(
            "2026-08-09 12:00:02 - sshpilot.test - WARNING - rotated\n"
        )
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and len(records) < 2:
            time.sleep(0.02)
        assert [item.getMessage() for item in records] == ["new", "rotated"]
    finally:
        logger.removeHandler(handler)
        stop_daemon_log_forwarder()


def test_sanitizer_targets_credentials_without_removing_hosts():
    text = (
        "host=example.test user=alice password=PASSWORD_SENTINEL_49291 "
        "Authorization: Bearer TOKEN_SENTINEL_49293 "
        "-----BEGIN OPENSSH PRIVATE KEY-----PRIVATE_KEY_SENTINEL_49295"
        "-----END OPENSSH PRIVATE KEY-----"
    )
    safe = sanitize_log_text(text)
    assert "example.test" in safe
    assert "alice" in safe
    assert "PASSWORD_SENTINEL_49291" not in safe
    assert "TOKEN_SENTINEL_49293" not in safe
    assert "PRIVATE_KEY_SENTINEL_49295" not in safe
