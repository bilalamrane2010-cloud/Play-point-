"""Telegram control panel for the Play Points support bot."""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import time
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import core

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    level=logging.INFO,
)

# The token MUST be supplied through the environment (TELEGRAM_BOT_TOKEN).
# Never hard-code it: this repository is public and the token would be exposed.
DEFAULT_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

USER_STATES: dict[int, dict] = {}
ACTIVE_CHALLENGES: dict[str, dict] = {}
NOTIFY_CHATS: set[int] = set()
BATCH_LOCK = asyncio.Lock()
SCHEDULER_TICK_SECONDS = 30

METHOD_LABELS = {
    "auto": "تلقائي ذكي",
    "web_form": "نموذج الدعم",
    "smtp": "البريد المباشر",
    "gmail_web": "ويب Gmail",
}


def _parse_allowed_ids() -> set[int]:
    raw = os.getenv("ALLOWED_USER_IDS", "").strip()
    result: set[int] = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk.lstrip("-").isdigit():
            result.add(int(chunk))
    return result


ALLOWED_USER_IDS = _parse_allowed_ids()


def is_authorized(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    user = update.effective_user
    return bool(user and user.id in ALLOWED_USER_IDS)


def restricted(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = getattr(update, "effective_user", None)
        if user is not None:
            # Printed so the owner can read their numeric ID from the logs and
            # put it in ALLOWED_USER_IDS to lock the bot down.
            logging.info(
                "Update from user_id=%s username=@%s name=%s",
                user.id,
                user.username or "-",
                user.full_name or "-",
            )
        if not is_authorized(update):
            text = "⛔ غير مصرح لك باستخدام هذا البوت."
            if update.callback_query:
                try:
                    await update.callback_query.answer(text, show_alert=True)
                except Exception:  # noqa: BLE001
                    pass
            elif update.effective_message:
                await update.effective_message.reply_text(text)
            return None
        return await func(update, context)

    return wrapper


def _fmt_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours} ساعة")
    if minutes:
        parts.append(f"{minutes} دقيقة")
    if not parts:
        parts.append(f"{secs} ثانية")
    return " و".join(parts)


def build_main_menu() -> InlineKeyboardMarkup:
    settings = core.load_settings()
    auto_icon = "🟢" if settings.get("auto_send_enabled") else "🔴"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🚀 إرسال بلاغ الآن", callback_data="menu_send_now"),
                InlineKeyboardButton("👤 الحسابات", callback_data="menu_accounts"),
            ],
            [
                InlineKeyboardButton(f"{auto_icon} الإرسال التلقائي", callback_data="toggle_auto"),
                InlineKeyboardButton("⏱️ الفاصل الزمني", callback_data="set_interval"),
            ],
            [
                InlineKeyboardButton("⚙️ الإعدادات", callback_data="menu_settings"),
                InlineKeyboardButton("📊 السجل", callback_data="menu_stats"),
            ],
        ]
    )


def _transport_label() -> str:
    """Human-readable label for the active email transport."""
    transport = core._email_transport()
    providers = core._https_email_providers()
    if transport in ("brevo", "resend", "apps_script"):
        return f"{transport} (HTTPS) {'✅' if providers.get(transport) else '❌ غير مهيأ'}"
    if transport == "smtp":
        return "SMTP مباشر"
    if any(providers.values()):
        return "SMTP ثم HTTPS تلقائياً ✅"
    return "SMTP فقط (قد يُحجب على Railway)"


def render_dashboard() -> str:
    accounts = core.load_accounts()
    settings = core.load_settings()
    stats = core.load_stats()
    state = core.load_scheduler_state()
    interval_hours = core.send_interval_seconds(settings) / 3600
    remaining = core.seconds_until_next_send(settings)
    queue_len = len(state.get("queue") or [])
    next_send = "جاهز الآن" if remaining <= 0 else f"بعد {_fmt_duration(remaining)}"
    return (
        "🛡️ لوحة التحكم\n"
        "━━━━━━━━━━━━━━\n"
        f"• الحسابات: {len(accounts)}\n"
        f"• الإرسال التلقائي: {'مفعّل ✅' if settings.get('auto_send_enabled') else 'متوقف ⏸️'}\n"
        f"• الفاصل بين كل إرسال: {interval_hours:g} ساعة\n"
        f"• الإرسال القادم: {next_send}\n"
        f"• في الطابور: {queue_len}\n"
        f"• الطريقة: {METHOD_LABELS.get(settings.get('send_method', 'auto'), 'تلقائي ذكي')}\n"
        f"• قناة البريد: {_transport_label()}\n"
        f"• تم الإرسال: {stats.get('total_sent', 0)} | فشل: {stats.get('failed', 0)}\n"
        "━━━━━━━━━━━━━━"
    )


