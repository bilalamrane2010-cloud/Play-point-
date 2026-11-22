"""Telegram handler tests with fake Update/Bot objects (no network needed)."""

import asyncio
from types import SimpleNamespace

import pytest

import core
import main


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeMessage:
    def __init__(self, text="", chat_id=1, document=None):
        self.text = text
        self.chat_id = chat_id
        self.document = document
        self.replies = []

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.replies.append(text)
        return self


class FakeQuery:
    def __init__(self, data, chat_id=1):
        self.data = data
        self.message = SimpleNamespace(chat_id=chat_id)
        self.edits = []
        self.markups = []
        self.answered = False

    async def answer(self, *args, **kwargs):
        self.answered = True

    async def edit_message_text(self, text, reply_markup=None, **kwargs):
        self.edits.append(text)
        self.markups.append(str(reply_markup))
        return self


class FakeUpdate:
    def __init__(self, chat_id=1, text=None, data=None, document=None):
        self.effective_chat = SimpleNamespace(id=chat_id)
        self.callback_query = FakeQuery(data, chat_id) if data is not None else None
        self.message = FakeMessage(text or "", chat_id, document)
        self.effective_message = self.callback_query or self.message


class FakeFile:
    def __init__(self, payload: bytes):
        self.payload = payload

    async def download_as_bytearray(self):
        return bytearray(self.payload)


class FakeBot:
    def __init__(self, file_payload: bytes = b""):
        self.messages = []
        self.photos = []
        self.file_payload = file_payload

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text))

    async def send_photo(self, chat_id, photo=None, caption=None, **kwargs):
        self.photos.append((chat_id, caption))

    async def send_document(self, chat_id, document=None, caption=None, **kwargs):
        pass

    async def get_file(self, file_id):
        return FakeFile(self.file_payload)


def ctx(bot=None):
    return SimpleNamespace(bot=bot or FakeBot())


@pytest.fixture(autouse=True)
def reset_state():
    main.USER_STATES.clear()
    main.ACTIVE_CHALLENGES.clear()
    main.NOTIFY_CHATS.clear()
    main.BATCH_LOCK = asyncio.Lock()
    yield
    main.USER_STATES.clear()
    main.ACTIVE_CHALLENGES.clear()
    main.NOTIFY_CHATS.clear()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

async def test_start_help_status_cancel():
    for handler in (main.cmd_start, main.cmd_help, main.cmd_status, main.cmd_cancel):
        update = FakeUpdate(chat_id=42)
        await handler(update, ctx())
        assert update.message.replies, handler.__name__
        assert 42 in main.NOTIFY_CHATS


async def test_cancel_resolves_active_challenge():
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    main.ACTIVE_CHALLENGES["a@gmail.com"] = {"future": future, "chat_id": 42, "type": "2fa"}
    update = FakeUpdate(chat_id=42)
    await main.cmd_cancel(update, ctx())
    assert future.result() == "/cancel"
    assert "تم إلغاء" in update.message.replies[0]


# ---------------------------------------------------------------------------
# Dashboard / menu
# ---------------------------------------------------------------------------

def test_dashboard_and_menu_reflect_settings(sample_account):
    text = main.render_dashboard()
    assert "لوحة التحكم" in text
    assert "6 ساعة" in text
    assert "الحسابات: 1" in text
    assert main.build_main_menu() is not None


def test_fmt_duration():
    assert main._fmt_duration(0) == "0 ثانية"
    assert main._fmt_duration(3600) == "1 ساعة"
    assert main._fmt_duration(21600) == "6 ساعة"
    assert main._fmt_duration(3661) == "1 ساعة و1 دقيقة"


# ---------------------------------------------------------------------------
# Callback router: navigation & settings
# ---------------------------------------------------------------------------

async def test_back_main_and_toggle_auto():
    update = FakeUpdate(data="back_main")
    await main.callback_router(update, ctx())
    assert "لوحة التحكم" in update.callback_query.edits[0]

    update = FakeUpdate(data="toggle_auto")
    await main.callback_router(update, ctx())
    assert core.load_settings()["auto_send_enabled"] is True
    update = FakeUpdate(data="toggle_auto")
    await main.callback_router(update, ctx())
    assert core.load_settings()["auto_send_enabled"] is False


async def test_set_interval_prompt_and_cycle_method_and_clear_queue():
    update = FakeUpdate(data="set_interval")
    await main.callback_router(update, ctx())
    assert main.USER_STATES[1]["step"] == "set_interval"

    update = FakeUpdate(data="cycle_method")
    await main.callback_router(update, ctx())
    assert core.load_settings()["send_method"] == "web_form"

    core.enqueue_accounts(["a@gmail.com"])
    update = FakeUpdate(data="clear_queue")
    await main.callback_router(update, ctx())
    assert core.load_scheduler_state()["queue"] == []


