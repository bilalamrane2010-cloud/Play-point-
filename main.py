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
            InlineKeyboardButton("📱 Device Identity", callback_data="menu_device"),
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
        "🛡️ **Control Panel — Google Play Support**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"• **Accounts:** `{len(accounts)}` (Saved sessions: `{active_sessions}`)\n"
        f"• **Auto Send:** {'Enabled ✅' if s.get('auto_send_enabled') else 'Disabled ⏸️'}\n"
        f"• **Interval:** Every `{s.get('auto_send_interval', 60)}` min\n"
        f"• **Channel:** `{method_labels.get(s.get('send_method', 'web_form'), 'auto')}`\n"
        f"• **Device Rotation:** Every `{s.get('rotate_hours', 1)}` hr (unlogged accounts)\n"
        f"• **Stats:** Success `{st.get('total_sent', 0)}` | Failed `{st.get('failed', 0)}`\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    USER_STATES.pop(update.effective_chat.id, None)
    await update.message.reply_text(
        render_dashboard(),
        reply_markup=build_main_menu(),
        parse_mode="Markdown",
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
    try:
        with open(screenshot_path, "rb") as photo:
            await bot.send_photo(
                chat_id=chat_id,
                photo=photo,
                caption=f"🔐 **Verification Required:** `{email}`\n\n{prompt_text}",
                reply_markup=kb,
                parse_mode="Markdown",
            )
    except Exception:
        await bot.send_message(
            chat_id=chat_id,
            text=f"🔐 **Verification Required:** `{email}`\n\n{prompt_text}",
            reply_markup=kb,
            parse_mode="Markdown",
        )

    try:
        return await asyncio.wait_for(fut, timeout=180)
    except asyncio.TimeoutError:
        return "/cancel"
    finally:
        ACTIVE_CHALLENGES.pop(email, None)


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    chat_id = q.message.chat_id

    if data.startswith("ch_done::"):
        email = data.split("::", 1)[1]
        ch = ACTIVE_CHALLENGES.get(email)
        if ch and not ch["future"].done():
            ch["future"].set_result("done")
        await q.edit_message_caption("✅ Completing session...")
        return

    if data.startswith("ch_cancel::"):
        email = data.split("::", 1)[1]
        ch = ACTIVE_CHALLENGES.get(email)
        if ch and not ch["future"].done():
            ch["future"].set_result("/cancel")
        await q.edit_message_caption("❌ Session cancelled.")
        return

    if data in ("refresh_main", "back_main"):
        USER_STATES.pop(chat_id, None)
        await q.edit_message_text(
            render_dashboard(),
            reply_markup=build_main_menu(),
            parse_mode="Markdown",
        )
        return

    if data == "toggle_auto":
        s = core.load_settings()
        s["auto_send_enabled"] = not s.get("auto_send_enabled", False)
        core.save_settings(s)
        await q.edit_message_text(
            render_dashboard(),
            reply_markup=build_main_menu(),
            parse_mode="Markdown",
        )
        return

    if data == "set_interval":
        USER_STATES[chat_id] = {"step": "set_interval"}
        await q.edit_message_text(
            "⏱️ Send auto-send interval in **minutes** (e.g., `30` or `60`), or `/cancel`:",
            parse_mode="Markdown",
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
        await q.edit_message_text(f"⏳ Opening Google Play GetHelp form for `{acc['email']}`...", parse_mode="Markdown")
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
        await q.edit_message_text(f"⏳ Generating AI appeal and sending from `{acc['email']}`...", parse_mode="Markdown")
        asyncio.create_task(execute_report_task(context.bot, chat_id, acc))
        return

    if data == "menu_accounts":
        accounts = core.load_accounts()
        kb = [
            [
                InlineKeyboardButton("➕ Add Account", callback_data="acc_add"),
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
            f"👤 **Accounts Manager ({len(accounts)})**",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown",
        )
        return

    if data == "menu_device":
        accounts = core.load_accounts()
        lines = ["📱 **Device Identities**"]
        for a in accounts:
            lock = "🔒 Locked" if a.get("logged_in") else "🔄 Hourly Rotation"
            lines.append(
                f"• `{a['email']}` [{lock}]\n"
                f"  Device: `{a.get('device_name') or 'Pending'}` | ID: `{a.get('device_id') or 'Pending'}`"
            )
        if len(lines) == 1:
            lines.append("No accounts configured yet.")
        kb = [
            [InlineKeyboardButton("🎲 Rotate Unlogged Devices Now", callback_data="rotate_devices_now")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_main")],
        ]
        await q.edit_message_text(
            "\n".join(lines)[:3900],
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown",
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
            f"✅ Rotated device identity for `{rotated}` unlogged accounts.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_device")]]),
            parse_mode="Markdown",
        )
        return

    if data == "acc_add":
        USER_STATES[chat_id] = {"step": "add_email"}
        await q.edit_message_text("📧 Send **Gmail address**:", parse_mode="Markdown")
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
            blocks.append(
                f"• `{a['email']}` ({a.get('first_name', '')} {a.get('last_name', '')})\n"
                f"  Session: {sess} | SMTP: {'✓' if a.get('app_password') else '✗'} | Pass: {'✓' if a.get('password') else '✗'}\n"
                f"  Device: `{a.get('device_name') or 'Auto'}` (`{a.get('device_id') or 'Auto'}`)\n"
                f"  Sent: `{a.get('sent', 0)}`"
            )
        text = "\n\n".join(blocks)
        if len(text) > 3900:
            text = text[:3900] + "\n..."
        await q.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_accounts")]]),
            parse_mode="Markdown",
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
            f"✏️ **Edit Account:** `{email}`",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown",
        )
        return

    if data.startswith("edit_field::"):
        _, email, field = data.split("::", 2)
        USER_STATES[chat_id] = {"step": "edit_account_field", "email": email, "field": field}
        prompts = {
            "app_password": "Send new **App Password** (16 chars) or `/clear`:",
            "password": "Send new **Account Password** or `/clear`:",
            "fullname": "Send **First and Last Name** separated by space (e.g., `Bilal Amran`):",
        }
        await q.edit_message_text(prompts[field], parse_mode="Markdown")
        return

    if data.startswith("reset_sess::"):
        email = data.split("::", 1)[1]
        core.update_account(email, logged_in=False, identity_updated_at=0, last_status="Session reset")
        await q.edit_message_text(
            f"✅ Session reset for `{email}`. Device identity will rotate on next login.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_accounts")]]),
            parse_mode="Markdown",
        )
        return

    if data.startswith("acc_del_confirm::"):
        email = data.split("::", 1)[1]
        core.remove_account(email)
        await q.edit_message_text(
            f"🗑️ Deleted account `{email}`.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_accounts")]]),
            parse_mode="Markdown",
        )
        return

    if data.startswith("acc_login_run::"):
        email = data.split("::", 1)[1]
        acc = core.get_account(email)
        if not acc:
            await q.edit_message_text("❌ Account not found.")
            return
        await q.edit_message_text(
            f"🔐 Starting login session for `{email}`...\n"
            "CAPTCHA or 2FA screenshots will appear here.",
            parse_mode="Markdown",
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
            f"🧪 **Test Result:** `{email}`\n• {res}\n• Session: {'Saved 🔒' if acc.get('logged_in') else 'Not logged in'}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_accounts")]]),
            parse_mode="Markdown",
        )
        return

    if data == "acc_export":
        payload = json.dumps(core.load_accounts(), ensure_ascii=False, indent=2)
        if len(payload) > 3800:
            payload = payload[:3800] + "\n..."
        await q.edit_message_text(
            f"```json\n{payload}\n```",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_accounts")]]),
            parse_mode="Markdown",
        )
        return

    if data == "menu_settings":
        s = core.load_settings()
        kb = [
            [InlineKeyboardButton(f"📤 Method: {s.get('send_method', 'web_form')}", callback_data="cycle_method")],
            [InlineKeyboardButton("📝 Edit Base Appeal Template", callback_data="edit_template")],
            [InlineKeyboardButton("📧 Edit Target Email", callback_data="edit_target")],
            [InlineKeyboardButton(f"⏳ Delay: {s.get('min_delay', 25)}-{s.get('max_delay', 90)}s", callback_data="edit_delays")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_main")],
        ]
        await q.edit_message_text(
            "⚙️ **Settings & Template**",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown",
        )
        return

    if data == "cycle_method":
        s = core.load_settings()
        modes = ["web_form", "smtp", "gmail_web", "auto"]
        cur = s.get("send_method", "web_form")
        s["send_method"] = modes[(modes.index(cur) + 1) % len(modes)]
        core.save_settings(s)
        await q.edit_message_text(
            f"✅ Send method updated to: `{s['send_method']}`",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_settings")]]),
            parse_mode="Markdown",
        )
        return

    if data == "edit_template":
        USER_STATES[chat_id] = {"step": "edit_template"}
        await q.edit_message_text("📝 Send new base appeal template text:")
        return

    if data == "edit_target":
        USER_STATES[chat_id] = {"step": "edit_target"}
        await q.edit_message_text("📧 Send target email address:", parse_mode="Markdown")
        return

    if data == "edit_delays":
        USER_STATES[chat_id] = {"step": "edit_delays"}
        await q.edit_message_text("⏳ Send min and max delay in seconds (e.g., `20 60`):", parse_mode="Markdown")
        return

    if data == "menu_stats":
        st = core.load_stats()
        lines = [
            "📊 **Statistics**",
            f"• Success: `{st.get('total_sent', 0)}`",
            f"• Failed: `{st.get('failed', 0)}`",
            "\n**Last 10 runs:**",
        ]
        for h in reversed(st.get("history", [])[-10:]):
            icon = "✅" if h["success"] else "❌"
            lines.append(f"{icon} `{h['email']}` — [{h.get('channel', 'auto')}]")
        await q.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_main")]]),
            parse_mode="Markdown",
        )
        return