async def _edit_or_reply(update: Update, text: str, reply_markup=None) -> None:
    query = update.callback_query
    if query:
        try:
            await query.edit_message_text(text, reply_markup=reply_markup)
            return
        except Exception:  # noqa: BLE001
            pass
    if update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=reply_markup)


@restricted
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    NOTIFY_CHATS.add(chat_id)
    USER_STATES.pop(chat_id, None)
    await update.message.reply_text(render_dashboard(), reply_markup=build_main_menu())


@restricted
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 الأوامر المتاحة\n"
        "• /start لوحة التحكم\n"
        "• /status حالة الإرسال القادم\n"
        "• /cancel إلغاء أي عملية جارية\n"
        "• /selftest فحص شامل لكل مكوّنات البوت\n\n"
        "الفاصل الأدنى بين كل إرسال والذي يليه هو 6 ساعات لكل الحسابات، "
        "ويمكن زيادته من الإعدادات.",
        reply_markup=build_main_menu(),
    )


@restricted
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(render_dashboard(), reply_markup=build_main_menu())


@restricted
async def cmd_selftest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """End-to-end self test that runs inside Telegram and reports back."""
    await update.message.reply_text("🧪 جارٍ فحص كل مكوّنات البوت... قد يستغرق حتى دقيقة.")

    lines: list[str] = []
    passed = 0
    failed = 0

    def add(ok: bool, label: str, detail: str = "") -> None:
        nonlocal passed, failed
        if ok:
            passed += 1
            lines.append(f"✅ {label}" + (f" — {detail}" if detail else ""))
        else:
            failed += 1
            lines.append(f"❌ {label}" + (f" — {detail}" if detail else ""))

    # 1. Telegram connectivity
    try:
        me = await context.bot.get_me()
        add(True, "الاتصال بتلغرام", f"@{me.username}")
    except Exception as exc:  # noqa: BLE001
        add(False, "الاتصال بتلغرام", core._format_exc(exc)[:120])

    # 2. Accounts
    accounts = core.load_accounts()
    add(bool(accounts), "الحسابات المسجّلة", f"{len(accounts)} حساب")

    # 3. Anti-spam interval
    settings = core.load_settings()
    interval_h = core.send_interval_seconds(settings) / 3600
    add(
        core.send_interval_seconds(settings) >= core.MIN_SEND_INTERVAL_SECONDS,
        "الفاصل الإجباري بين الإرسال",
        f"{interval_h:g} ساعة (الحد الأدنى 6)",
    )

    # 4. Email transport configuration
    transport = core._email_transport()
    providers = core._https_email_providers()
    has_https = any(providers.values())
    add(
        has_https or transport == "smtp",
        "قناة البريد",
        _transport_label(),
    )

    # 5. Chromium (needed for the web login / GetHelp form)
    chrome = core._resolve_chrome_binary()
    add(bool(chrome), "متصفح Chromium", chrome or "غير موجود")

    # 6. AI report generation
    try:
        report = await asyncio.wait_for(core.generate_report(), timeout=60)
        add(
            bool(report.get("body")),
            "توليد البلاغ بالذكاء الاصطناعي",
            f"{len(report.get('body', ''))} حرف",
        )
    except Exception as exc:  # noqa: BLE001
        add(False, "توليد البلاغ بالذكاء الاصطناعي", core._format_exc(exc)[:120])

    # 7. Live delivery test per account
    for account in accounts[:3]:
        email = account.get("email", "?")
        has_pw = bool((account.get("app_password") or "").strip())
        if not has_pw and not has_https:
            add(False, f"بيانات {email}", "لا App Password ولا قناة HTTPS")
            continue
        try:
            ok, info = await asyncio.wait_for(
                asyncio.to_thread(core.test_smtp, account), timeout=45
            )
            add(ok, f"الإرسال من {email}", info[:160])
        except Exception as exc:  # noqa: BLE001
            add(False, f"الإرسال من {email}", core._format_exc(exc)[:120])

    header = "🧪 نتيجة الفحص الشامل\n" + "─" * 14 + "\n"
    footer = (
        "\n" + "─" * 14 + f"\n✅ نجح: {passed}   ❌ فشل: {failed}\n"
        + ("🎉 كل شيء يعمل!" if failed == 0 else "⚠️ راجع العناصر الفاشلة أعلاه.")
    )
    await update.message.reply_text(
        header + "\n".join(lines) + footer, reply_markup=build_main_menu()
    )


