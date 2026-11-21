import os
import json
import time
import random
import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
import core

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    level=logging.INFO,
)

USER_STATES = {}
ACTIVE_CHALLENGES = {}
LAST_AUTO_RUN = {"ts": 0}


def build_main_menu():
    s = core.load_settings()
    auto_icon = "🟢" if s.get("auto_send_enabled") else "🔴"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🚀 Instant Report", callback_data="menu_send_now"),
            InlineKeyboardButton("🌐 Send via Support Form", callback_data="send_form_now"),
        ],
        [
            InlineKeyboardButton("👤 Accounts Manager", callback_data="menu_accounts"),
            InlineKeyboardButton("📱 Device & IP Identity", callback_data="menu_device"),
        ],
        [
            InlineKeyboardButton(f"{auto_icon} Auto Send", callback_data="toggle_auto"),
            InlineKeyboardButton("⏱️ Send Interval", callback_data="set_interval"),
        ],
        [
            InlineKeyboardButton("⚙️ Settings & Template", callback_data="menu_settings"),
            InlineKeyboardButton("📊 Logs & Stats", callback_data="menu_stats"),
        ],
        [
            InlineKeyboardButton("🔄 Refresh Dashboard", callback_data="refresh_main"),
        ],
    ])


def render_dashboard():
    accounts = core.load_accounts()
    s = core.load_settings()
    st = core.load_stats()
    active_sessions = sum(1 for a in accounts if a.get("logged_in"))
    method_labels = {
        "web_form": "Google Play GetHelp Form",
        "smtp": "App Password (SMTP)",
        "gmail_web": "Gmail Web",
        "auto": "Smart Auto",
    }
    return (
        "🛡️ Control Panel — Google Play Support\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"• Accounts: {len(accounts)} (Saved sessions: {active_sessions})\n"
        f"• Auto Login on Add: {'Enabled ✅' if s.get('auto_login_on_add', True) else 'Disabled ⏸️'}\n"
        f"• Auto Send: {'Enabled ✅' if s.get('auto_send_enabled') else 'Disabled ⏸️'}\n"
        f"• Interval: Every {s.get('auto_send_interval', 60)} min\n"
        f"• Channel: {method_labels.get(s.get('send_method', 'web_form'), 'auto')}\n"
        f"• Stats: Success {st.get('total_sent', 0)} | Failed {st.get('failed', 0)}\n"
        "━━━━━━━━━━━━━━━━━━━━"
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
        "✅ Current action cancelled.",
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
        [InlineKeyboardButton("✅ Approved on Device", callback_data=f"ch_done::{email}")],
        [InlineKeyboardButton("❌ Cancel Session", callback_data=f"ch_cancel::{email}")],
    ])
    caption_str = f"🔐 Verification Required: {email}\n\n{prompt_text}"
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
        await _safe_ack_challenge(q, "✅ Completing session...")
        return

    if data.startswith("ch_cancel::"):
        email = data.split("::", 1)[1]
        ch = ACTIVE_CHALLENGES.get(email)
        if ch and not ch["future"].done():
            ch["future"].set_result("/cancel")
        await _safe_ack_challenge(q, "❌ Session cancelled.")
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
        await q.edit_message_text(
            "⏱️ Send auto-send interval in minutes (e.g., 30 or 60), or /cancel:"
        )
        return

    if data == "menu_send_now":
        accounts = core.load_accounts()
        if not accounts:
            await q.edit_message_text(
                "❌ No accounts found.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_main")]]),
            )
            return
        kb = [[InlineKeyboardButton(f"📨 {a['email']}", callback_data=f"run_send::{a['email']}")] for a in accounts]
        kb.append([InlineKeyboardButton("🎲 Random Account", callback_data="run_send::RANDOM")])
        kb.append([InlineKeyboardButton("🔙 Back", callback_data="back_main")])
        await q.edit_message_text("Select account for instant report:", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data == "send_form_now":
        accounts = core.load_accounts()
        if not accounts:
            await q.edit_message_text(
                "❌ Add an account first.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_main")]]),
            )
            return
        acc = random.choice(accounts)
        await q.edit_message_text(f"⏳ Opening Google Play GetHelp form for {acc['email']}...")
        asyncio.create_task(execute_report_task(context.bot, chat_id, acc, force_method="web_form"))
        return

    if data.startswith("run_send::"):
        target = data.split("::", 1)[1]
        accounts = core.load_accounts()
        if not accounts:
            await q.edit_message_text("❌ No accounts found.")
            return
        acc = random.choice(accounts) if target == "RANDOM" else core.get_account(target)
        if not acc:
            await q.edit_message_text("❌ Account not found.")
            return
        await q.edit_message_text(f"⏳ Generating AI appeal and sending from {acc['email']}...")
        asyncio.create_task(execute_report_task(context.bot, chat_id, acc))
        return

    if data == "menu_accounts":
        accounts = core.load_accounts()
        kb = [
            [
                InlineKeyboardButton("➕ Add Account", callback_data="acc_add_choose_mode"),
                InlineKeyboardButton("📋 List Accounts", callback_data="acc_list"),
            ],
            [
                InlineKeyboardButton("✏️ Edit Account", callback_data="acc_edit_select"),
                InlineKeyboardButton("🗑️ Delete Account", callback_data="acc_del_select"),
            ],
            [
                InlineKeyboardButton("🔐 Login & Save Session", callback_data="acc_login_select"),
                InlineKeyboardButton("🧪 Test SMTP", callback_data="acc_test_select"),
            ],
            [
                InlineKeyboardButton("💾 Export JSON", callback_data="acc_export"),
                InlineKeyboardButton("🔙 Back", callback_data="back_main"),
            ],
        ]
        await q.edit_message_text(
            f"👤 Accounts Manager ({len(accounts)})",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if data == "acc_add_choose_mode":
        kb = [
            [InlineKeyboardButton("📧 Email + Password (Auto Login)", callback_data="acc_add_mode::email_code")],
            [InlineKeyboardButton("🔑 Email + App Password (SMTP)", callback_data="acc_add_mode::app_password")],
            [InlineKeyboardButton("🔙 Back", callback_data="menu_accounts")],
        ]
        await q.edit_message_text(
            "➕ Choose Login / Add Method:",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if data.startswith("acc_add_mode::"):
        mode = data.split("::", 1)[1]
        USER_STATES[chat_id] = {"step": "add_email", "auth_type": mode}
        label = "Email + Password (Auto Login)" if mode == "email_code" else "Email + App Password (SMTP)"
        await q.edit_message_text(
            f"📧 Method: {label}\nSend Gmail address (Name & Surname are extracted automatically):"
        )
        return

    if data == "menu_device":
        accounts = core.load_accounts()
        lines = ["📱 Device & IP Identities"]
        for a in accounts:
            lock = "🔒 Locked" if a.get("logged_in") else "🔄 Hourly Rotation"
            lines.append(
                f"• {a['email']} [{lock}]\n"
                f"  Phone: {a.get('device_name') or 'Pending'} | ID: {a.get('device_id') or 'Pending'}\n"
                f"  IP: {a.get('ip_address') or 'Auto'} | TZ: {a.get('timezone') or 'Africa/Algiers'}"
            )
        if len(lines) == 1:
            lines.append("No accounts configured yet.")
        kb = [
            [InlineKeyboardButton("🎲 Rotate Unlogged Devices Now", callback_data="rotate_devices_now")],
            [InlineKeyboardButton("✏️ Set Device / IP for Account", callback_data="set_dev_ip_select")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_main")],
        ]
        await q.edit_message_text(
            "\n".join(lines)[:3900],
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if data == "set_dev_ip_select":
        accounts = core.load_accounts()
        if not accounts:
            await q.edit_message_text("No accounts found.")
            return
        kb = [[InlineKeyboardButton(a["email"], callback_data=f"set_dev_ip_acc::{a['email']}")] for a in accounts]
        kb.append([InlineKeyboardButton("🔙 Back", callback_data="menu_device")])
        await q.edit_message_text("Select account to set Phone Name, Device ID & IP:", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data.startswith("set_dev_ip_acc::"):
        email = data.split("::", 1)[1]
        USER_STATES[chat_id] = {"step": "set_dev_ip_val", "email": email}
        await q.edit_message_text(
            f"📱 Send for {email} in format:\nPhone Name | Device ID | IP Address\nExample:\nXiaomi Redmi Note 12 Pro 5G | 22101316I-001 | 105.104.22.14"
        )
        return

    if data == "rotate_devices_now":
        accounts = core.load_accounts()
        rotated = 0
        for a in accounts:
            if not a.get("logged_in"):
                await core.ensure_account_session_identity(a, force_rotate=True)
                rotated += 1
        await q.edit_message_text(
            f"✅ Rotated device & IP identity for {rotated} unlogged accounts.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_device")]]),
        )
        return

    if data == "acc_list":
        accounts = core.load_accounts()
        if not accounts:
            await q.edit_message_text(
                "No accounts saved.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_accounts")]]),
            )
            return
        blocks = []
        for a in accounts:
            sess = "Saved 🔒" if a.get("logged_in") else "Not logged in"
            mode_txt = "Email+Pass" if a.get("auth_type", "email_code") == "email_code" else "App Password"
            blocks.append(
                f"• {a['email']} — {a.get('first_name', '')} {a.get('last_name', '')} [{mode_txt}]\n"
                f"  Session: {sess} | Status: {a.get('last_status', 'New')}\n"
                f"  Phone: {a.get('device_name') or 'Auto'} ({a.get('device_id') or 'Auto'})\n"
                f"  IP: {a.get('ip_address') or 'Auto'} | Sent: {a.get('sent', 0)}"
            )
        text = "\n\n".join(blocks)
        if len(text) > 3900:
            text = text[:3900] + "\n..."
        await q.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_accounts")]]),
        )
        return

    if data in ("acc_edit_select", "acc_del_select", "acc_login_select", "acc_test_select"):
        accounts = core.load_accounts()
        if not accounts:
            await q.edit_message_text(
                "No accounts found.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_accounts")]]),
            )
            return
        prefix_map = {
            "acc_edit_select": "acc_edit_menu",
            "acc_del_select": "acc_del_confirm",
            "acc_login_select": "acc_login_run",
            "acc_test_select": "acc_test_run",
        }
        prefix = prefix_map[data]
        kb = [[InlineKeyboardButton(a["email"], callback_data=f"{prefix}::{a['email']}")] for a in accounts]
        kb.append([InlineKeyboardButton("🔙 Back", callback_data="menu_accounts")])
        await q.edit_message_text("Select account:", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data.startswith("acc_edit_menu::"):
        email = data.split("::", 1)[1]
        kb = [
            [InlineKeyboardButton("🔑 Edit App Password", callback_data=f"edit_field::{email}::app_password")],
            [InlineKeyboardButton("🔒 Edit Password", callback_data=f"edit_field::{email}::password")],
            [InlineKeyboardButton("🪪 Edit First & Last Name", callback_data=f"edit_field::{email}::fullname")],
            [InlineKeyboardButton("🔓 Reset Session (Allow Device Rotation)", callback_data=f"reset_sess::{email}")],
            [InlineKeyboardButton("🔙 Back", callback_data="menu_accounts")],
        ]
        await q.edit_message_text(
            f"✏️ Edit Account: {email}",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if data.startswith("edit_field::"):
        _, email, field = data.split("::", 2)
        USER_STATES[chat_id] = {"step": "edit_account_field", "email": email, "field": field}
        prompts = {
            "app_password": "Send new App Password (16 chars) or /clear:",
            "password": "Send new Account Password or /clear:",
            "fullname": "Send First and Last Name separated by space (e.g., Bilal Amran):",
        }
        await q.edit_message_text(prompts[field])
        return

    if data.startswith("reset_sess::"):
        email = data.split("::", 1)[1]
        core.update_account(email, logged_in=False, identity_updated_at=0, last_status="Session reset")
        await q.edit_message_text(
            f"✅ Session reset for {email}. Device identity will rotate on next login.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_accounts")]]),
        )
        return

    if data.startswith("acc_del_confirm::"):
        email = data.split("::", 1)[1]
        core.remove_account(email)
        await q.edit_message_text(
            f"🗑️ Deleted account {email}.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_accounts")]]),
        )
        return

    if data.startswith("acc_login_run::"):
        email = data.split("::", 1)[1]
        acc = core.get_account(email)
        if not acc:
            await q.edit_message_text("❌ Account not found.")
            return
        await q.edit_message_text(
            f"🔐 Starting login session for {email}...\n"
            "Verification code / 2FA prompts or screenshots will appear here."
        )
        asyncio.create_task(execute_login_only(context.bot, chat_id, acc))
        return

    if data.startswith("acc_test_run::"):
        email = data.split("::", 1)[1]
        acc = core.get_account(email)
        if not acc:
            await q.edit_message_text("❌ Account not found.")
            return
        await q.edit_message_text("⏳ Testing SMTP connection...")
        if acc.get("app_password"):
            ok, info = core.test_smtp(acc)
            res = f"App Password SMTP: {'OK ✅' if ok else 'Failed ❌ (' + info + ')'}"
        else:
            res = "App Password SMTP: Not set"
        await q.edit_message_text(
            f"🧪 Test Result: {email}\n• {res}\n• Session: {'Saved 🔒' if acc.get('logged_in') else 'Not logged in'}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_accounts")]]),
        )
        return

    if data == "acc_export":
        payload = json.dumps(core.load_accounts(), ensure_ascii=False, indent=2)
        if len(payload) > 3800:
            payload = payload[:3800] + "\n..."
        await q.edit_message_text(
            payload,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_accounts")]]),
        )
        return

    if data == "menu_settings":
        s = core.load_settings()
        auto_login_txt = "ON ✅" if s.get("auto_login_on_add", True) else "OFF ❌"
        kb = [
            [InlineKeyboardButton(f"📤 Method: {s.get('send_method', 'web_form')}", callback_data="cycle_method")],
            [InlineKeyboardButton(f"⚡ Auto Login on Add: {auto_login_txt}", callback_data="toggle_auto_login")],
            [InlineKeyboardButton("📝 Edit Base Appeal Template", callback_data="edit_template")],
            [InlineKeyboardButton("📧 Edit Target Email", callback_data="edit_target")],
            [InlineKeyboardButton(f"⏳ Delay: {s.get('min_delay', 25)}-{s.get('max_delay', 90)}s", callback_data="edit_delays")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_main")],
        ]
        await q.edit_message_text(
            "⚙️ Settings & Template",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if data == "toggle_auto_login":
        s = core.load_settings()
        s["auto_login_on_add"] = not s.get("auto_login_on_add", True)
        core.save_settings(s)
        await q.edit_message_text(
            f"✅ Auto Login on Add set to: {'Enabled' if s['auto_login_on_add'] else 'Disabled'}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_settings")]]),
        )
        return

    if data == "cycle_method":
        s = core.load_settings()
        modes = ["web_form", "smtp", "gmail_web", "auto"]
        cur = s.get("send_method", "web_form")
        s["send_method"] = modes[(modes.index(cur) + 1) % len(modes)]
        core.save_settings(s)
        await q.edit_message_text(
            f"✅ Send method updated to: {s['send_method']}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_settings")]]),
        )
        return

    if data == "edit_template":
        USER_STATES[chat_id] = {"step": "edit_template"}
        await q.edit_message_text("📝 Send new base appeal template text:")
        return

    if data == "edit_target":
        USER_STATES[chat_id] = {"step": "edit_target"}
        await q.edit_message_text("📧 Send target email address:")
        return

    if data == "edit_delays":
        USER_STATES[chat_id] = {"step": "edit_delays"}
        await q.edit_message_text("⏳ Send min and max delay in seconds (e.g., 20 60):")
        return

    if data == "menu_stats":
        st = core.load_stats()
        lines = [
            "📊 Statistics",
            f"• Success: {st.get('total_sent', 0)}",
            f"• Failed: {st.get('failed', 0)}",
            "\nLast 10 runs:",
        ]
        for h in reversed(st.get("history", [])[-10:]):
            icon = "✅" if h["success"] else "❌"
            lines.append(f"{icon} {h['email']} — [{h.get('channel', 'auto')}]")
        await q.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_main")]]),
        )
        return