async def test_menu_settings_and_stats_and_edit_template():
    update = FakeUpdate(data="menu_settings")
    await main.callback_router(update, ctx())
    assert "الإعدادات" in update.callback_query.edits[0]

    core.record_stat("a@gmail.com", True, "smtp", "S")
    update = FakeUpdate(data="menu_stats")
    await main.callback_router(update, ctx())
    assert "إحصائيات" in update.callback_query.edits[0]

    update = FakeUpdate(data="edit_template")
    await main.callback_router(update, ctx())
    assert main.USER_STATES[1]["step"] == "edit_template"


async def test_unknown_callback():
    update = FakeUpdate(data="does_not_exist")
    await main.callback_router(update, ctx())
    assert "غير معروف" in update.callback_query.edits[0]


# ---------------------------------------------------------------------------
# Accounts menu
# ---------------------------------------------------------------------------

async def test_menu_accounts_empty_and_with_account(sample_account):
    update = FakeUpdate(data="menu_accounts")
    await main.callback_router(update, ctx())
    assert "إدارة الحسابات (1)" in update.callback_query.edits[0]

    update = FakeUpdate(data="acc_list")
    await main.callback_router(update, ctx())
    assert "tester@gmail.com" in update.callback_query.edits[0]


async def test_add_account_flow_via_callbacks_and_text():
    update = FakeUpdate(data="acc_add_choose_mode")
    await main.callback_router(update, ctx())
    assert "طريقة إضافة" in update.callback_query.edits[0]

    update = FakeUpdate(data="acc_add_mode::app_password")
    await main.callback_router(update, ctx())
    assert main.USER_STATES[1]["step"] == "add_email"

    # invalid email keeps the state
    update = FakeUpdate(text="not-an-email")
    await main.text_input_handler(update, ctx())
    assert "غير صالح" in update.message.replies[0]
    assert main.USER_STATES[1]["step"] == "add_email"

    update = FakeUpdate(text="new@gmail.com")
    await main.text_input_handler(update, ctx())
    assert main.USER_STATES[1]["step"] == "add_app_password"

    update = FakeUpdate(text="abcd efgh ijkl mnop")
    await main.text_input_handler(update, ctx())
    assert "تم حفظ الحساب" in update.message.replies[0]
    assert core.get_account("new@gmail.com")["app_password"] == "abcdefghijklmnop"


async def test_add_account_email_code_flow(monkeypatch):
    async def fake_login(*args, **kwargs):
        return None

    monkeypatch.setattr(main, "execute_login_only", fake_login)

    update = FakeUpdate(data="acc_add_mode::email_code")
    await main.callback_router(update, ctx())
    update = FakeUpdate(text="code@gmail.com")
    await main.text_input_handler(update, ctx())
    assert main.USER_STATES[1]["step"] == "add_password"

    update = FakeUpdate(text="secret")
    await main.text_input_handler(update, ctx())
    assert "تم حفظ الحساب" in update.message.replies[0]
    assert core.get_account("code@gmail.com")["password"] == "secret"


async def test_account_selects_and_edit_delete(sample_account):
    for data in ("acc_edit_select", "acc_del_select", "acc_login_select", "acc_test_select", "acc_cookies_select"):
        update = FakeUpdate(data=data)
        await main.callback_router(update, ctx())
        assert "tester@gmail.com" in update.callback_query.markups[0]

    update = FakeUpdate(data="acc_edit_menu::tester@gmail.com")
    await main.callback_router(update, ctx())
    assert "تعديل الحساب" in update.callback_query.edits[0]

    update = FakeUpdate(data="edit_field::tester@gmail.com::fullname")
    await main.callback_router(update, ctx())
    assert main.USER_STATES[1]["field"] == "fullname"

    update = FakeUpdate(text="Bilal Amrane")
    await main.text_input_handler(update, ctx())
    account = core.get_account("tester@gmail.com")
    assert account["first_name"] == "Bilal"
    assert account["last_name"] == "Amrane"

    update = FakeUpdate(data="acc_del_confirm::tester@gmail.com")
    await main.callback_router(update, ctx())
    assert core.get_account("tester@gmail.com") is None