@restricted
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    USER_STATES.pop(chat_id, None)
    cancelled = 0
    for email, challenge in list(ACTIVE_CHALLENGES.items()):
        if challenge["chat_id"] == chat_id and not challenge["future"].done():
            challenge["future"].set_result("/cancel")
            cancelled += 1
    suffix = f" (تم إلغاء {cancelled} تحقق)" if cancelled else ""
    await update.message.reply_text(f"✅ تم إلغاء العملية.{suffix}", reply_markup=build_main_menu())


async def send_challenge_to_telegram(bot, chat_id, email, challenge_type, screenshot_path, prompt_text):
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    ACTIVE_CHALLENGES[email] = {"future": future, "chat_id": chat_id, "type": challenge_type}
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ تم التأكيد", callback_data=f"ch_done::{email}")],
            [InlineKeyboardButton("❌ إلغاء", callback_data=f"ch_cancel::{email}")],
        ]
    )
    caption = f"🔐 تحقق مطلوب للحساب: {email}\n\n{prompt_text}"
    sent_photo = False
    if screenshot_path and os.path.exists(screenshot_path):
        try:
            with open(screenshot_path, "rb") as photo:
                await bot.send_photo(chat_id=chat_id, photo=photo, caption=caption, reply_markup=keyboard)
            sent_photo = True
        except Exception as exc:  # noqa: BLE001
            logging.warning("Failed to send challenge photo: %s", core._format_exc(exc))
    if not sent_photo:
        await bot.send_message(chat_id=chat_id, text=caption, reply_markup=keyboard)

    try:
        return await asyncio.wait_for(future, timeout=300)
    except asyncio.TimeoutError:
        return "/cancel"
    finally:
        ACTIVE_CHALLENGES.pop(email, None)


async def _safe_ack_challenge(query, status_text: str) -> None:
    try:
        if query.message and query.message.photo:
            await query.edit_message_caption(status_text)
        else:
            await query.edit_message_text(status_text)
    except Exception:  # noqa: BLE001
        pass


def _challenge_callback(bot, chat_id: Optional[int]):
    target_chat = chat_id
    if target_chat is None and NOTIFY_CHATS:
        target_chat = next(iter(NOTIFY_CHATS))
    if target_chat is None:
        return None

    async def callback(email, challenge_type, screenshot_path, prompt_text):
        return await send_challenge_to_telegram(bot, target_chat, email, challenge_type, screenshot_path, prompt_text)

    return callback


async def run_one_send(bot, chat_id: Optional[int], account: dict) -> tuple[bool, str]:
    """Generate and deliver one report, then update stats and the global timer."""
    settings = core.load_settings()
    method = settings.get("send_method", "auto")
    report = await core.generate_report()
    callback = _challenge_callback(bot, chat_id)
    try:
        ok, info, used_channel = await core.dispatch_report(
            account,
            report["subject"],
            report["body"],
            method=method,
            challenge_callback=callback,
        )
    except Exception as exc:  # noqa: BLE001
        ok, info, used_channel = False, core._format_exc(exc), method

    core.record_stat(account["email"], ok, used_channel, report["subject"])
    if ok:
        latest = core.get_account(account["email"]) or account
        core.update_account(
            account["email"],
            sent=int(latest.get("sent", 0)) + 1,
            last_status="تم الإرسال ✅",
        )
        core.mark_global_send(account["email"])
        message = (
            "✅ تم إرسال البلاغ بنجاح\n"
            f"• الحساب: {account['email']}\n"
            f"• القناة: {METHOD_LABELS.get(used_channel, used_channel)}\n"
            f"• العنوان: {report['subject']}\n"
            f"• الإرسال القادم بعد: {_fmt_duration(core.send_interval_seconds(settings))}"
        )
    else:
        core.update_account(account["email"], last_status="فشل الإرسال ❌")
        delay = core.mark_failed_send(account["email"])
        hint = ""
        if core._is_network_blocked(info):
            hint = (
                "\n\n💡 منصة الاستضافة تحجب منافذ SMTP. الحل: أضف APPS_SCRIPT_URL "
                "(ملف google_apps_script.gs) في متغيرات البيئة وسيعمل الإرسال عبر HTTPS."
            )
        message = (
            f"❌ فشل إرسال البلاغ ({account['email']})\n"
            f"السبب: {info}{hint}\n\n"
            f"⏳ تم إيقاف المحاولات مؤقتاً لمدة {_fmt_duration(delay)} لتجنب التكرار."
        )

    targets = [chat_id] if chat_id is not None else list(NOTIFY_CHATS)
    for target in targets:
        try:
            await bot.send_message(target, message)
        except Exception as exc:  # noqa: BLE001
            logging.warning("Could not notify chat %s: %s", target, core._format_exc(exc))
    return ok, info


