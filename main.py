import asyncio
import logging
import os
import time
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

DEFAULT_BOT_TOKEN = "9204455871:AAGTPgz2d086vZCYlFkrcDCci7DWr6un9oY"

USER_STATES = {}
ACTIVE_CHALLENGES = {}
LAST_AUTO_RUN = {"ts": 0}
AUTO_BATCH_LOCK = asyncio.Lock()


def build_main_menu():
    s = core.load_settings()
    auto_icon = "🟢" if s.get("auto_send_enabled") else "🔴"
    return InlineKeyboardMarkup([
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
    ])


def render_dashboard():
    accounts = core.load_accounts()
    s = core.load_settings()
    st = core.load_stats()
    method_labels = {
        "auto": "تلقائي ذكي",
        "web_form": "نموذج الدعم",
        "smtp": "البريد المباشر",
        "gmail_web": "ويب Gmail",
    }
    return (
        "🛡️ لوحة التحكم\n"
        "━━━━━━━━━━━━━━\n"
        f"• الحسابات: {len(accounts)}\n"
        f"• الإرسال التلقائي: {'مفعّل ✅' if s.get('auto_send_enabled') else 'متوقف ⏸️'}\n"
        f"• دورة التلقائي: كل {s.get('auto_send_interval', 60)} دقيقة\n"
        f"• المدة بين كل حساب: {s.get('between_accounts_delay', 30)} ثانية\n"
        f"• الطريقة: {method_labels.get(s.get('send_method', 'auto'), 'تلقائي ذكي')}\n"
        f"• تم الإرسال: {st.get('total_sent', 0)} | فشل: {st.get('failed', 0)}\n"
        "━━━━━━━━━━━━━━"
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    USER_STATES.pop(update.effective_chat.id, None)
    await update.message.reply_text(
        render_dashboard(),
        reply_markup=build_main_menu(),
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    USER_STATES.pop(chat_id, None)
    for email, ch in list(ACTIVE_CHALLENGES.items()):
        if ch["chat_id"] == chat_id and not ch["future"].done():
            ch["future"].set_result("/cancel")
    await update.message.reply_text(
        "✅ تم إلغاء العملية.",
        reply_markup=build_main_menu(),
    )


async def send_challenge_to_telegram(bot, chat_id, email, challenge_type, screenshot_path, prompt_text):
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    ACTIVE_CHALLENGES[email] = {
        "future": fut,
        "chat_id": chat_id,
        "type": challenge_type,
    }
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ تم التأكيد", callback_data=f"ch_done::{email}")],
        [InlineKeyboardButton("❌ إلغاء", callback_data=f"ch_cancel::{email}")],
    ])
    caption_str = f"🔐 تحقق مطلوب للحساب: {email}\n\n{prompt_text}"
    sent_photo = False
    if screenshot_path and os.path.exists(screenshot_path):
        try:
            with open(screenshot_path, "rb") as photo:
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=photo,
                    caption=caption_str,
                    reply_markup=kb,
                )
            sent_photo = True
        except Exception as e:
            logging.warning(f"Failed to send challenge photo: {core._format_exc(e)}")
    if not sent_photo:
        await bot.send_message(
            chat_id=chat_id,
            text=caption_str,
            reply_markup=kb,
        )

    try:
        return await asyncio.wait_for(fut, timeout=180)
    except asyncio.TimeoutError:
        return "/cancel"
    finally:
        ACTIVE_CHALLENGES.pop(email, None)


