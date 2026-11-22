"""End-to-end SMTP test against a real local SMTP server (aiosmtpd)."""

import socket

import pytest
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult

import core


class _Handler:
    def __init__(self):
        self.envelopes = []

    async def handle_DATA(self, server, session, envelope):
        self.envelopes.append(envelope)
        return "250 Message accepted for delivery"


def _authenticator(server, session, envelope, mechanism, auth_data):
    # Accept any credentials: we only verify the client can AUTH + send.
    return AuthResult(success=True)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def smtp_server(monkeypatch):
    handler = _Handler()
    port = _free_port()
    controller = Controller(
        handler,
        hostname="127.0.0.1",
        port=port,
        authenticator=_authenticator,
        auth_require_tls=False,
    )
    controller.start()
    monkeypatch.setenv("SMTP_HOST", "127.0.0.1")
    monkeypatch.setenv("SMTP_PORT", str(port))
    monkeypatch.setenv("SMTP_SECURITY", "plain")
    monkeypatch.setenv("SMTP_TIMEOUT", "10")
    try:
        yield handler
    finally:
        controller.stop()


def test_send_via_smtp_real_server(smtp_server, sample_account):
    ok, info = core.send_via_smtp(
        sample_account,
        "googleplay-support@google.com",
        "Formal Appeal: Play Points",
        "Hello, this is a real SMTP test body.",
    )
    assert ok, info
    assert "Sent successfully" in info
    assert len(smtp_server.envelopes) == 1
    envelope = smtp_server.envelopes[0]
    assert envelope.mail_from == "tester@gmail.com"
    assert envelope.rcpt_tos == ["googleplay-support@google.com"]
    from email import message_from_bytes

    raw = envelope.content.decode("utf-8", errors="replace")
    assert "Subject: Formal Appeal: Play Points" in raw
    assert "Test User <tester@gmail.com>" in raw
    parsed = message_from_bytes(envelope.content)
    body = parsed.get_payload(0).get_payload(decode=True).decode("utf-8")
    assert body == "Hello, this is a real SMTP test body."


def test_test_smtp_real_server(smtp_server, sample_account):
    ok, info = core.test_smtp(sample_account)
    assert ok, info
    assert "SMTP connection OK" in info


def test_send_via_smtp_missing_password(smtp_server):
    account = {"email": "nopass@gmail.com", "app_password": "", "first_name": "No", "last_name": "Pass"}
    ok, info = core.send_via_smtp(account, "x@google.com", "s", "b")
    assert ok is False
    assert "Missing App Password" in info


def test_send_via_smtp_invalid_sender(smtp_server):
    account = {"email": "not-an-email", "app_password": "abcd", "first_name": "X", "last_name": "Y"}
    ok, info = core.send_via_smtp(account, "x@google.com", "s", "b")
    assert ok is False
    assert "Invalid sender email" in info


def test_send_via_smtp_bad_server_returns_error(monkeypatch, sample_account):
    monkeypatch.setenv("SMTP_HOST", "127.0.0.1")
    monkeypatch.setenv("SMTP_PORT", str(_free_port()))  # nothing listening
    monkeypatch.setenv("SMTP_SECURITY", "plain")
    monkeypatch.setenv("SMTP_TIMEOUT", "2")
    ok, info = core.send_via_smtp(sample_account, "x@google.com", "s", "b")
    assert ok is False
    assert "SMTP" in info


def test_dispatch_report_smtp_channel(smtp_server, sample_account):
    import asyncio

    ok, info, channel = asyncio.run(
        core.dispatch_report(sample_account, "Subj", "Body", method="smtp")
    )
    assert ok, info
    assert channel == "smtp"
    assert len(smtp_server.envelopes) == 1