def _login_failure_hint(message: str) -> str:
    """Return actionable Arabic guidance for a failed Google web login."""
    low = (message or "").lower()
    if "may not be secure" in low or "not trusted" in low or "blocked" in low:
        return (
            "\n\n💡 الحل الأفضل: استخدم «بريد + App Password» بدل تسجيل الدخول. "
            "هذه الطريقة لا تحتاج تحقق Google وتعمل دائماً.\n"
            "أنشئ App Password من: myaccount.google.com/apppasswords\n"
            "ثم: الحسابات ← إضافة حساب ← بريد + App Password."
        )
    if "rejected" in low or "رفض" in message:
        return (
            "\n\n💡 أعد المحاولة وكن مستعداً: سيصل إشعار إلى هاتفك، اضغط Yes ثم الرقم الظاهر، "
            "وبعدها اضغط زر «تم التأكيد» في تلغرام خلال دقيقتين."
        )
    if "cancelled" in low:
        return (
            "\n\n💡 ألغيت العملية. أعد المحاولة، وإذا ظهرت شاشة تحقق افتح الصورة، "
            "أكمل التحقق من هاتفك، ثم اضغط «تم التأكيد»."
        )
    if "timeout" in low:
        return "\n\n💡 انتهت المهلة. أعد المحاولة وأكمل التحقق من هاتفك بسرعة."
    if "chromium" in low or "chrome" in low:
        return "\n\n💡 المتصفح غير مثبت في الخادم. تأكد من بناء الصورة من Dockerfile المرفق."
    return ""


async def execute_login_only(bot, chat_id: int, account: dict) -> None:
    account = await core.ensure_account_session_identity(account)
    driver = None
    try:
        driver = core._build_chrome_driver(account)
        ok, message = await core._interactive_google_login(
            driver, account, _challenge_callback(bot, chat_id)
        )
        if ok:
            core.extract_and_save_profile_names(driver, account)
            await bot.send_message(chat_id, f"✅ تم تسجيل الدخول بنجاح: {account['email']}")
        else:
            hint = _login_failure_hint(message)
            await bot.send_message(
                chat_id, f"❌ تعذر تسجيل الدخول ({account['email']}): {message}{hint}"
            )
    except Exception as exc:  # noqa: BLE001
        await bot.send_message(chat_id, f"❌ حدث خطأ: {core._format_exc(exc)}")
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:  # noqa: BLE001
                pass