async def _safe_ack_challenge(q, status_text):
    try:
        if q.message and q.message.photo:
            await q.edit_message_caption(status_text)
        else:
            await q.edit_message_text(status_text)
    except Exception:
        pass


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass
    data = q.data or ""
    chat_id = q.message.chat_id

    if data.startswith("ch_done::"):
        email = data.split("::", 1)[1]
        ch = ACTIVE_CHALLENGES.get(email)
        if ch and not ch["future"].done():
            ch["future"].set_result("done")
        await _safe_ack_challenge(q, "✅ جاري المتابعة...")
        return

    if data.startswith("ch_cancel::"):
        email = data.split("::", 1)[1]
        ch = ACTIVE_CHALLENGES.get(email)
        if ch and not ch["future"].done():
            ch["future"].set_result("/cancel")
        await _safe_ack_challenge(q, "❌ تم الإلغاء.")
        return

    if data in ("refresh_main", "back_main"):
        USER_STATES.pop(chat_id, None)
        await q.edit_message_text(
            render_dashboard(),
            reply_markup=build_main_menu(),
        )
        return

    if data == "toggle_auto":
        s = core.load_settings()
        s["auto_send_enabled"] = not s.get("auto_send_enabled", False)
        core.save_settings(s)
        await q.edit_message_text(
            render_dashboard(),
            reply_markup=build_main_menu(),
        )
        return

    if data == "set_interval":
        USER_STATES[chat_id] = {"step": "set_interval"}
        await q.edit_message_text("⏱️ أرسل الفاصل الزمني بين كل دورة إرسال تلقائي بالدقائق (مثال: 60) أو /cancel:")
        return

    if data == "menu_send_now":
        accounts = core.load_accounts()
        if not accounts:
            await q.edit_message_text(
                "❌ لا توجد حسابات مضافة بعد.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="back_main")]]),
            )
            return
        kb = [[InlineKeyboardButton("📨 إرسال بكل الحسابات", callback_data="run_send::ALL")]]
        for a in accounts:
            kb.append([InlineKeyboardButton(f"👤 {a['email']}", callback_data=f"run_send::{a['email']}")])
        kb.append([InlineKeyboardButton("🔙 رجوع", callback_data="back_main")])
        await q.edit_message_text(
            "اختر إرسال البلاغ بكل الحسابات أو تحديد حساب واحد:",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if data.startswith("run_send::"):
        target = data.split("::", 1)[1]
        accounts = core.load_accounts()
        if not accounts:
            await q.edit_message_text("❌ لا توجد حسابات.")
            return

        if target == "ALL":
            delay_sec = max(0, int(core.load_settings().get("between_accounts_delay", 30)))
            await q.edit_message_text(
                f"⏳ جاري بدء الإرسال بكل الحسابات ({len(accounts)} حساب) مع فاصل {delay_sec} ثانية بين كل حساب..."
            )
            asyncio.create_task(execute_all_accounts_task(context.bot, chat_id, accounts, delay_sec))
            return

        acc = core.get_account(target)
        if not acc:
            await q.edit_message_text("❌ الحساب غير موجود.")
            return
        await q.edit_message_text(f"⏳ جاري إرسال البلاغ من الحساب: {acc['email']}...")
        asyncio.create_task(execute_report_task(context.bot, chat_id, acc))
        return

    if data == "menu_accounts":
        accounts = core.load_accounts()
        kb = [
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
        await q.edit_message_text(
            f"👤 إدارة الحسابات ({len(accounts)})",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if data == "acc_add_choose_mode":
        kb = [
            [InlineKeyboardButton("🔑 بريد + App Password (مستحسن)", callback_data="acc_add_mode::app_password")],
            [InlineKeyboardButton("📧 بريد + كلمة المرور", callback_data="acc_add_mode::email_code")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="menu_accounts")],
        ]
        await q.edit_message_text(
            "اختر طريقة إضافة الحساب:",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if data.startswith("acc_add_mode::"):
        mode = data.split("::", 1)[1]
        USER_STATES[chat_id] = {"step": "add_email", "auth_type": mode}
        await q.edit_message_text("📧 أرسل عنوان البريد الإلكتروني (Gmail):")
        return

    if data == "acc_list":
        accounts = core.load_accounts()
        if not accounts:
            await q.edit_message_text(
                "لا توجد حسابات محفوظة.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_accounts")]]),
            )
            return
        blocks = []
        for a in accounts:
            blocks.append(
                f"• {a['email']}\n"
                f"  الحالة: {a.get('last_status', 'جديد')} | المرسل: {a.get('sent', 0)}"
            )
        text = "\n\n".join(blocks)
        if len(text) > 3900:
            text = text[:3900] + "\n..."
        await q.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_accounts")]]),
        )
        return

    if data in ("acc_edit_select", "acc_del_select", "acc_login_select", "acc_test_select", "acc_cookies_select"):
        accounts = core.load_accounts()
        if not accounts:
            await q.edit_message_text(
                "لا توجد حسابات.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_accounts")]]),
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
        kb = [[InlineKeyboardButton(a["email"], callback_data=f"{prefix}::{a['email']}")] for a in accounts]
        kb.append([InlineKeyboardButton("🔙 رجوع", callback_data="menu_accounts")])
        await q.edit_message_text("اختر الحساب:", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data.startswith("acc_cookies_prompt::"):
        email = data.split("::", 1)[1]
        USER_STATES[chat_id] = {"step": "import_cookies", "email": email}
        await q.edit_message_text(
            f"🍪 أرسل كوكيز الحساب ({email}) بصيغة JSON أو ارفع ملف .json أو /cancel:"
        )
        return

    if data.startswith("acc_edit_menu::"):
        email = data.split("::", 1)[1]
        kb = [
            [InlineKeyboardButton("🔑 تعديل App Password", callback_data=f"edit_field::{email}::app_password")],
            [InlineKeyboardButton("🔒 تعديل كلمة المرور", callback_data=f"edit_field::{email}::password")],
            [InlineKeyboardButton("🪪 تعديل الاسم", callback_data=f"edit_field::{email}::fullname")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="menu_accounts")],
        ]
        await q.edit_message_text(
            f"✏️ تعديل الحساب: {email}",
            reply_markup=InlineKeyboardMarkup(kb),
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
        await q.edit_message_text(prompts[field])
        return

    if data.startswith("acc_del_confirm::"):
        email = data.split("::", 1)[1]
        core.remove_account(email)
        await q.edit_message_text(
            f"🗑️ تم حذف الحساب {email}.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_accounts")]]),
        )
        return

    if data.startswith("acc_login_run::"):
        email = data.split("::", 1)[1]
        acc = core.get_account(email)
        if not acc:
            await q.edit_message_text("❌ الحساب غير موجود.")
            return
        await q.edit_message_text(f"🔐 جاري تسجيل الدخول للحساب {email}...")
        asyncio.create_task(execute_login_only(context.bot, chat_id, acc))
        return

    if data.startswith("acc_test_run::"):
        email = data.split("::", 1)[1]
        acc = core.get_account(email)
        if not acc:
            await q.edit_message_text("❌ الحساب غير موجود.")
            return
        await q.edit_message_text("⏳ جاري فحص الاتصال...")
        if acc.get("app_password"):
            ok, info = core.test_smtp(acc)
            res = "متصل بنجاح ✅" if ok else f"فشل الاتصال ❌ ({info})"
        else:
            res = "غير مضبوط"
        await q.edit_message_text(
            f"🧪 نتيجة الفحص ({email}):\n• البريد المباشر: {res}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_accounts")]]),
        )
        return

    if data == "menu_settings":
        s = core.load_settings()
        method_labels = {
            "auto": "تلقائي ذكي",
            "web_form": "نموذج الدعم",
            "smtp": "البريد المباشر",
            "gmail_web": "ويب Gmail",
        }
        cur_label = method_labels.get(s.get("send_method", "auto"), "تلقائي ذكي")
        between_sec = s.get("between_accounts_delay", 30)
        kb = [
            [InlineKeyboardButton(f"📤 طريقة الإرسال: {cur_label}", callback_data="cycle_method")],
            [InlineKeyboardButton("📝 تعديل نص البلاغ", callback_data="edit_template")],
            [InlineKeyboardButton(f"⏳ المدة بين كل إرسال: {between_sec} ث", callback_data="edit_between_delay")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="back_main")],
        ]
        await q.edit_message_text(
            "⚙️ الإعدادات",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if data == "cycle_method":
        s = core.load_settings()
        modes = ["auto", "web_form", "smtp", "gmail_web"]
        cur = s.get("send_method", "auto")
        if cur not in modes:
            cur = "auto"
        s["send_method"] = modes[(modes.index(cur) + 1) % len(modes)]
        core.save_settings(s)
        method_labels = {
            "auto": "تلقائي ذكي",
            "web_form": "نموذج الدعم",
            "smtp": "البريد المباشر",
            "gmail_web": "ويب Gmail",
        }
        await q.edit_message_text(
            f"✅ تم التغيير إلى: {method_labels[s['send_method']]}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_settings")]]),
        )
        return

    if data == "edit_template":
        USER_STATES[chat_id] = {"step": "edit_template"}
        await q.edit_message_text("📝 أرسل نص البلاغ الأساسي الجديد:")
        return

    if data == "edit_between_delay":
        USER_STATES[chat_id] = {"step": "edit_between_delay"}
        await q.edit_message_text("⏳ أرسل المدة بين كل إرسال بالثواني (مثال: 30 أو 0 لعدم الانتظار):")
        return

    if data == "menu_stats":
        st = core.load_stats()
        lines = [
            "📊 إحصائيات الإرسال",
            f"• ناجح: {st.get('total_sent', 0)}",
            f"• فاشل: {st.get('failed', 0)}",
            "\nآخر العمليات:",
        ]
        for h in reversed(st.get("history", [])[-10:]):
            icon = "✅" if h["success"] else "❌"
            lines.append(f"{icon} {h['email']}")
        await q.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="back_main")]]),
        )
        return


