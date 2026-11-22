import asyncio
import json
import os

import pytest

import core


# ---------------------------------------------------------------------------
# Validation / accounts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value,expected",
    [
        ("user@gmail.com", True),
        ("first.last+tag@sub.domain.co", True),
        ("bad", False),
        ("bad@", False),
        ("bad@gmail", False),
        ("@gmail.com", False),
        ("user name@gmail.com", False),
        ("", False),
        (None, False),
    ],
)
def test_is_valid_email(value, expected):
    assert core.is_valid_email(value) is expected


def test_add_get_update_remove_account():
    assert core.add_account("User@Gmail.com", app_password="abcd efgh ijkl mnop")
    account = core.get_account("user@gmail.com")
    assert account is not None
    assert account["app_password"] == "abcdefghijklmnop"  # spaces stripped
    assert account["sent"] == 0
    assert account["last_sent_at"] == 0
    assert account["logged_in"] is False
    assert account["profile_dir"].endswith("user_at_gmail_com")

    # duplicate (case-insensitive) is rejected
    assert core.add_account("user@gmail.com") is False
    # invalid email is rejected
    assert core.add_account("not-an-email") is False

    assert core.update_account("USER@gmail.com", sent=5, last_status="ok")
    assert core.get_account("user@gmail.com")["sent"] == 5

    assert core.remove_account("USER@gmail.com") is True
    assert core.get_account("user@gmail.com") is None
    assert core.remove_account("user@gmail.com") is False


def test_accounts_persist_to_disk(data_dir):
    assert core.add_account("persist@gmail.com")
    raw = json.loads((data_dir / "accounts.json").read_text())
    assert raw[0]["email"] == "persist@gmail.com"
    assert core.load_accounts()[0]["email"] == "persist@gmail.com"


def test_corrupted_json_is_backed_up_and_recovers(data_dir):
    (data_dir / "accounts.json").write_text("{not valid json")
    assert core.load_accounts() == []
    backups = list(data_dir.glob("accounts.json.corrupt.*"))
    assert backups, "corrupted file should be preserved for inspection"
    assert core.add_account("after-recovery@gmail.com")


# ---------------------------------------------------------------------------
# Cookies
# ---------------------------------------------------------------------------

def test_import_cookies_plain_list(sample_account):
    payload = [
        {"name": "SID", "value": "abc", "domain": ".google.com", "path": "/", "expiry": 2000000000, "secure": True},
        {"name": "HSID", "value": "def"},
        {"name": "", "value": "ignored"},
        "garbage",
    ]
    ok, message = core.import_cookies_for_account("tester@gmail.com", json.dumps(payload))
    assert ok, message
    account = core.get_account("tester@gmail.com")
    assert account["logged_in"] is True
    assert len(account["cookies"]) == 2
    assert account["cookies"][0]["expiry"] == 2000000000
    assert account["cookies"][1]["domain"] == ".google.com"


def test_import_cookies_wrapped_object(sample_account):
    payload = {"cookies": [{"name": "A", "value": "1"}]}
    ok, _ = core.import_cookies_for_account("tester@gmail.com", json.dumps(payload))
    assert ok
    assert core.get_account("tester@gmail.com")["cookies"] == [
        {"name": "A", "value": "1", "domain": ".google.com", "path": "/"}
    ]


@pytest.mark.parametrize("raw", ["not json", "[]", "{}", '{"cookies": []}', '[{"value": "x"}]'])
def test_import_cookies_invalid(sample_account, raw):
    ok, message = core.import_cookies_for_account("tester@gmail.com", raw)
    assert ok is False
    assert message


def test_import_cookies_unknown_account():
    ok, message = core.import_cookies_for_account("nobody@gmail.com", '[{"name":"a","value":"b"}]')
    assert ok is False
    assert "غير موجود" in message


# ---------------------------------------------------------------------------
# Settings: the 6-hour anti-spam floor
# ---------------------------------------------------------------------------

def test_default_settings_are_six_hours():
    settings = core.default_settings()
    assert settings["send_interval_hours"] == 6
    assert settings["between_accounts_delay"] == 21600
    assert core.send_interval_seconds(settings) == 21600


def test_old_settings_file_is_forced_to_six_hours(data_dir):
    (data_dir / "settings.json").write_text(json.dumps({"send_interval_hours": 1, "auto_send_interval": 1}))
    settings = core.load_settings()
    assert settings["send_interval_hours"] == 6
    assert settings["auto_send_interval"] == 360
    assert settings["between_accounts_delay"] == 21600
    assert core.send_interval_seconds(settings) == 21600


@pytest.mark.parametrize("hours", [0, 1, 5.9, -3, "abc", None])
def test_send_interval_never_below_six_hours(hours):
    settings = dict(core.default_settings(), send_interval_hours=hours)
    assert core.send_interval_seconds(settings) >= 21600


def test_larger_interval_is_respected():
    settings = dict(core.default_settings(), send_interval_hours=12)
    assert core.send_interval_seconds(settings) == 43200


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def test_record_stats_and_history_cap():
    core.record_stat("a@gmail.com", True, "smtp", "Subject A")
    core.record_stat("a@gmail.com", False, "web_form", "Subject B")
    stats = core.load_stats()
    assert stats["total_sent"] == 1
    assert stats["failed"] == 1
    assert len(stats["history"]) == 2
    assert stats["history"][0]["success"] is True

    for i in range(300):
        core.record_stat("a@gmail.com", True, "smtp", f"S{i}")
    assert len(core.load_stats()["history"]) == 250


# ---------------------------------------------------------------------------
# Scheduler / 6-hour gate
# ---------------------------------------------------------------------------