async def text_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()

    if text.lower() == "/cancel":
        await cmd_cancel(update, context)
        return

    for email, ch in list(ACTIVE_CHALLENGES.items()):
        if ch["chat_id"] == chat_id and not ch["future"].done():
            ch["future"].set_result(text)
            await update.message.reply_text("⏳ Received input, continuing session...")
            return

    state = USER_STATES.get(chat_id)
    if not state:
        await update.message.reply_text("Use /start to open control panel.")
        return

    step = state["step"]

    if step == "add_email":
        if "@" not in text or "." not in text:
            await update.message.reply_text("❌ Invalid email. Try again:")
            return
        state["email"] = text
        auth_type = state.get("auth_type", "email_code")
        if auth_type == "app_password":
            state["step"] = "add_app_password"
            await update.message.reply_text("🔑 Send App Password (16 chars):")
        else:
            state["step"] = "add_password"
            await update.message.reply_text(
                "🔒 Send Account Password (login starts immediately after saving):"
            )
        return

    if step == "add_app_password":
        app_pw = "" if text == "-" else text
        added = core.add_account(
            email=state["email"],
            app_password=app_pw,
            auth_type="app_password",
        )
        USER_STATES.pop(chat_id, None)
        acc = core.get_account(state["email"])
        name_str = f"{acc['first_name']} {acc['last_name']}" if acc else ""
        await update.message.reply_text(
            f"✅ Saved account {state['email']} (Name: {name_str}) via App Password."
            if added else "❌ Account already exists.",
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
            await update.message.reply_text("❌ Account already exists.", reply_markup=build_main_menu())
            return

        await update.message.reply_text(
            f"✅ Saved {acc['email']}\n"
            f"📱 Phone: {acc.get('device_name')} | ID: {acc.get('device_id')}\n"
            f"🌐 IP: {acc.get('ip_address')}\n"
            f"🔐 Starting direct login now...",
            reply_markup=build_main_menu(),
        )
        s = core.load_settings()
        if s.get("auto_login_on_add", True):
            asyncio.create_task(execute_login_only(context.bot, chat_id, acc))
        return

    if step == "set_dev_ip_val":
        email = state["email"]
        parts = [p.strip() for p in text.split("|")]
        updates = {}
        if len(parts) >= 1 and parts[0]:
            updates["device_name"] = parts[0]
        if len(parts) >= 2 and parts[1]:
            updates["device_id"] = parts[1]
        if len(parts) >= 3 and parts[2]:
            updates["ip_address"] = parts[2]
        if updates:
            core.update_account(email, **updates)
        USER_STATES.pop(chat_id, None)
        await update.message.reply_text(
            f"✅ Updated device/IP identity for {email}.",
            reply_markup=build_main_menu(),
        )
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
            f"✅ Updated {email}.",
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
                f"✅ Interval set to {mins} minutes.",
                reply_markup=build_main_menu(),
            )
        except ValueError:
            await update.message.reply_text("❌ Enter a valid integer.")
        return

    if step == "edit_template":
        s = core.load_settings()
        s["base_template"] = text
        core.save_settings(s)
        USER_STATES.pop(chat_id, None)
        await update.message.reply_text("✅ Base template updated.", reply_markup=build_main_menu())
        return

    if step == "edit_target":
        s = core.load_settings()
        s["target_email"] = text
        core.save_settings(s)
        USER_STATES.pop(chat_id, None)
        await update.message.reply_text(f"✅ Target email updated to {text}.", reply_markup=build_main_menu())
        return

    if step == "edit_delays":
        try:
            p1, p2 = map(int, text.split())
            s = core.load_settings()
            s["min_delay"] = min(p1, p2)
            s["max_delay"] = max(p1, p2)
            core.save_settings(s)
            USER_STATES.pop(chat_id, None)
            await update.message.reply_text(f"✅ Delay set to {s['min_delay']}–{s['max_delay']}s.", reply_markup=build_main_menu())
        except Exception:
            await update.message.reply_text("❌ Send two numbers separated by space, e.g.: 25 90")
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
            fn, ln = core.extract_and_save_profile_names(driver, account)
            await bot.send_message(
                chat_id,
                f"✅ Session Saved!\n"
                f"• Account: {account['email']}\n"
                f"• Name: {fn} {ln}\n"
                f"• Phone: {account.get('device_name')} ({account.get('device_id')})\n"
                f"• IP: {account.get('ip_address')}",
            )
        else:
            await bot.send_message(chat_id, f"❌ Login failed ({account['email']}): {msg}")
    except Exception as e:
        await bot.send_message(chat_id, f"❌ Error: {core._format_exc(e)}")
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