async def document_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = USER_STATES.get(chat_id)
    if not state or state.get("step") != "import_cookies":
        return
    doc = update.message.document
    if not doc:
        return
    try:
        tg_file = await context.bot.get_file(doc.file_id)
        raw_bytes = await tg_file.download_as_bytearray()
        raw_text = raw_bytes.decode("utf-8", errors="replace")
        ok, msg = core.import_cookies_for_account(state["email"], raw_text)
        USER_STATES.pop(chat_id, None)
        await update.message.reply_text(
            f"{'✅' if ok else '❌'} {msg}",
            reply_markup=build_main_menu(),
        )
    except Exception as e:
        await update.message.reply_text(f"❌ تعذر قراءة الملف: {core._format_exc(e)}")


async def text_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()

    if text.lower() == "/cancel":
        await cmd_cancel(update, context)
        return

    for email, ch in list(ACTIVE_CHALLENGES.items()):
        if ch["chat_id"] == chat_id and not ch["future"].done():
            ch["future"].set_result(text)
            await update.message.reply_text("⏳ تم الاستلام، جاري المتابعة...")
            return

    state = USER_STATES.get(chat_id)
    if not state:
        await update.message.reply_text("أرسل /start لفتح القائمة الرئيسية.")
        return

    step = state["step"]

    if step == "import_cookies":
        ok, msg = core.import_cookies_for_account(state["email"], text)
        USER_STATES.pop(chat_id, None)
        await update.message.reply_text(
            f"{'✅' if ok else '❌'} {msg}",
            reply_markup=build_main_menu(),
        )
        return

    if step == "add_email":
        if "@" not in text or "." not in text:
            await update.message.reply_text("❌ بريد إلكتروني غير صالح، حاول مجدداً:")
            return
        state["email"] = text
        auth_type = state.get("auth_type", "app_password")
        if auth_type == "app_password":
            state["step"] = "add_app_password"
            await update.message.reply_text("🔑 أرسل App Password (16 حرفاً):")
        else:
            state["step"] = "add_password"
            await update.message.reply_text("🔒 أرسل كلمة مرور الحساب:")
        return

    if step == "add_app_password":
        app_pw = "" if text == "-" else text
        added = core.add_account(
            email=state["email"],
            app_password=app_pw,
            auth_type="app_password",
        )
        USER_STATES.pop(chat_id, None)
        await update.message.reply_text(
            f"✅ تم حفظ الحساب {state['email']} بنجاح." if added else "❌ الحساب مضاف مسبقاً.",
            reply_markup=build_main_menu(),
        )
        return

    if step == "add_password":
        pw = "" if text == "-" else text
        added = core.add_account(
            email=state["email"],
            password=pw,
            auth_type="email_code",
        )
        USER_STATES.pop(chat_id, None)
        acc = core.get_account(state["email"])
        if not added or not acc:
            await update.message.reply_text("❌ الحساب مضاف مسبقاً.", reply_markup=build_main_menu())
            return

        await update.message.reply_text(
            f"✅ تم حفظ الحساب {acc['email']}.",
            reply_markup=build_main_menu(),
        )
        s = core.load_settings()
        if s.get("auto_login_on_add", True):
            asyncio.create_task(execute_login_only(context.bot, chat_id, acc))
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
            mins = max(1, int(text))
            s = core.load_settings()
            s["auto_send_interval"] = mins
            core.save_settings(s)
            USER_STATES.pop(chat_id, None)
            await update.message.reply_text(
                f"✅ تم ضبط الفاصل الزمني للإرسال التلقائي إلى {mins} دقيقة.",
                reply_markup=build_main_menu(),
            )
        except ValueError:
            await update.message.reply_text("❌ يرجى إدخال رقم صحيح.")
        return

    if step == "edit_template":
        s = core.load_settings()
        s["base_template"] = text
        core.save_settings(s)
        USER_STATES.pop(chat_id, None)
        await update.message.reply_text("✅ تم تحديث نص البلاغ.", reply_markup=build_main_menu())
        return

    if step == "edit_between_delay":
        try:
            parts = text.split()
            if len(parts) >= 2:
                sec = max(0, int((int(parts[0]) + int(parts[1])) / 2))
            else:
                sec = max(0, int(parts[0]))
            s = core.load_settings()
            s["between_accounts_delay"] = sec
            core.save_settings(s)
            USER_STATES.pop(chat_id, None)
            await update.message.reply_text(
                f"✅ تم ضبط المدة بين كل إرسال إلى {sec} ثانية.",
                reply_markup=build_main_menu(),
            )
        except Exception:
            await update.message.reply_text("❌ يرجى إدخال رقم صحيح بالثواني (مثال: 30 أو 0).")
        return