async def test_cookies_prompt_and_text_import(sample_account):
    update = FakeUpdate(data="acc_cookies_prompt::tester@gmail.com")
    await main.callback_router(update, ctx())
    assert main.USER_STATES[1]["step"] == "import_cookies"

    update = FakeUpdate(text='[{"name":"SID","value":"x"}]')
    await main.text_input_handler(update, ctx())
    assert "تم استيراد" in update.message.replies[0]
    assert core.get_account("tester@gmail.com")["logged_in"] is True


async def test_document_cookie_upload(sample_account):
    main.USER_STATES[1] = {"step": "import_cookies", "email": "tester@gmail.com"}
    bot = FakeBot(file_payload=b'[{"name":"SID","value":"fromfile"}]')
    document = SimpleNamespace(file_id="f1")
    update = FakeUpdate(chat_id=1, document=document)
    await main.document_input_handler(update, ctx(bot))
    assert "تم استيراد" in update.message.replies[0]
    assert core.get_account("tester@gmail.com")["cookies"][0]["value"] == "fromfile"


async def test_document_ignored_without_state():
    document = SimpleNamespace(file_id="f1")
    update = FakeUpdate(chat_id=1, document=document)
    await main.document_input_handler(update, ctx(FakeBot()))
    assert update.message.replies == []


async def test_test_account_with_and_without_app_password(monkeypatch, sample_account):
    monkeypatch.setattr(core, "test_smtp", lambda account: (True, "OK"))
    update = FakeUpdate(data="acc_test_run::tester@gmail.com")
    await main.callback_router(update, ctx())
    assert "متصل بنجاح" in update.callback_query.edits[-1]

    core.update_account("tester@gmail.com", app_password="")
    update = FakeUpdate(data="acc_test_run::tester@gmail.com")
    await main.callback_router(update, ctx())
    assert "لا يوجد App Password" in update.callback_query.edits[-1]


# ---------------------------------------------------------------------------
# Sending menu + 6-hour gate
# ---------------------------------------------------------------------------

async def test_send_now_without_accounts():
    update = FakeUpdate(data="menu_send_now")
    await main.callback_router(update, ctx())
    assert "لا توجد حسابات" in update.callback_query.edits[0]


async def test_send_now_lists_accounts_and_all_queues(sample_account):
    core.add_account("second@gmail.com")
    update = FakeUpdate(data="menu_send_now")
    await main.callback_router(update, ctx())
    assert "tester@gmail.com" in update.callback_query.markups[0]

    update = FakeUpdate(data="run_send::ALL")
    await main.callback_router(update, ctx())
    assert "طابور الإرسال" in update.callback_query.edits[0]
    assert set(core.load_scheduler_state()["queue"]) == {"tester@gmail.com", "second@gmail.com"}


async def test_single_send_blocked_until_six_hours(sample_account):
    core.mark_global_send("tester@gmail.com")
    update = FakeUpdate(data="run_send::tester@gmail.com")
    await main.callback_router(update, ctx())
    text = update.callback_query.edits[0]
    assert "لم يمر الفاصل الزمني" in text
    assert "force_send::tester@gmail.com" in update.callback_query.markups[0]


async def test_force_send_runs_task(monkeypatch, sample_account):
    calls = []

    async def fake_send(bot, chat_id, account):
        calls.append(account["email"])
        return True, "ok"

    monkeypatch.setattr(main, "run_one_send", fake_send)
    core.mark_global_send("tester@gmail.com")

    update = FakeUpdate(data="force_send::tester@gmail.com")
    await main.callback_router(update, ctx())
    await asyncio.sleep(0.05)
    assert calls == ["tester@gmail.com"]


async def test_single_send_runs_when_interval_elapsed(monkeypatch, sample_account):
    calls = []

    async def fake_send(bot, chat_id, account):
        calls.append(account["email"])
        return True, "ok"

    monkeypatch.setattr(main, "run_one_send", fake_send)
    update = FakeUpdate(data="run_send::tester@gmail.com")
    await main.callback_router(update, ctx())
    await asyncio.sleep(0.05)
    assert calls == ["tester@gmail.com"]


async def test_login_task_started(monkeypatch, sample_account):
    calls = []

    async def fake_login(bot, chat_id, account):
        calls.append(account["email"])

    monkeypatch.setattr(main, "execute_login_only", fake_login)
    update = FakeUpdate(data="acc_login_run::tester@gmail.com")
    await main.callback_router(update, ctx())
    await asyncio.sleep(0.05)
    assert calls == ["tester@gmail.com"]


# ---------------------------------------------------------------------------
# Text states
# ---------------------------------------------------------------------------