def test_global_send_blocks_next_six_hours():
    now = 1_700_000_000.0
    core.mark_global_send("a@gmail.com", when=now)
    assert core.seconds_until_next_send(now=now) == 21600
    assert core.seconds_until_next_send(now=now + 3600) == 18000
    assert core.seconds_until_next_send(now=now + 21599) == 1
    assert core.seconds_until_next_send(now=now + 21600) == 0
    assert core.seconds_until_next_send(now=now + 999999) == 0


def test_mark_global_send_updates_account(sample_account):
    core.mark_global_send("tester@gmail.com", when=123456.0)
    account = core.get_account("tester@gmail.com")
    assert account["last_sent_at"] == 123456.0
    state = core.load_scheduler_state()
    assert state["last_account"] == "tester@gmail.com"
    assert state["rr_index"] == 1


def test_queue_is_fifo_and_deduplicated():
    core.enqueue_accounts(["a@gmail.com", "b@gmail.com"])
    assert core.enqueue_accounts(["b@gmail.com", "c@gmail.com"]) == 3
    assert core.load_scheduler_state()["queue"] == ["a@gmail.com", "b@gmail.com", "c@gmail.com"]

    accounts = [
        {"email": "a@gmail.com"},
        {"email": "b@gmail.com"},
        {"email": "c@gmail.com"},
    ]
    assert core.pick_next_account(accounts)["email"] == "a@gmail.com"
    assert core.pick_next_account(accounts)["email"] == "b@gmail.com"
    assert core.load_scheduler_state()["queue"] == ["c@gmail.com"]
    core.clear_queue()
    assert core.load_scheduler_state()["queue"] == []


def test_round_robin_when_queue_empty():
    accounts = [{"email": "a@gmail.com"}, {"email": "b@gmail.com"}, {"email": "c@gmail.com"}]
    picked = [core.pick_next_account(accounts)["email"] for _ in range(6)]
    assert picked == [
        "a@gmail.com", "b@gmail.com", "c@gmail.com",
        "a@gmail.com", "b@gmail.com", "c@gmail.com",
    ]


def test_pick_next_account_skips_deleted_queue_entries():
    core.enqueue_accounts(["gone@gmail.com", "b@gmail.com"])
    accounts = [{"email": "a@gmail.com"}, {"email": "b@gmail.com"}]
    assert core.pick_next_account(accounts)["email"] == "b@gmail.com"


def test_scheduler_state_survives_restart(data_dir):
    core.mark_global_send("a@gmail.com", when=1000.0)
    core.enqueue_accounts(["b@gmail.com"])
    # simulate a restart: files are the only source of truth
    state = json.loads((data_dir / "scheduler_state.json").read_text())
    assert state["last_send_ts"] == 1000.0
    assert state["queue"] == ["b@gmail.com"]
    assert core.seconds_until_next_send(now=1000.0) == 21600


# ---------------------------------------------------------------------------
# Report generation (offline fallback)
# ---------------------------------------------------------------------------

def test_generate_report_offline_uses_template():
    report = asyncio.run(core.generate_report())
    assert report["subject"].startswith("Formal Appeal")
    assert "Google Play" in report["body"]


def test_generate_report_uses_custom_template():
    settings = core.load_settings()
    settings["base_template"] = "Subject: My Subject\nMy custom body"
    core.save_settings(settings)
    report = asyncio.run(core.generate_report())
    assert report["subject"] == "My Subject"
    assert report["body"] == "My custom body"


# ---------------------------------------------------------------------------
# Chrome helpers that do not need a browser
# ---------------------------------------------------------------------------

def test_parse_resolution():
    assert core._parse_resolution("1080x2400") == (1080, 2400)
    assert core._parse_resolution("bad") == (412, 915)
    assert core._parse_resolution("10x10") == (412, 915)


def test_apply_saved_cookies_with_fake_driver(sample_account):
    class FakeDriver:
        def __init__(self):
            self.visited = []
            self.cookies = []

        def get(self, url):
            self.visited.append(url)

        def add_cookie(self, cookie):
            if cookie["name"] == "BAD":
                raise RuntimeError("rejected")
            self.cookies.append(cookie)

    account = dict(sample_account)
    account["cookies"] = [
        {"name": "SID", "value": "1", "domain": ".google.com", "path": "/", "expiry": 2000000000, "secure": True},
        {"name": "BAD", "value": "2"},
        {"name": "HSID", "value": "3"},
    ]
    driver = FakeDriver()
    assert core._apply_saved_cookies(driver, account) == 2
    assert driver.visited == ["https://www.google.com/"]
    assert driver.cookies[0]["expiry"] == 2000000000
    assert driver.cookies[0]["secure"] is True

    driver2 = FakeDriver()
    assert core._apply_saved_cookies(driver2, dict(account, cookies=[])) == 0
    assert driver2.visited == []


def test_capture_screenshot_with_fake_driver(data_dir, sample_account):
    class FakeDriver:
        def save_screenshot(self, path):
            with open(path, "wb") as fh:
                fh.write(b"png")

    path = core._capture_screenshot(FakeDriver(), "user/../x@gmail.com")
    assert path and os.path.exists(path)
    assert os.path.dirname(path).endswith("screenshots")
    # slashes must never survive in the filename (no path traversal)
    assert "/" not in os.path.basename(path)
    assert os.path.realpath(path).startswith(os.path.realpath(str(data_dir)))


def test_smtp_message_builder():
    from email import message_from_string

    message = core._build_smtp_message("a@gmail.com", "A B", "to@google.com", "Subj", "Body")
    assert message["From"] == "A B <a@gmail.com>"
    assert message["To"] == "to@google.com"
    assert message["Subject"] == "Subj"
    parsed = message_from_string(message.as_string())
    payload = parsed.get_payload(0).get_payload(decode=True).decode("utf-8")
    assert payload == "Body"