async def execute_login_only(bot, chat_id, account):
    account = await core.ensure_account_session_identity(account)

    async def cb(email, ctype, shot, prompt_txt):
        return await send_challenge_to_telegram(bot, chat_id, email, ctype, shot, prompt_txt)

    driver = None
    try:
        driver = core._build_chrome_driver(account)
        ok, msg = await core._interactive_google_login(driver, account, cb)
        if ok:
            core.extract_and_save_profile_names(driver, account)
            await bot.send_message(chat_id, f"✅ تم تسجيل الدخول بنجاح: {account['email']}")
        else:
            await bot.send_message(chat_id, f"❌ تعذر تسجيل الدخول ({account['email']}): {msg}")
    except Exception as e:
        await bot.send_message(chat_id, f"❌ حدث خطأ: {core._format_exc(e)}")
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


async def execute_report_task(bot, chat_id, account, force_method=None):
    s = core.load_settings()
    method = force_method or s.get("send_method", "auto")
    report = await core.generate_report()

    async def cb(email, ctype, shot, prompt_txt):
        return await send_challenge_to_telegram(bot, chat_id, email, ctype, shot, prompt_txt)

    ok, info, used_channel = await core.dispatch_report(
        account,
        report["subject"],
        report["body"],
        method=method,
        challenge_callback=cb,
    )
    core.record_stat(account["email"], ok, used_channel, report["subject"])
    if ok:
        latest = core.get_account(account["email"]) or account
        core.update_account(account["email"], sent=latest.get("sent", 0) + 1, last_status="تم الإرسال ✅")
        await bot.send_message(
            chat_id,
            f"✅ تم إرسال البلاغ بنجاح\n"
            f"• الحساب: {account['email']}\n"
            f"• العنوان: {report['subject']}",
        )
    else:
        core.update_account(account["email"], last_status="فشل الإرسال ❌")
        await bot.send_message(
            chat_id,
            f"❌ فشل إرسال البلاغ ({account['email']})\nالسبب: {info}",
        )


