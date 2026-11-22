"""Real Chromium tests: launch, navigate, fill a form, cookies, screenshots."""

import asyncio
import http.server
import pathlib
import threading

import pytest

import core

FORM_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Mock Support Form</title></head>
<body>
  <h1>Contact Support</h1>
  <form id="f" onsubmit="event.preventDefault();
      document.getElementById('result').textContent =
        'submitted:' + document.getElementById('name').value + ':' + document.getElementById('body').value;">
    <input id="name" type="text" placeholder="Name">
    <textarea id="body" placeholder="Message"></textarea>
    <button id="send" type="submit">Send</button>
  </form>
  <div id="result"></div>
</body></html>
"""


@pytest.fixture
def web_server(tmp_path):
    (tmp_path / "form.html").write_text(FORM_HTML, encoding="utf-8")

    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    handler = lambda *a, **k: QuietHandler(*a, directory=str(tmp_path), **k)  # noqa: E731
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/form.html"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def driver(sample_account):
    drv = core._build_chrome_driver(sample_account)
    try:
        yield drv
    finally:
        drv.quit()


def test_chromium_launches_and_reports_version(driver):
    assert driver.capabilities.get("browserName", "").lower() in ("chrome", "chromium")
    assert core._resolve_chrome_binary()
    assert core._resolve_chromedriver_path()


def test_fill_and_submit_local_form(driver, web_server):
    from selenium.webdriver.common.by import By

    driver.get(web_server)
    assert "Mock Support Form" in driver.title

    driver.find_element(By.ID, "name").send_keys("Bilal Amran")
    driver.find_element(By.ID, "body").send_keys("Please review my Play Points account.")
    driver.find_element(By.ID, "send").click()

    result = driver.find_element(By.ID, "result").text
    assert result == "submitted:Bilal Amran:Please review my Play Points account."


def test_cookie_injection_roundtrip(driver, web_server):
    driver.get(web_server)
    driver.add_cookie({"name": "SID", "value": "abc123", "path": "/"})
    cookies = {c["name"]: c["value"] for c in driver.get_cookies()}
    assert cookies.get("SID") == "abc123"


def test_real_screenshot_capture(driver, web_server, data_dir):
    driver.get(web_server)
    path = core._capture_screenshot(driver, "tester@gmail.com")
    assert path
    assert pathlib.Path(path).exists()
    assert pathlib.Path(path).stat().st_size > 1000


def test_page_text_and_captcha_helpers(driver, web_server):
    driver.get(web_server)
    text = core._page_text(driver)
    assert "contact support" in text
    assert core._is_insecure_browser_block(driver) is False
    assert core._has_real_captcha_challenge(driver) is False


def test_interactive_login_detects_saved_session(monkeypatch, sample_account):
    class FakeDriver:
        def __init__(self):
            self.current_url = ""

        def get(self, url):
            self.current_url = "https://myaccount.google.com/?hl=en"

        @property
        def title(self):
            return "My Account"

        @property
        def page_source(self):
            return "<html></html>"

        def find_elements(self, *args, **kwargs):
            return []

    async def no_sleep(*args, **kwargs):
        return None

    monkeypatch.setattr(core.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(core.time, "sleep", lambda *a, **k: None)

    ok, message = asyncio.run(
        core._interactive_google_login(FakeDriver(), sample_account)
    )
    assert ok is True
    assert "saved session" in message
    assert core.get_account("tester@gmail.com")["logged_in"] is True


def test_interactive_login_detects_rejected_email(monkeypatch, sample_account):
    class FakeElement:
        def clear(self):
            pass

        def send_keys(self, *args):
            pass

    class FakeDriver:
        current_url = "https://accounts.google.com/signin/v2/identifier?hl=en"
        title = "Sign in"
        page_source = "<html>Couldn't find your Google Account</html>"

        def get(self, url):
            self.current_url = "https://accounts.google.com/signin/v2/identifier?hl=en"

        def find_elements(self, *args, **kwargs):
            return []

        def find_element(self, *args, **kwargs):
            return FakeElement()

    async def no_sleep(*args, **kwargs):
        return None

    monkeypatch.setattr(core.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(core.time, "sleep", lambda *a, **k: None)

    ok, message = asyncio.run(
        core._interactive_google_login(FakeDriver(), sample_account, timeout=1)
    )
    assert ok is False
    assert "rejected" in message.lower()