async def text_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()

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
        state["step"] = "add_fullname"
        await update.message.reply_text("🪪 Send **First and Last Name** or `/skip`:", parse_mode="Markdown")
        return

    if step == "add_fullname":
        if text.lower() != "/skip":
            parts = text.split(None, 1)
            state["first_name"] = parts[0]
            state["last_name"] = parts[1] if len(parts) > 1 else ""
        state["step"] = "add_app_password"
        await update.message.reply_text("🔑 Send **App Password** (16 chars) or `/skip`:", parse_mode="Markdown")
        return

    if step == "add_app_password":
        state["app_password"] = "" if text.lower() == "/skip" else text
        state["step"] = "add_password"
        await update.message.reply_text("🔒 Send **Account Password** or `/skip`:", parse_mode="Markdown")
        return

    if step == "add_password":
        state["password"] = "" if text.lower() == "/skip" else text
        added = core.add_account(
            email=state["email"],
            first_name=state.get("first_name", ""),
            last_name=state.get("last_name", ""),
            app_password=state.get("app_password", ""),
            password=state.get("password", ""),
        )
        USER_STATES.pop(chat_id, None)
        await update.message.reply_text(
            f"✅ Saved account `{state['email']}`." if added else "❌ Account already exists.",
            reply_markup=build_main_menu(),
            parse_mode="Markdown",
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
            f"✅ Updated `{email}`.",
            reply_markup=build_main_menu(),
            parse_mode="Markdown",
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
                f"✅ Interval set to `{mins}` minutes.",
                reply_markup=build_main_menu(),
                parse_mode="Markdown",
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
        await update.message.reply_text(f"✅ Target email updated to `{text}`.", reply_markup=build_main_menu(), parse_mode="Markdown")
        return

    if step == "edit_delays":
        try:
            p1, p2 = map(int, text.split())
            s = core.load_settings()
            s["min_delay"] = min(p1, p2)
            s["max_delay"] = max(p1, p2)
            core.save_settings(s)
            USER_STATES.pop(chat_id, None)
            await update.message.reply_text(f"✅ Delay set to `{s['min_delay']}`–`{s['max_delay']}`s.", reply_markup=build_main_menu(), parse_mode="Markdown")
        except Exception:
            await update.message.reply_text("❌ Send two numbers separated by space, e.g.: `25 90`")
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
            fn, ln = core._extract_profile_names_if_missing(driver, account)
            await bot.send_message(
                chat_id,
                f"✅ **Session Saved!**\n"
                f"• Account: `{account['email']}`\n"
                f"• Name: `{fn} {ln}`\n"
                f"• Device: `{account.get('device_name')}` (`{account.get('device_id')}`)",
                parse_mode="Markdown",
            )
        else:
            await bot.send_message(chat_id, f"❌ Login failed (`{account['email']}`): {msg}", parse_mode="Markdown")
    except Exception as e:
        await bot.send_message(chat_id, f"❌ Error: {e}")
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
            f"✅ **Appeal Submitted!**\n"
            f"• Account: `{account['email']}`\n"
            f"• Method: `{method}`\n"
            f"• Subject: `{report['subject']}`\n\n"
            f"📄 **Generated Text:**\n_{snippet}_",
            parse_mode="Markdown",
        )
    else:
        core.update_account(account["email"], last_status=f"Failed: {info}")
        await bot.send_message(
            chat_id,
            f"❌ **Submission Failed** (`{account['email']}`)\nReason: `{info}`",
            parse_mode="Markdown",
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
    token = os.getenv("9204455871:AAGTPgz2d086vZCYlFkrcDCci7DWr6un9oY")
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