async def test_text_without_state_and_cancel():
    update = FakeUpdate(text="hello")
    await main.text_input_handler(update, ctx())
    assert "/start" in update.message.replies[0]

    update = FakeUpdate(text="/cancel")
    await main.text_input_handler(update, ctx())
    assert "تم إلغاء" in update.message.replies[0]


async def test_text_routed_to_active_challenge():
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    main.ACTIVE_CHALLENGES["a@gmail.com"] = {"future": future, "chat_id": 1, "type": "2fa"}
    update = FakeUpdate(text="123456")
    await main.text_input_handler(update, ctx())
    assert future.result() == "123456"
    assert "تم الاستلام" in update.message.replies[0]


async def test_set_interval_validation():
    main.USER_STATES[1] = {"step": "set_interval"}
    update = FakeUpdate(text="3")
    await main.text_input_handler(update, ctx())
    assert "الحد الأدنى" in update.message.replies[0]
    assert core.load_settings()["send_interval_hours"] == 6

    update = FakeUpdate(text="12")
    await main.text_input_handler(update, ctx())
    assert "12 ساعة" in update.message.replies[0]
    assert core.load_settings()["send_interval_hours"] == 12
    assert core.send_interval_seconds() == 43200

    main.USER_STATES[1] = {"step": "set_interval"}
    update = FakeUpdate(text="abc")
    await main.text_input_handler(update, ctx())
    assert "رقم صحيح" in update.message.replies[0]


async def test_edit_template_text():
    main.USER_STATES[1] = {"step": "edit_template"}
    update = FakeUpdate(text="Subject: New\nBody here")
    await main.text_input_handler(update, ctx())
    assert "تم تحديث" in update.message.replies[0]
    assert core.load_settings()["base_template"].startswith("Subject: New")


# ---------------------------------------------------------------------------
# Challenge flow
# ---------------------------------------------------------------------------

async def test_challenge_photo_and_done_callback():
    bot = FakeBot()
    task = asyncio.create_task(
        main.send_challenge_to_telegram(bot, 1, "a@gmail.com", "2fa", None, "Enter code")
    )
    await asyncio.sleep(0.05)
    assert bot.messages and "Enter code" in bot.messages[0][1]

    update = FakeUpdate(data="ch_done::a@gmail.com")
    await main.callback_router(update, ctx(bot))
    assert await asyncio.wait_for(task, 1) == "done"


async def test_challenge_cancel_callback():
    bot = FakeBot()
    task = asyncio.create_task(
        main.send_challenge_to_telegram(bot, 1, "a@gmail.com", "password", None, "Password?")
    )
    await asyncio.sleep(0.05)
    update = FakeUpdate(data="ch_cancel::a@gmail.com")
    await main.callback_router(update, ctx(bot))
    assert await asyncio.wait_for(task, 1) == "/cancel"


# ---------------------------------------------------------------------------
# run_one_send
# ---------------------------------------------------------------------------

async def test_run_one_send_success(monkeypatch, sample_account):
    async def fake_report():
        return {"subject": "Subj", "body": "Body"}

    async def fake_dispatch(account, subject, body, method="auto", challenge_callback=None):
        return True, "sent", "smtp"

    monkeypatch.setattr(core, "generate_report", fake_report)
    monkeypatch.setattr(core, "dispatch_report", fake_dispatch)

    bot = FakeBot()
    ok, info = await main.run_one_send(bot, 1, sample_account)
    assert ok is True
    assert info == "sent"
    assert core.get_account("tester@gmail.com")["sent"] == 1
    assert core.load_stats()["total_sent"] == 1
    assert core.load_scheduler_state()["last_send_ts"] > 0
    assert "تم إرسال البلاغ بنجاح" in bot.messages[0][1]
    assert "6 ساعة" in bot.messages[0][1]


async def test_run_one_send_failure_does_not_start_timer(monkeypatch, sample_account):
    async def fake_report():
        return {"subject": "Subj", "body": "Body"}

    async def fake_dispatch(account, subject, body, method="auto", challenge_callback=None):
        return False, "boom", "smtp"

    monkeypatch.setattr(core, "generate_report", fake_report)
    monkeypatch.setattr(core, "dispatch_report", fake_dispatch)

    bot = FakeBot()
    ok, _ = await main.run_one_send(bot, 1, sample_account)
    assert ok is False
    assert core.load_scheduler_state()["last_send_ts"] == 0
    assert core.load_stats()["failed"] == 1
    assert "فشل إرسال البلاغ" in bot.messages[0][1]