async def execute_all_accounts_task(bot, chat_id, accounts, delay_sec):
    async with AUTO_BATCH_LOCK:
        success_count = 0
        fail_count = 0
        total = len(accounts)

        for idx, acc in enumerate(accounts):
            s = core.load_settings()
            method = s.get("send_method", "auto")
            report = await core.generate_report()

            async def cb(email, ctype, shot, prompt_txt):
                return await send_challenge_to_telegram(bot, chat_id, email, ctype, shot, prompt_txt)

            ok, info, used_channel = await core.dispatch_report(
                acc,
                report["subject"],
                report["body"],
                method=method,
                challenge_callback=cb,
            )
            core.record_stat(acc["email"], ok, used_channel, report["subject"])
            if ok:
                latest = core.get_account(acc["email"]) or acc
                core.update_account(acc["email"], sent=latest.get("sent", 0) + 1, last_status="تم الإرسال ✅")
                success_count += 1
            else:
                core.update_account(acc["email"], last_status="فشل الإرسال ❌")
                fail_count += 1

            if idx < total - 1 and delay_sec > 0:
                await asyncio.sleep(delay_sec)

        if chat_id is not None:
            await bot.send_message(
                chat_id,
                f"📊 اكتمل الإرسال بكل الحسابات\n"
                f"• ناجح: {success_count}\n"
                f"• فاشل: {fail_count}",
            )


async def auto_scheduler_job(context: ContextTypes.DEFAULT_TYPE):
    if AUTO_BATCH_LOCK.locked():
        return
    s = core.load_settings()
    if not s.get("auto_send_enabled"):
        return
    interval_sec = max(1, int(s.get("auto_send_interval", 60))) * 60
    now = time.time()
    if now - LAST_AUTO_RUN["ts"] < interval_sec:
        return
    accounts = core.load_accounts()
    if not accounts:
        return

    LAST_AUTO_RUN["ts"] = now
    delay_sec = max(0, int(s.get("between_accounts_delay", 30)))
    await execute_all_accounts_task(context.bot, None, accounts, delay_sec)


def main():
    token = (
        os.getenv("TELEGRAM_BOT_TOKEN")
        or os.getenv("BOT_TOKEN")
        or os.getenv("TOKEN")
        or DEFAULT_BOT_TOKEN
    ).strip()

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler(filters.Document.ALL, document_input_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_input_handler))
    app.job_queue.run_repeating(auto_scheduler_job, interval=20, first=10)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