@restricted
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:  # noqa: BLE001
        pass
    data = query.data or ""
    chat_id = query.message.chat_id if query.message else update.effective_chat.id
    NOTIFY_CHATS.add(chat_id)

    if data.startswith("ch_done::"):
        email = data.split("::", 1)[1]
        challenge = ACTIVE_CHALLENGES.get(email)
        if challenge and not challenge["future"].done():
            challenge["future"].set_result("done")
        await _safe_ack_challenge(query, "✅ جاري المتابعة...")
        return

    if data.startswith("ch_cancel::"):
        email = data.split("::", 1)[1]
        challenge = ACTIVE_CHALLENGES.get(email)
        if challenge and not challenge["future"].done():
            challenge["future"].set_result("/cancel")
        await _safe_ack_challenge(query, "❌ تم الإلغاء.")
        return

    if data in ("refresh_main", "back_main"):
        USER_STATES.pop(chat_id, None)
        await _edit_or_reply(update, render_dashboard(), build_main_menu())
        return

    if data == "toggle_auto":
        settings = core.load_settings()
        settings["auto_send_enabled"] = not settings.get("auto_send_enabled", False)
        core.save_settings(settings)
        await _edit_or_reply(update, render_dashboard(), build_main_menu())
        return

    if data in ("set_interval", "edit_between_delay"):
        USER_STATES[chat_id] = {"step": "set_interval"}
        await _edit_or_reply(
            update,
            f"⏱️ أرسل الفاصل الزمني بالساعات بين كل إرسال والذي يليه.\n"
            f"الحد الأدنى الإجباري هو {core.MIN_SEND_INTERVAL_HOURS} ساعات (مثال: 6 أو 12) أو /cancel:",
        )
        return

    if data == "menu_send_now":
        accounts = core.load_accounts()
        if not accounts:
            await _edit_or_reply(
                update,
                "❌ لا توجد حسابات مضافة بعد.",
                InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="back_main")]]),
            )
            return
        keyboard = [
            [InlineKeyboardButton("📨 إرسال لكل الحسابات (بالدور)", callback_data="run_send::ALL")]
        ]
        for account in accounts:
            keyboard.append([InlineKeyboardButton(f"👤 {account['email']}", callback_data=f"run_send::{account['email']}")])
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="back_main")])
        await _edit_or_reply(
            update,
            "اختر إرسال البلاغ لكل الحسابات أو لحساب واحد:",
            InlineKeyboardMarkup(keyboard),
        )
        return

    if data.startswith("run_send::"):
        target = data.split("::", 1)[1]
        accounts = core.load_accounts()
        if not accounts:
            await _edit_or_reply(update, "❌ لا توجد حسابات.")
            return

        if target == "ALL":
            queued = core.enqueue_accounts([a["email"] for a in accounts])
            remaining = core.seconds_until_next_send()
            await _edit_or_reply(
                update,
                f"🧾 تمت إضافة {len(accounts)} حساب إلى طابور الإرسال.\n"
                f"• إجمالي الطابور: {queued}\n"
                f"• سيبدأ أول إرسال: {'خلال 30 ثانية' if remaining <= 0 else 'بعد ' + _fmt_duration(remaining)}\n"
                f"• ثم إرسال واحد كل {core.MIN_SEND_INTERVAL_HOURS} ساعات على الأقل.",
                InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="back_main")]]),
            )
            return

        account = core.get_account(target)
        if not account:
            await _edit_or_reply(update, "❌ الحساب غير موجود.")
            return
        remaining = core.seconds_until_next_send()
        if remaining > 0:
            await _edit_or_reply(
                update,
                f"⏳ لم يمر الفاصل الزمني بعد. الإرسال القادم بعد {_fmt_duration(remaining)}.\n"
                "يمكنك الانتظار أو تجاوز الفاصل يدوياً (قد يزيد خطر الحظر).",
                InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("⚠️ تجاوز الفاصل والإرسال الآن", callback_data=f"force_send::{account['email']}")],
                        [InlineKeyboardButton("🔙 رجوع", callback_data="back_main")],
                    ]
                ),
            )
            return
        await _edit_or_reply(update, f"⏳ جاري إرسال البلاغ من الحساب: {account['email']}...")
        asyncio.create_task(run_one_send(context.bot, chat_id, account))
        return

    if data.startswith("force_send::"):
        email = data.split("::", 1)[1]
        account = core.get_account(email)
        if not account:
            await _edit_or_reply(update, "❌ الحساب غير موجود.")
            return
        await _edit_or_reply(update, f"⚠️ تجاوز الفاصل: جاري الإرسال من {email}...")
        asyncio.create_task(run_one_send(context.bot, chat_id, account))
        return

    if data == "menu_accounts":
        accounts = core.load_accounts()
        keyboard = [
            [
                InlineKeyboardButton("➕ إضافة حساب", callback_data="acc_add_choose_mode"),
                InlineKeyboardButton("📋 قائمة الحسابات", callback_data="acc_list"),
            ],
            [
                InlineKeyboardButton("✏️ تعديل حساب", callback_data="acc_edit_select"),
                InlineKeyboardButton("🗑️ حذف حساب", callback_data="acc_del_select"),
            ],
            [
                InlineKeyboardButton("🔐 تسجيل دخول", callback_data="acc_login_select"),
                InlineKeyboardButton("🍪 استيراد كوكيز", callback_data="acc_cookies_select"),
            ],
            [
                InlineKeyboardButton("🧪 فحص البريد", callback_data="acc_test_select"),
                InlineKeyboardButton("🔙 رجوع", callback_data="back_main"),
            ],
        ]
        await _edit_or_reply(
            update,
            f"👤 إدارة الحسابات ({len(accounts)})",
            InlineKeyboardMarkup(keyboard),
        )
        return

    if data == "acc_add_choose_mode":
        keyboard = [
            [InlineKeyboardButton("🔑 بريد + App Password (مستحسن)", callback_data="acc_add_mode::app_password")],
            [InlineKeyboardButton("📧 بريد + كلمة المرور", callback_data="acc_add_mode::email_code")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="menu_accounts")],
        ]
        await _edit_or_reply(update, "اختر طريقة إضافة الحساب:", InlineKeyboardMarkup(keyboard))
        return

    if data.startswith("acc_add_mode::"):
        mode = data.split("::", 1)[1]
        USER_STATES[chat_id] = {"step": "add_email", "auth_type": mode}
        await _edit_or_reply(update, "📧 أرسل عنوان البريد الإلكتروني (Gmail):")
        return

    if data == "acc_list":
        accounts = core.load_accounts()
        if not accounts:
            await _edit_or_reply(
                update,
                "لا توجد حسابات محفوظة.",
                InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_accounts")]]),
            )
            return
        blocks = []
        for account in accounts:
            blocks.append(
                f"• {account['email']}\n"
                f"  الحالة: {account.get('last_status', 'جديد')} | المرسل: {account.get('sent', 0)}"
            )
        text = "\n\n".join(blocks)
        if len(text) > 3900:
            text = text[:3900] + "\n..."
        await _edit_or_reply(
            update,
            text,
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_accounts")]]),
        )
        return

    if data in ("acc_edit_select", "acc_del_select", "acc_login_select", "acc_test_select", "acc_cookies_select"):
        accounts = core.load_accounts()
        if not accounts:
            await _edit_or_reply(
                update,
                "لا توجد حسابات.",
                InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_accounts")]]),
            )
            return
        prefix_map = {
            "acc_edit_select": "acc_edit_menu",
            "acc_del_select": "acc_del_confirm",
            "acc_login_select": "acc_login_run",
            "acc_test_select": "acc_test_run",
            "acc_cookies_select": "acc_cookies_prompt",
        }
        prefix = prefix_map[data]
        keyboard = [[InlineKeyboardButton(a["email"], callback_data=f"{prefix}::{a['email']}")] for a in accounts]
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="menu_accounts")])
        await _edit_or_reply(update, "اختر الحساب:", InlineKeyboardMarkup(keyboard))
        return

    if data.startswith("acc_cookies_prompt::"):
        email = data.split("::", 1)[1]
        USER_STATES[chat_id] = {"step": "import_cookies", "email": email}
        await _edit_or_reply(
            update,
            f"🍪 أرسل كوكيز الحساب ({email}) بصيغة JSON أو ارفع ملف .json أو /cancel:",
        )
        return

    if data.startswith("acc_edit_menu::"):
        email = data.split("::", 1)[1]
        keyboard = [
            [InlineKeyboardButton("🔑 تعديل App Password", callback_data=f"edit_field::{email}::app_password")],
            [InlineKeyboardButton("🔒 تعديل كلمة المرور", callback_data=f"edit_field::{email}::password")],
            [InlineKeyboardButton("🪪 تعديل الاسم", callback_data=f"edit_field::{email}::fullname")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="menu_accounts")],
        ]
        await _edit_or_reply(
            update,
            f"✏️ تعديل الحساب: {email}",
            InlineKeyboardMarkup(keyboard),
        )
        return

    if data.startswith("edit_field::"):
        _, email, field = data.split("::", 2)
        USER_STATES[chat_id] = {"step": "edit_account_field", "email": email, "field": field}
        prompts = {
            "app_password": "أرسل App Password الجديد (16 حرفاً) أو /clear:",
            "password": "أرسل كلمة المرور الجديدة أو /clear:",
            "fullname": "أرسل الاسم الأول واللقب (مثال: Bilal Amran):",
        }
        await _edit_or_reply(update, prompts.get(field, "أرسل القيمة الجديدة:"))
        return

    if data.startswith("acc_del_confirm::"):
        email = data.split("::", 1)[1]
        core.remove_account(email)
        await _edit_or_reply(
            update,
            f"🗑️ تم حذف الحساب {email}.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_accounts")]]),
        )
        return

    if data.startswith("acc_login_run::"):
        email = data.split("::", 1)[1]
        account = core.get_account(email)
        if not account:
            await _edit_or_reply(update, "❌ الحساب غير موجود.")
            return
        await _edit_or_reply(update, f"🔐 جاري تسجيل الدخول للحساب {email}...")
        asyncio.create_task(execute_login_only(context.bot, chat_id, account))
        return

    if data.startswith("acc_test_run::"):
        email = data.split("::", 1)[1]
        account = core.get_account(email)
        if not account:
            await _edit_or_reply(update, "❌ الحساب غير موجود.")
            return
        await _edit_or_reply(update, "⏳ جاري فحص الاتصال...")
        if account.get("app_password"):
            ok, info = await asyncio.to_thread(core.test_smtp, account)
            result = "متصل بنجاح ✅" if ok else f"فشل الاتصال ❌ ({info})"
        else:
            result = "غير مضبوط (لا يوجد App Password)"
        await _edit_or_reply(
            update,
            f"🧪 نتيجة الفحص ({email}):\n• البريد المباشر: {result}",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_accounts")]]),
        )
        return

    if data == "menu_settings":
        settings = core.load_settings()
        method_label = METHOD_LABELS.get(settings.get("send_method", "auto"), "تلقائي ذكي")
        interval_hours = core.send_interval_seconds(settings) / 3600
        keyboard = [
            [InlineKeyboardButton(f"📤 طريقة الإرسال: {method_label}", callback_data="cycle_method")],
            [InlineKeyboardButton("📝 تعديل نص البلاغ", callback_data="edit_template")],
            [InlineKeyboardButton(f"⏱️ الفاصل بين كل إرسال: {interval_hours:g} ساعة", callback_data="set_interval")],
            [InlineKeyboardButton("🧹 تفريغ طابور الإرسال", callback_data="clear_queue")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="back_main")],
        ]
        await _edit_or_reply(update, "⚙️ الإعدادات", InlineKeyboardMarkup(keyboard))
        return

    if data == "clear_queue":
        core.clear_queue()
        await _edit_or_reply(
            update,
            "🧹 تم تفريغ طابور الإرسال.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_settings")]]),
        )
        return

    if data == "cycle_method":
        settings = core.load_settings()
        modes = ["auto", "web_form", "smtp", "gmail_web"]
        current = settings.get("send_method", "auto")
        if current not in modes:
            current = "auto"
        settings["send_method"] = modes[(modes.index(current) + 1) % len(modes)]
        core.save_settings(settings)
        await _edit_or_reply(
            update,
            f"✅ تم التغيير إلى: {METHOD_LABELS[settings['send_method']]}",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_settings")]]),
        )
        return

    if data == "edit_template":
        USER_STATES[chat_id] = {"step": "edit_template"}
        await _edit_or_reply(update, "📝 أرسل نص البلاغ الأساسي الجديد (يبدأ بـ Subject: ...):")
        return

    if data == "menu_stats":
        stats = core.load_stats()
        lines = [
            "📊 إحصائيات الإرسال",
            f"• ناجح: {stats.get('total_sent', 0)}",
            f"• فاشل: {stats.get('failed', 0)}",
            "\nآخر العمليات:",
        ]
        history = stats.get("history", [])[-10:]
        if not history:
            lines.append("لا توجد عمليات بعد.")
        for item in reversed(history):
            icon = "✅" if item.get("success") else "❌"
            lines.append(f"{icon} {item.get('email', '?')} — {item.get('channel', '?')}")
        await _edit_or_reply(
            update,
            "\n".join(lines),
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="back_main")]]),
        )
        return

    await _edit_or_reply(
        update,
        "⚠️ خيار غير معروف. استخدم /start للعودة للقائمة.",
        InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="back_main")]]),
    )