async def test_run_one_send_handles_dispatch_exception(monkeypatch, sample_account):
    async def fake_report():
        return {"subject": "Subj", "body": "Body"}

    async def boom(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(core, "generate_report", fake_report)
    monkeypatch.setattr(core, "dispatch_report", boom)

    bot = FakeBot()
    ok, info = await main.run_one_send(bot, 1, sample_account)
    assert ok is False
    assert "kaboom" in info


# ---------------------------------------------------------------------------
# Scheduler job
# ---------------------------------------------------------------------------

async def test_scheduler_does_nothing_when_disabled_and_empty(monkeypatch):
    calls = []

    async def fake_send(bot, chat_id, account):
        calls.append(account["email"])
        return True, ""

    monkeypatch.setattr(main, "run_one_send", fake_send)
    await main.scheduler_job(ctx())
    assert calls == []


async def test_scheduler_respects_six_hour_gate(monkeypatch, sample_account):
    calls = []

    async def fake_send(bot, chat_id, account):
        calls.append(account["email"])
        return True, ""

    monkeypatch.setattr(main, "run_one_send", fake_send)
    settings = core.load_settings()
    settings["auto_send_enabled"] = True
    core.save_settings(settings)
    core.mark_global_send("tester@gmail.com")

    await main.scheduler_job(ctx())
    assert calls == []


async def test_scheduler_sends_one_account_when_due(monkeypatch):
    core.add_account("a@gmail.com")
    core.add_account("b@gmail.com")
    calls = []

    async def fake_send(bot, chat_id, account):
        calls.append(account["email"])
        core.mark_global_send(account["email"])
        return True, ""

    monkeypatch.setattr(main, "run_one_send", fake_send)
    settings = core.load_settings()
    settings["auto_send_enabled"] = True
    core.save_settings(settings)

    await main.scheduler_job(ctx())
    assert calls == ["a@gmail.com"]  # only one send per tick

    await main.scheduler_job(ctx())
    assert calls == ["a@gmail.com"]  # interval now blocks the next one


async def test_scheduler_consumes_queue_and_requeues_on_failure(monkeypatch):
    core.add_account("a@gmail.com")
    core.add_account("b@gmail.com")
    core.enqueue_accounts(["b@gmail.com"])
    calls = []

    async def failing_send(bot, chat_id, account):
        calls.append(account["email"])
        return False, "boom"

    monkeypatch.setattr(main, "run_one_send", failing_send)
    await main.scheduler_job(ctx())
    assert calls == ["b@gmail.com"]
    assert core.load_scheduler_state()["queue"] == ["b@gmail.com"]


async def test_scheduler_skips_when_batch_lock_held(monkeypatch):
    core.add_account("a@gmail.com")
    calls = []

    async def fake_send(bot, chat_id, account):
        calls.append(account["email"])
        return True, ""

    monkeypatch.setattr(main, "run_one_send", fake_send)
    settings = core.load_settings()
    settings["auto_send_enabled"] = True
    core.save_settings(settings)

    async with main.BATCH_LOCK:
        await main.scheduler_job(ctx())
    assert calls == []


# ---------------------------------------------------------------------------
# Application wiring / authorization
# ---------------------------------------------------------------------------

def test_build_application_registers_all_handlers():
    app = main.build_application("123456:TEST-TOKEN")
    handlers = app.handlers[0]
    assert len(handlers) == 7
    command_names = {
        h.commands for h in handlers if h.__class__.__name__ == "CommandHandler"
    }
    assert command_names == {frozenset({"start"}), frozenset({"help"}), frozenset({"status"}), frozenset({"cancel"})}
    assert app.job_queue is not None
    jobs = app.job_queue.get_jobs_by_name("scheduler_job")
    assert len(jobs) == 1
    assert app.error_handlers


def test_authorization_whitelist(monkeypatch):
    monkeypatch.setattr(main, "ALLOWED_USER_IDS", {111})
    allowed = FakeUpdate(chat_id=1)
    allowed.effective_user = SimpleNamespace(id=111)
    denied = FakeUpdate(chat_id=2)
    denied.effective_user = SimpleNamespace(id=222)
    assert main.is_authorized(allowed) is True
    assert main.is_authorized(denied) is False

    monkeypatch.setattr(main, "ALLOWED_USER_IDS", set())
    assert main.is_authorized(denied) is True


def test_parse_allowed_ids(monkeypatch):
    monkeypatch.setenv("ALLOWED_USER_IDS", "111, 222;333")
    assert main._parse_allowed_ids() == {111, 222, 333}
    monkeypatch.setenv("ALLOWED_USER_IDS", "")
    assert main._parse_allowed_ids() == set()
