"""Safe connection log metadata never includes peer or exception text."""

import json
import logging

import pytest
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
from websockets.frames import Close, CloseCode

from agent.connection_diagnostics import safe_connection_error


def closed(received=None, sent=None, *, ok=False):
    kind = ConnectionClosedOK if ok else ConnectionClosedError
    order = True if received is not None and sent is not None else None
    return kind(received, sent, order)


def test_keepalive_timeout_preserves_actual_frame_codes():
    exc = closed(sent=Close(CloseCode.INTERNAL_ERROR, "keepalive ping timeout"))
    assert safe_connection_error(exc) == {
        "exception_type": "ConnectionClosedError",
        "received_close_code": None,
        "sent_close_code": 1011,
        "reason_category": "keepalive_timeout",
    }


def test_no_close_frame_does_not_invent_synthetic_received_code():
    result = safe_connection_error(closed())
    assert result["received_close_code"] is None
    assert result["sent_close_code"] is None
    assert result["reason_category"] == "no_close_frame"


def test_regular_close_codes_survive_without_disclosing_reason(caplog):
    secret = "wss://example.invalid/socket?ticket=PRIVATE-TICKET-4931"
    exc = closed(Close(4001, secret), Close(1000, "password=HIDDEN-PASSWORD"), ok=True)
    with caplog.at_level(logging.WARNING, logger="agent.connection.test"):
        logging.getLogger("agent.connection.test").warning(
            "Connection lost; safe metadata: %s", safe_connection_error(exc)
        )
    assert "4001" in caplog.text and "1000" in caplog.text
    assert "ConnectionClosedOK" in caplog.text
    assert "reason_category" in caplog.text and "closed" in caplog.text
    for forbidden in (secret, "PRIVATE-TICKET", "password", "HIDDEN-PASSWORD", "example.invalid"):
        assert forbidden not in caplog.text
    assert exc.rcvd.reason == secret


@pytest.mark.parametrize(
    "reason",
    [
        "timeout?token=PRIVATE-TOKEN",
        "keepalive ping timeout; password=PRIVATE-PASSWORD",
        "\x1b]52;c;PRIVATE-CLIPBOARD\x07",
        "PRIVATE-LARGE-REASON" * 10000,
    ],
)
def test_unrecognized_peer_reasons_are_not_logged_or_parsed_as_known(reason):
    result = safe_connection_error(closed(Close(1011, reason)))
    assert result["reason_category"] == "closed"
    encoded = json.dumps(result)
    assert "PRIVATE" not in encoded and len(encoded) < 256


@pytest.mark.parametrize("reason", ["timeout", "Idle Timeout", " opening handshake timeout "])
def test_exact_timeout_reason_categories(reason):
    result = safe_connection_error(closed(Close(1001, reason)))
    assert result["reason_category"] == "timeout"


def test_keepalive_category_takes_precedence_over_generic_timeout():
    result = safe_connection_error(closed(Close(1001, "timeout"), Close(1011, "ping timeout")))
    assert result["reason_category"] == "keepalive_timeout"


@pytest.mark.parametrize("code", [True, 999, 5000, "1011 PRIVATE-CODE", None])
def test_malformed_frame_codes_cannot_be_coerced_into_log_data(code):
    result = safe_connection_error(closed(Close(code, "PRIVATE-REASON")))
    assert result["received_close_code"] is None
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize("exception", [TimeoutError, OSError, ValueError])
def test_other_exceptions_never_serialize_message_or_cause(exception):
    exc = exception("https://example.invalid/?secret=PRIVATE-ERROR")
    exc.__cause__ = RuntimeError("PRIVATE-CAUSE")
    result = safe_connection_error(exc)
    assert result["exception_type"] == exception.__name__
    assert result["reason_category"] == ("timeout" if exception is TimeoutError else "other")
    assert result["received_close_code"] is None and result["sent_close_code"] is None
    assert "PRIVATE" not in json.dumps(result)


def test_helper_does_not_call_exception_str():
    class UnprintableTimeout(TimeoutError):
        def __str__(self):
            raise AssertionError("Exception text must not be formatted")

    result = safe_connection_error(UnprintableTimeout("PRIVATE-ERROR"))
    assert result["exception_type"] == "UnprintableTimeout"
    assert result["reason_category"] == "timeout"


def test_exception_class_name_is_bounded_and_single_line():
    exception = type("Unexpected\nPRIVATE-LOG-INJECTION", (Exception,), {})
    result = safe_connection_error(exception("PRIVATE-ERROR"))
    assert result["exception_type"] == "Exception"
    assert "PRIVATE" not in json.dumps(result)