@restricted
async def document_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = USER_STATES.get(chat_id)
    if not state or state.get("step") != "import_cookies":
        return
    document = update.message.document
    if not document:
        return
    try:
        telegram_file = await context.bot.get_file(document.file_id)
        raw_bytes = await telegram_file.download_as_bytearray()
        raw_text = raw_bytes.decode("utf-8", errors="replace")
        ok, message = core.import_cookies_for_account(state["email"], raw_text)
        USER_STATES.pop(chat_id, None)
        await update.message.reply_text(
            f"{'✅' if ok else '❌'} {message}",
            reply_markup=build_main_menu(),
        )
    except Exception as exc:  # noqa: BLE001
        await update.message.reply_text(f"❌ تعذر قراءة الملف: {core._format_exc(exc)}")


@restricted
async def text_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()

    if text.lower() == "/cancel":
        await cmd_cancel(update, context)
        return

    for email, challenge in list(ACTIVE_CHALLENGES.items()):
        if challenge["chat_id"] == chat_id and not challenge["future"].done():
            challenge["future"].set_result(text)
            await update.message.reply_text("⏳ تم الاستلام، جاري المتابعة...")
            return

    state = USER_STATES.get(chat_id)
    if not state:
        await update.message.reply_text("أرسل /start لفتح القائمة الرئيسية.")
        return

    step = state["step"]

    if step == "import_cookies":
        ok, message = core.import_cookies_for_account(state["email"], text)
        USER_STATES.pop(chat_id, None)
        await update.message.reply_text(
            f"{'✅' if ok else '❌'} {message}",
            reply_markup=build_main_menu(),
        )
        return

    if step == "add_email":
        if not core.is_valid_email(text):
            await update.message.reply_text("❌ بريد إلكتروني غير صالح، حاول مجدداً:")
            return
        state["email"] = text
        auth_type = state.get("auth_type", "app_password")
        if auth_type == "app_password":
            state["step"] = "add_app_password"
            await update.message.reply_text("🔑 أرسل App Password (16 حرفاً) أو - لتخطي:")
        else:
            state["step"] = "add_password"
            await update.message.reply_text("🔒 أرسل كلمة مرور الحساب أو - لتخطي:")
        return

    if step == "add_app_password":
        app_password = "" if text == "-" else text
        added = core.add_account(
            email=state["email"],
            app_password=app_password,
            auth_type="app_password",
        )
        USER_STATES.pop(chat_id, None)
        await update.message.reply_text(
            f"✅ تم حفظ الحساب {state['email']} بنجاح." if added else "❌ الحساب مضاف مسبقاً أو البريد غير صالح.",
            reply_markup=build_main_menu(),
        )
        return

    if step == "add_password":
        password = "" if text == "-" else text
        added = core.add_account(
            email=state["email"],
            password=password,
            auth_type="email_code",
        )
        USER_STATES.pop(chat_id, None)
        account = core.get_account(state["email"])
        if not added or not account:
            await update.message.reply_text(
                "❌ الحساب مضاف مسبقاً أو البريد غير صالح.",
                reply_markup=build_main_menu(),
            )
            return
        await update.message.reply_text(
            f"✅ تم حفظ الحساب {account['email']}.",
            reply_markup=build_main_menu(),
        )
        if core.load_settings().get("auto_login_on_add", True):
            asyncio.create_task(execute_login_only(context.bot, chat_id, account))
        return

    if step == "edit_account_field":
        email = state["email"]
        field = state["field"]
        if field == "fullname":
            parts = text.split(None, 1)
            core.update_account(email, first_name=parts[0], last_name=parts[1] if len(parts) > 1 else "")
        else:
            core.update_account(email, **{field: "" if text.lower() == "/clear" else text})
        USER_STATES.pop(chat_id, None)
        await update.message.reply_text(
            "✅ تم تحديث بيانات الحساب.",
            reply_markup=build_main_menu(),
        )
        return

    if step == "set_interval":
        try:
            hours = float(text.replace(",", "."))
            if hours < core.MIN_SEND_INTERVAL_HOURS:
                await update.message.reply_text(
                    f"❌ الحد الأدنى هو {core.MIN_SEND_INTERVAL_HOURS} ساعات. أرسل رقماً أكبر أو /cancel:"
                )
                return
            settings = core.load_settings()
            settings["send_interval_hours"] = hours
            core.save_settings(settings)
            USER_STATES.pop(chat_id, None)
            await update.message.reply_text(
                f"✅ تم ضبط الفاصل بين كل إرسال إلى {hours:g} ساعة.",
                reply_markup=build_main_menu(),
            )
        except ValueError:
            await update.message.reply_text("❌ يرجى إدخال رقم صحيح بالساعات (مثال: 6).")
        return

    if step == "edit_template":
        settings = core.load_settings()
        settings["base_template"] = text
        core.save_settings(settings)
        USER_STATES.pop(chat_id, None)
        await update.message.reply_text("✅ تم تحديث نص البلاغ.", reply_markup=build_main_menu())
        return