async def execute_report_task(bot, chat_id, account, force_method=None):
    s = core.load_settings()
    method = force_method or s.get("send_method", "web_form")
    report = await core.generate_report()

    async def cb(email, ctype, shot, prompt_txt):
        return await send_challenge_to_telegram(bot, chat_id, email, ctype, shot, prompt_txt)

    ok, info = await core.dispatch_report(
        account,
        report["subject"],
        report["body"],
        method=method,
        challenge_callback=cb,
    )
    core.record_stat(account["email"], ok, method, report["subject"])
    if ok:
        core.update_account(account["email"], sent=account.get("sent", 0) + 1, last_status="Sent OK")
        snippet = report["body"][:350] + ("..." if len(report["body"]) > 350 else "")
        await bot.send_message(
            chat_id,
            f"✅ Appeal Submitted!\n"
            f"• Account: {account['email']}\n"
            f"• Method: {method}\n"
            f"• Subject: {report['subject']}\n\n"
            f"📄 Generated Text:\n{snippet}",
        )
    else:
        core.update_account(account["email"], last_status=f"Failed: {info}")
        await bot.send_message(
            chat_id,
            f"❌ Submission Failed ({account['email']})\nReason: {info}",
        )


async def auto_scheduler_job(context: ContextTypes.DEFAULT_TYPE):
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
    acc = random.choice(accounts)
    report = await core.generate_report()
    method = s.get("send_method", "web_form")
    ok, _ = await core.dispatch_report(acc, report["subject"], report["body"], method=method)
    core.record_stat(acc["email"], ok, method, report["subject"])
    if ok:
        core.update_account(acc["email"], sent=acc.get("sent", 0) + 1)

    delay = random.randint(s.get("min_delay", 25), s.get("max_delay", 90))
    await asyncio.sleep(delay)


def main():
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "9204455871:AAGTPgz2d086vZCYlFkrcDCci7DWr6un9oY").strip()
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN missing")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_input_handler))
    app.job_queue.run_repeating(auto_scheduler_job, interval=20, first=10)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