async def scheduler_job(context: ContextTypes.DEFAULT_TYPE):
    """Send at most one report per interval, cycling through all accounts."""
    if BATCH_LOCK.locked():
        return
    settings = core.load_settings()
    state = core.load_scheduler_state()
    if not settings.get("auto_send_enabled") and not state.get("queue"):
        return
    if core.seconds_until_next_send(settings) > 0:
        return

    accounts = core.load_accounts()
    if not accounts:
        return
    queued_email = (state.get("queue") or [None])[0]
    account = core.pick_next_account(accounts)
    if not account:
        return

    async with BATCH_LOCK:
        ok, _ = await run_one_send(context.bot, None, account)
        if not ok and queued_email and core.get_account(queued_email):
            core.enqueue_accounts([queued_email])


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log unexpected errors instead of letting the polling loop die noisily."""
    logging.error("Unhandled exception while processing an update", exc_info=context.error)
    try:
        chat = getattr(update, "effective_chat", None)
        if chat is not None:
            await context.bot.send_message(
                chat.id,
                "⚠️ حدث خطأ غير متوقع أثناء تنفيذ العملية. أرسل /start للمحاولة مجدداً.",
            )
    except Exception:  # noqa: BLE001
        pass


def build_application(token: str) -> Application:
    """Create the Application and register every handler (also used by tests)."""
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("selftest", cmd_selftest))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler(filters.Document.ALL, document_input_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_input_handler))
    app.add_error_handler(error_handler)
    if app.job_queue:
        app.job_queue.run_repeating(
            scheduler_job, interval=SCHEDULER_TICK_SECONDS, first=5, name="scheduler_job"
        )
    return app


def main():
    token = (
        os.getenv("TELEGRAM_BOT_TOKEN")
        or os.getenv("BOT_TOKEN")
        or os.getenv("TOKEN")
        or DEFAULT_BOT_TOKEN
    ).strip()
    if not token:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is not set. Add it in Railway → Variables "
            "(or your local .env) before starting the bot."
        )
    if not ALLOWED_USER_IDS:
        logging.warning("ALLOWED_USER_IDS is not set: anyone who finds the bot can control it.")

    app = build_application(token)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
