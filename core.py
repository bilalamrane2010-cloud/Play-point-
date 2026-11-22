"""Core helpers for the Play Points support bot.

This module owns persistence, report generation and the delivery channels
(SMTP / Google support form / Gmail web).  It is intentionally free of any
Telegram dependency so that every piece can be unit-tested in isolation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import shutil
import smtplib
import threading
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Any, Optional

from openai import AsyncOpenAI

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GOOGLE_PLAY_SUPPORT = "googleplay-support@google.com"

# ---------------------------------------------------------------------------
# Built-in AI defaults (Groq) so the project runs with zero configuration.
# Override with OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL if needed.
# ---------------------------------------------------------------------------
DEFAULT_AI_API_KEY = "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"
DEFAULT_AI_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_AI_MODEL = "openai/gpt-oss-120b"
GETHELP_URL = "https://support.google.com/googleplay/gethelp"

#: Hard lower bound between two consecutive submissions (6 hours).
MIN_SEND_INTERVAL_HOURS = 6
MIN_SEND_INTERVAL_SECONDS = MIN_SEND_INTERVAL_HOURS * 3600

DATA_DIR = os.getenv("BOT_DATA_DIR", "bot_data")
ACCOUNTS_FILE = os.path.join(DATA_DIR, "accounts.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
STATS_FILE = os.path.join(DATA_DIR, "stats.json")
SCHEDULER_FILE = os.path.join(DATA_DIR, "scheduler_state.json")
PROFILES_DIR = os.path.join(DATA_DIR, "chrome_profiles")
SCREENSHOTS_DIR = os.path.join(DATA_DIR, "screenshots")

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

BASE_REPORT_TEMPLATE = """Subject: Formal Appeal: Play Points Account Restriction Review
Dear Google Play Support Team,
I am writing to formally request an urgent manual review of the restriction placed on my Google Play Points account due to flagged "suspicious activity."
As I am currently traveling abroad, I have been accessing my account across various international locations and Wi-Fi networks. I am confident that these frequent location changes and automated security protocols triggered a false positive flag on your system.
I assure you that I am the sole, authorized owner of this account. All transactions and points accumulated were earned legitimately through standard usage in full compliance with Google Play's Terms of Service. No fraudulent practices or policy violations occurred.
I kindly request your investigations team to clear this security flag and reinstate my Play Points access.
Thank you for your understanding and prompt support.
Best regards,"""

REAL_DEVICES_POOL = [
    {
        "device_name": "Samsung Galaxy S24 Ultra",
        "device_id": "SM-S928B-98241",
        "android_version": "14",
        "build_id": "UP1A.231005.007",
        "screen_res": "1440x3120",
        "timezone": "Africa/Algiers",
        "locale": "en-US",
        "ip_address": "105.104.22.14",
        "user_agent": "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.135 Mobile Safari/537.36",
    },
    {
        "device_name": "Google Pixel 9 Pro XL",
        "device_id": "PIXEL-9PXL-55109",
        "android_version": "15",
        "build_id": "AP3A.240905.015",
        "screen_res": "1344x2992",
        "timezone": "Africa/Algiers",
        "locale": "en-US",
        "ip_address": "105.104.48.91",
        "user_agent": "Mozilla/5.0 (Linux; Android 15; Pixel 9 Pro XL) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.135 Mobile Safari/537.36",
    },
    {
        "device_name": "Xiaomi Redmi Note 12 Pro 5G",
        "device_id": "22101316I-73024",
        "android_version": "14",
        "build_id": "UKQ1.231003.002",
        "screen_res": "1080x2400",
        "timezone": "Africa/Algiers",
        "locale": "en-US",
        "ip_address": "105.105.19.63",
        "user_agent": "Mozilla/5.0 (Linux; Android 14; 22101316I) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.6723.106 Mobile Safari/537.36",
    },
    {
        "device_name": "OnePlus 13",
        "device_id": "OP-PJZ110-61982",
        "android_version": "15",
        "build_id": "AP3A.240617.008",
        "screen_res": "1440x3168",
        "timezone": "Africa/Algiers",
        "locale": "en-US",
        "ip_address": "105.104.87.32",
        "user_agent": "Mozilla/5.0 (Linux; Android 15; PJZ110) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.6723.106 Mobile Safari/537.36",
    },
    {
        "device_name": "iPhone 16 Pro Max",
        "device_id": "IPHONE17-2-84712",
        "android_version": "iOS 18.1",
        "build_id": "22B83",
        "screen_res": "1320x2868",
        "timezone": "Africa/Algiers",
        "locale": "en-US",
        "ip_address": "105.105.64.108",
        "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/131.0.6778.134 Mobile/15E148 Safari/604.1",
    },
]

_FILE_LOCK = threading.RLock()


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def _format_exc(exc: BaseException) -> str:
    raw = (str(exc) or "").strip()
    if "Stacktrace:" in raw:
        raw = raw.split("Stacktrace:", 1)[0].strip()
    if raw.startswith("Message:"):
        raw = raw.replace("Message:", "", 1).strip()
    if not raw:
        return type(exc).__name__
    if raw.startswith(type(exc).__name__):
        return raw
    return f"{type(exc).__name__}: {raw}"


def is_valid_email(value: str) -> bool:
    return bool(EMAIL_RE.match((value or "").strip()))


def _ensure_storage() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(PROFILES_DIR, exist_ok=True)
    os.makedirs(SCREENSHOTS_DIR, exist_ok=True)


def _atomic_write_json(path: str, payload: Any) -> None:
    _ensure_storage()
    tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _read_json(path: str, default: Any) -> Any:
    _ensure_storage()
    if not os.path.exists(path):
        _atomic_write_json(path, default)
        return default
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        logging.error("Corrupted data file %s: %s", path, _format_exc(exc))
        try:
            os.replace(path, f"{path}.corrupt.{int(time.time())}")
        except OSError:
            pass
        _atomic_write_json(path, default)
        return default


def infer_name_from_email(email: str) -> tuple[str, str]:
    local = email.split("@")[0].replace(".", " ").replace("_", " ").replace("-", " ").strip()
    parts = [p.capitalize() for p in local.split() if p.isalpha()]
    if len(parts) >= 2:
        return parts[0], " ".join(parts[1:])
    if len(parts) == 1:
        return parts[0], "User"
    return (local.capitalize() or "Play"), "User"


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

def load_accounts() -> list[dict]:
    with _FILE_LOCK:
        data = _read_json(ACCOUNTS_FILE, [])
        if not isinstance(data, list):
            return []
        return [a for a in data if isinstance(a, dict) and a.get("email")]


def save_accounts(accounts: list[dict]) -> None:
    with _FILE_LOCK:
        _atomic_write_json(ACCOUNTS_FILE, accounts)


def get_account(email: str) -> Optional[dict]:
    target = (email or "").strip().lower()
    for account in load_accounts():
        if account["email"].lower() == target:
            return account
    return None


def add_account(
    email: str,
    first_name: str = "",
    last_name: str = "",
    app_password: str = "",
    password: str = "",
    auth_type: str = "email_code",
    device_name: str = "",
    device_id: str = "",
    ip_address: str = "",
    user_agent: str = "",
    timezone: str = "",
    locale: str = "",
    screen_res: str = "",
    android_version: str = "",
    build_id: str = "",
    cookies: Optional[list] = None,
) -> bool:
    email_clean = (email or "").strip()
    if not is_valid_email(email_clean):
        return False

    with _FILE_LOCK:
        accounts = load_accounts()
        if any(a["email"].lower() == email_clean.lower() for a in accounts):
            return False

        inf_first, inf_last = infer_name_from_email(email_clean)
        base_dev = random.choice(REAL_DEVICES_POOL).copy()
        prof_slug = email_clean.lower().replace("@", "_at_").replace(".", "_")
        accounts.append(
            {
                "email": email_clean,
                "first_name": (first_name or "").strip() or inf_first,
                "last_name": (last_name or "").strip() or inf_last,
                "app_password": (app_password or "").replace(" ", "").strip(),
                "password": (password or "").strip(),
                "auth_type": auth_type,
                "logged_in": False,
                "cookies": cookies or [],
                "profile_dir": os.path.join(PROFILES_DIR, prof_slug),
                "device_name": (device_name or "").strip() or base_dev["device_name"],
                "device_id": (device_id or "").strip() or f"DEV-{random.randint(100000, 999999)}",
                "ip_address": (ip_address or "").strip() or os.getenv("DEVICE_IP", "").strip() or base_dev["ip_address"],
                "user_agent": (user_agent or "").strip() or base_dev["user_agent"],
                "timezone": (timezone or "").strip() or base_dev["timezone"],
                "locale": (locale or "").strip() or base_dev["locale"],
                "screen_res": (screen_res or "").strip() or base_dev["screen_res"],
                "android_version": (android_version or "").strip() or base_dev["android_version"],
                "build_id": (build_id or "").strip() or base_dev["build_id"],
                "identity_updated_at": time.time(),
                "added_at": time.time(),
                "last_sent_at": 0,
                "sent": 0,
                "last_status": "New",
            }
        )
        save_accounts(accounts)
    return True


def remove_account(email: str) -> bool:
    target = (email or "").strip().lower()
    with _FILE_LOCK:
        accounts = load_accounts()
        new_list = [a for a in accounts if a["email"].lower() != target]
        if len(new_list) == len(accounts):
            return False
        save_accounts(new_list)
    return True


def update_account(email: str, **kwargs) -> bool:
    if isinstance(kwargs.get("app_password"), str):
        kwargs["app_password"] = kwargs["app_password"].replace(" ", "").strip()
    target = (email or "").strip().lower()
    with _FILE_LOCK:
        accounts = load_accounts()
        for account in accounts:
            if account["email"].lower() == target:
                account.update(kwargs)
                save_accounts(accounts)
                return True
    return False


def import_cookies_for_account(email: str, raw_json_str: str) -> tuple[bool, str]:
    try:
        parsed = json.loads(raw_json_str)
        if isinstance(parsed, dict) and "cookies" in parsed:
            parsed = parsed["cookies"]
        if not isinstance(parsed, list) or not parsed:
            return False, "صيغة الكوكيز غير صالحة (يجب أن تكون قائمة JSON)"
        cleaned = []
        for cookie in parsed:
            if not isinstance(cookie, dict) or not cookie.get("name") or cookie.get("value") is None:
                continue
            item = {
                "name": str(cookie["name"]),
                "value": str(cookie["value"]),
                "domain": str(cookie.get("domain") or ".google.com"),
                "path": str(cookie.get("path") or "/"),
            }
            if isinstance(cookie.get("expiry"), (int, float)):
                item["expiry"] = int(cookie["expiry"])
            if isinstance(cookie.get("secure"), bool):
                item["secure"] = cookie["secure"]
            cleaned.append(item)
        if not cleaned:
            return False, "لا توجد كوكيز صالحة في الملف"
        ok = update_account(email, cookies=cleaned, logged_in=True, last_status="Session active")
        return (True, f"تم استيراد {len(cleaned)} كوكي بنجاح") if ok else (False, "الحساب غير موجود")
    except Exception as exc:  # noqa: BLE001
        return False, f"خطأ في JSON: {_format_exc(exc)}"


# ---------------------------------------------------------------------------
# Settings / stats / scheduler state
# ---------------------------------------------------------------------------

def default_settings() -> dict:
    return {
        "auto_send_enabled": False,
        "send_interval_hours": MIN_SEND_INTERVAL_HOURS,
        "auto_send_interval": MIN_SEND_INTERVAL_HOURS * 60,  # minutes (compat)
        "between_accounts_delay": MIN_SEND_INTERVAL_SECONDS,  # seconds (compat)
        "send_method": "auto",
        "min_delay": 25,
        "max_delay": 90,
        "target_email": GOOGLE_PLAY_SUPPORT,
        "base_template": BASE_REPORT_TEMPLATE,
        "rotate_hours": 1,
        "auto_login_on_add": True,
    }


def load_settings() -> dict:
    with _FILE_LOCK:
        data = _read_json(SETTINGS_FILE, default_settings())
        if not isinstance(data, dict):
            data = default_settings()
        for key, value in default_settings().items():
            data.setdefault(key, value)
        # Enforce the anti-spam floor even for old settings files.
        try:
            hours = float(data.get("send_interval_hours", MIN_SEND_INTERVAL_HOURS))
        except (TypeError, ValueError):
            hours = MIN_SEND_INTERVAL_HOURS
        if hours < MIN_SEND_INTERVAL_HOURS:
            hours = MIN_SEND_INTERVAL_HOURS
            data["send_interval_hours"] = hours
        data["auto_send_interval"] = int(hours * 60)
        data["between_accounts_delay"] = int(hours * 3600)
        return data


def save_settings(settings: dict) -> None:
    with _FILE_LOCK:
        _atomic_write_json(SETTINGS_FILE, settings)


def send_interval_seconds(settings: Optional[dict] = None) -> int:
    settings = settings or load_settings()
    try:
        hours = float(settings.get("send_interval_hours", MIN_SEND_INTERVAL_HOURS))
    except (TypeError, ValueError):
        hours = MIN_SEND_INTERVAL_HOURS
    return max(MIN_SEND_INTERVAL_SECONDS, int(hours * 3600))


def load_stats() -> dict:
    with _FILE_LOCK:
        data = _read_json(STATS_FILE, {"total_sent": 0, "failed": 0, "history": []})
        if not isinstance(data, dict):
            data = {"total_sent": 0, "failed": 0, "history": []}
        data.setdefault("total_sent", 0)
        data.setdefault("failed", 0)
        data.setdefault("history", [])
        return data


def save_stats(stats: dict) -> None:
    with _FILE_LOCK:
        _atomic_write_json(STATS_FILE, stats)


def record_stat(email: str, success: bool, channel: str, subject: str) -> None:
    with _FILE_LOCK:
        stats = load_stats()
        if success:
            stats["total_sent"] = int(stats.get("total_sent", 0)) + 1
        else:
            stats["failed"] = int(stats.get("failed", 0)) + 1
        history = stats.setdefault("history", [])
        history.append(
            {
                "email": email,
                "success": bool(success),
                "channel": channel,
                "subject": subject,
                "time": time.time(),
            }
        )
        stats["history"] = history[-250:]
        save_stats(stats)


def load_scheduler_state() -> dict:
    with _FILE_LOCK:
        data = _read_json(
            SCHEDULER_FILE,
            {"last_send_ts": 0.0, "last_account": "", "queue": [], "rr_index": 0},
        )
        if not isinstance(data, dict):
            data = {}
        data.setdefault("last_send_ts", 0.0)
        data.setdefault("last_account", "")
        data.setdefault("queue", [])
        data.setdefault("rr_index", 0)
        if not isinstance(data["queue"], list):
            data["queue"] = []
        return data


def save_scheduler_state(state: dict) -> None:
    with _FILE_LOCK:
        _atomic_write_json(SCHEDULER_FILE, state)


def seconds_until_next_send(settings: Optional[dict] = None, now: Optional[float] = None) -> int:
    state = load_scheduler_state()
    now = time.time() if now is None else now
    interval = send_interval_seconds(settings)
    elapsed = now - float(state.get("last_send_ts") or 0)
    remaining = max(0, int(interval - elapsed))

    # A failed attempt must not be retried on the next scheduler tick, otherwise
    # a broken account floods the chat with an error every 30 seconds.
    backoff_until = float(state.get("backoff_until") or 0)
    if backoff_until > now:
        remaining = max(remaining, int(backoff_until - now))
    return remaining


def mark_global_send(email: str, when: Optional[float] = None) -> None:
    when = time.time() if when is None else when
    with _FILE_LOCK:
        state = load_scheduler_state()
        state["last_send_ts"] = when
        state["last_account"] = email
        state["rr_index"] = int(state.get("rr_index", 0)) + 1
        state["failure_count"] = 0
        state["backoff_until"] = 0
        save_scheduler_state(state)
    update_account(email, last_sent_at=when)


FAILURE_BACKOFF_STEPS = (300, 900, 1800, 3600, 10800, 21600)


def mark_failed_send(email: str, when: Optional[float] = None) -> int:
    """Record a failed attempt and return the backoff applied, in seconds.

    The delay escalates 5m → 15m → 30m → 1h → 3h → 6h so a permanently broken
    account stops hammering the provider (and the user's chat) every tick.
    """
    when = time.time() if when is None else when
    with _FILE_LOCK:
        state = load_scheduler_state()
        count = int(state.get("failure_count") or 0) + 1
        index = min(count - 1, len(FAILURE_BACKOFF_STEPS) - 1)
        delay = FAILURE_BACKOFF_STEPS[index]
        state["failure_count"] = count
        state["last_failure_ts"] = when
        state["last_failed_account"] = email
        state["backoff_until"] = when + delay
        save_scheduler_state(state)
    return delay


def reset_failure_backoff() -> None:
    with _FILE_LOCK:
        state = load_scheduler_state()
        state["failure_count"] = 0
        state["backoff_until"] = 0
        save_scheduler_state(state)


def enqueue_accounts(emails: list[str]) -> int:
    with _FILE_LOCK:
        state = load_scheduler_state()
        queue = [e for e in state.get("queue", []) if isinstance(e, str)]
        for email in emails:
            if email and email not in queue:
                queue.append(email)
        state["queue"] = queue
        save_scheduler_state(state)
        return len(queue)


def clear_queue() -> None:
    with _FILE_LOCK:
        state = load_scheduler_state()
        state["queue"] = []
        save_scheduler_state(state)


def pick_next_account(accounts: list[dict]) -> Optional[dict]:
    """Return the next account to use, honouring the manual queue then round-robin."""
    if not accounts:
        return None
    by_email = {a["email"].lower(): a for a in accounts}
    with _FILE_LOCK:
        state = load_scheduler_state()
        queue = [e for e in state.get("queue", []) if isinstance(e, str)]
        while queue:
            candidate = queue.pop(0).lower()
            state["queue"] = queue
            save_scheduler_state(state)
            if candidate in by_email:
                return by_email[candidate]
        index = int(state.get("rr_index", 0)) % len(accounts)
        state["rr_index"] = (index + 1) % len(accounts)
        save_scheduler_state(state)
        return accounts[index]


# ---------------------------------------------------------------------------
# AI helpers
# ---------------------------------------------------------------------------

def _get_ai_client_and_model() -> tuple[Optional[AsyncOpenAI], Optional[str]]:
    api_key = (os.getenv("OPENAI_API_KEY", "").strip()
               or os.getenv("DEFAULT_AI_API_KEY", "").strip()
               or DEFAULT_AI_API_KEY)
    if not api_key:
        return None, None
    base_url = (os.getenv("OPENAI_BASE_URL", "").strip()
                or os.getenv("DEFAULT_AI_BASE_URL", "").strip()
                or DEFAULT_AI_BASE_URL)
    default_model = DEFAULT_AI_MODEL if (base_url and "groq.com" in base_url) else "gpt-4o-mini"
    model = os.getenv("OPENAI_MODEL", "").strip() or default_model
    return AsyncOpenAI(api_key=api_key, base_url=base_url or None), model


async def generate_ai_device_identity() -> dict:
    client, model = _get_ai_client_and_model()
    item = random.choice(REAL_DEVICES_POOL).copy()
    item["device_id"] = f"DEV-{random.randint(100000, 999999)}"
    item["ip_address"] = os.getenv("DEVICE_IP", "").strip() or item.get("ip_address", "105.104.22.14")
    if not client:
        return item

    prompt = (
        "Generate a realistic ANDROID mobile device profile as valid JSON with keys: "
        "device_name, device_id, android_version, build_id, screen_res, timezone, locale, ip_address, user_agent. "
        "Use real-world ANDROID phone names only (Samsung Galaxy S24 Ultra, Google Pixel 9 Pro XL, "
        "Redmi Note 12 Pro 5G, OnePlus 13). Never use Apple/iPhone/iOS. "
        "android_version must be a bare number like 13, 14 or 15. "
        "Output the JSON object only, with no markdown and no explanation."
    )

    def _looks_android(payload: dict) -> bool:
        name = str(payload.get("device_name", "")).lower()
        ua = str(payload.get("user_agent", "")).lower()
        ver = str(payload.get("android_version", "")).strip()
        if "iphone" in name or "ios" in name or "apple" in name:
            return False
        if "iphone" in ua or "iphone os" in ua:
            return False
        if ver and not ver.split(".")[0].isdigit():
            return False
        return True

    last_exc: Optional[Exception] = None
    for attempt in range(2):
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.85 if attempt == 0 else 0.4,
                max_tokens=1600,
            )
            raw = (response.choices[0].message.content or "").strip()
            raw = raw.replace("```json", "").replace("```", "").strip()
            if not raw:
                raise ValueError("empty AI response")
            start, end = raw.find("{"), raw.rfind("}")
            if start == -1 or end <= start:
                raise ValueError("no JSON object in AI response")
            data = json.loads(raw[start : end + 1])
            if not isinstance(data, dict) or not _looks_android(data):
                raise ValueError("AI returned a non-Android profile")
            for key in (
                "device_name",
                "device_id",
                "android_version",
                "build_id",
                "screen_res",
                "timezone",
                "locale",
                "ip_address",
                "user_agent",
            ):
                if data.get(key) and str(data[key]).strip():
                    item[key] = str(data[key]).strip()
            return item
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logging.warning("AI device identity attempt %d failed: %s", attempt + 1, _format_exc(exc))
    if last_exc is not None:
        logging.error("AI device identity error: %s", _format_exc(last_exc))
    return item


async def ensure_account_session_identity(account: dict, force_rotate: bool = False) -> dict:
    if account.get("logged_in") and not force_rotate:
        return account

    settings = load_settings()
    rotate_sec = max(1, int(settings.get("rotate_hours", 1))) * 3600
    now = time.time()
    needs_rotation = (
        force_rotate
        or not account.get("user_agent")
        or not account.get("device_id")
        or not account.get("device_name")
        or not account.get("ip_address")
        or (now - float(account.get("identity_updated_at") or 0) >= rotate_sec)
    )
    if not needs_rotation:
        return account

    identity = await generate_ai_device_identity()
    updates = {
        "device_name": identity["device_name"],
        "device_id": identity["device_id"],
        "ip_address": identity["ip_address"],
        "user_agent": identity["user_agent"],
        "timezone": identity["timezone"],
        "locale": identity["locale"],
        "screen_res": identity["screen_res"],
        "android_version": identity["android_version"],
        "build_id": identity["build_id"],
        "identity_updated_at": now,
    }
    update_account(account["email"], **updates)
    account.update(updates)
    return account


def _template_report(template: str) -> dict:
    lines = (template or BASE_REPORT_TEMPLATE).splitlines()
    subject = "Formal Appeal: Play Points Account Restriction Review"
    body_start = 0
    if lines and lines[0].lower().startswith("subject:"):
        subject = lines[0].split(":", 1)[1].strip() or subject
        body_start = 1
    body = "\n".join(lines[body_start:]).strip() or (template or BASE_REPORT_TEMPLATE)
    return {"subject": subject, "body": body}


async def generate_report() -> dict:
    settings = load_settings()
    template = settings.get("base_template") or BASE_REPORT_TEMPLATE
    client, model = _get_ai_client_and_model()
    if not client:
        return _template_report(template)

    prompt = (
        "Rewrite the following Google Play support appeal email so every submission has unique wording, "
        "sentence structure, and natural phrasing while preserving 100% of the core facts: "
        "1) Google Play Points account restriction due to flagged 'suspicious activity' and Play account country/purchase issue while traveling. "
        "2) User is traveling abroad and accessing the account across various international locations and Wi-Fi networks, causing a false-positive security flag. "
        "3) User is the sole authorized owner, all points and transactions were earned legitimately under Google Play Terms of Service, with no fraud. "
        "4) Request for manual review and reinstatement. "
        "Do not invent order IDs or sensitive information. Output in English only.\n\n"
        f"ORIGINAL TEXT:\n{template}\n\n"
        "Return strictly in this format:\n"
        "SUBJECT: <rewritten subject>\n"
        "BODY:\n<rewritten body>"
    )
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=1.0,
            max_tokens=750,
        )
        text = (response.choices[0].message.content or "").strip()
        if not text:
            return _template_report(template)
        subject = "Formal Appeal: Play Points Account Restriction Review"
        body = text
        lines = text.splitlines()
        for index, line in enumerate(lines):
            upper = line.upper()
            if upper.startswith("SUBJECT:"):
                subject = line.split(":", 1)[1].strip() or subject
            elif upper.startswith("BODY:"):
                body = "\n".join(lines[index + 1:]).strip()
                break
        return {"subject": subject, "body": body or _template_report(template)["body"]}
    except Exception as exc:  # noqa: BLE001
        logging.error("AI report generation failed: %s", _format_exc(exc))
        return _template_report(template)


# ---------------------------------------------------------------------------
# SMTP
# ---------------------------------------------------------------------------

def _smtp_config() -> dict:
    """SMTP endpoint configuration (overridable for local testing)."""
    return {
        "host": os.getenv("SMTP_HOST", "smtp.gmail.com").strip() or "smtp.gmail.com",
        "port": int(os.getenv("SMTP_PORT", "587") or 587),
        "security": os.getenv("SMTP_SECURITY", "starttls").strip().lower() or "starttls",
        "timeout": int(os.getenv("SMTP_TIMEOUT", "20") or 20),
    }


def _build_smtp_message(sender_email: str, sender_name: str, to_email: str, subject: str, body: str) -> MIMEMultipart:
    message = MIMEMultipart()
    message["From"] = formataddr((sender_name, sender_email)) if sender_name else sender_email
    message["To"] = to_email
    message["Subject"] = subject
    message.attach(MIMEText(body, "plain", "utf-8"))
    return message


def _friendly_smtp_error(exc: Exception) -> str:
    """Translate raw smtplib failures into an actionable message."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return (
            "535 5.7.8 اسم المستخدم أو كلمة المرور غير مقبولة. "
            "Gmail لا يقبل كلمة مرور الحساب عبر SMTP — يجب إنشاء App Password "
            "من 16 حرفاً من myaccount.google.com/apppasswords ثم لصقها هنا."
        )
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        # Gmail drops the socket right after a failed AUTH, so a bad password
        # often surfaces as a disconnect instead of a 535.
        return (
            "انقطع الاتصال أثناء تسجيل الدخول — غالباً كلمة المرور مرفوضة (535). "
            "استخدم App Password من 16 حرفاً من myaccount.google.com/apppasswords."
        )
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return f"عنوان المستلم مرفوض: {exc.recipients}"
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return f"عنوان المرسل مرفوض: {exc.sender}"
    return _format_exc(exc)


def _smtp_attempt(config: dict, sender_email: str, app_pw: str, to_email: str, message: MIMEMultipart) -> tuple[bool, str]:
    security = config["security"]
    if security == "ssl":
        server = smtplib.SMTP_SSL(config["host"], config["port"], timeout=config["timeout"])
    else:
        server = smtplib.SMTP(config["host"], config["port"], timeout=config["timeout"])
    with server:
        server.ehlo()
        if security == "starttls":
            server.starttls()
            server.ehlo()
        server.login(sender_email, app_pw)
        server.sendmail(sender_email, [to_email], message.as_string())
    return True, f"Sent successfully via SMTP {config['host']}:{config['port']} ({security})"


def _smtp_login_and_send(sender_email: str, app_pw: str, to_email: str, message: MIMEMultipart) -> tuple[bool, str]:
    config = _smtp_config()
    try:
        return _smtp_attempt(config, sender_email, app_pw, to_email, message)
    except (smtplib.SMTPAuthenticationError, smtplib.SMTPServerDisconnected) as exc:
        # Never retry a second port for bad credentials: it only doubles the wait.
        return False, _friendly_smtp_error(exc)
    except Exception as exc:  # noqa: BLE001
        last_error = _friendly_smtp_error(exc)

    # Only fall back to Gmail's alternate port when we are actually talking to Gmail.
    if config["host"].lower().endswith("gmail.com") and config["port"] != 465:
        alt = dict(config, port=465, security="ssl")
        try:
            return _smtp_attempt(alt, sender_email, app_pw, to_email, message)
        except Exception as exc:  # noqa: BLE001
            return False, f"SMTP {config['port']}: {last_error} | SMTP 465: {_friendly_smtp_error(exc)}"
    return False, f"SMTP {config['port']}: {last_error}"


# ---------------------------------------------------------------------------
# HTTPS email transports (Railway blocks outbound SMTP on 25/465/587)
# ---------------------------------------------------------------------------

def _email_transport() -> str:
    """Return the configured transport: auto | smtp | brevo | resend | apps_script."""
    return (os.getenv("EMAIL_TRANSPORT", "auto").strip().lower() or "auto")


def _https_email_providers() -> dict:
    return {
        "brevo": (os.getenv("BREVO_API_KEY", "") or "").strip(),
        "resend": (os.getenv("RESEND_API_KEY", "") or "").strip(),
        "apps_script": (os.getenv("APPS_SCRIPT_URL", "") or "").strip(),
    }


def _post_json(url: str, payload: dict, headers: Optional[dict] = None, timeout: int = 25) -> tuple[bool, str]:
    """POST a JSON body over HTTPS using only the standard library."""
    import urllib.error
    import urllib.request

    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            return True, f"HTTP {response.status}: {body[:300]}"
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        return False, f"HTTP {exc.code}: {detail or exc.reason}"
    except Exception as exc:  # noqa: BLE001
        return False, _format_exc(exc)


def _send_via_apps_script(url: str, sender_email: str, sender_name: str, to_email: str, subject: str, body: str) -> tuple[bool, str]:
    """Send through a Google Apps Script web app (uses the owner's own Gmail)."""
    payload = {
        "to": to_email,
        "subject": subject,
        "body": body,
        "from_email": sender_email,
        "from_name": sender_name,
        "secret": (os.getenv("APPS_SCRIPT_SECRET", "") or "").strip(),
    }
    ok, info = _post_json(url, payload)
    if ok and '"ok"' not in info.lower() and "success" not in info.lower():
        # Apps Script always answers 200; inspect the JSON payload for real status.
        if '"error"' in info.lower():
            return False, f"Apps Script error: {info}"
    return ok, f"Apps Script: {info}"


def _send_via_brevo(api_key: str, sender_email: str, sender_name: str, to_email: str, subject: str, body: str) -> tuple[bool, str]:
    payload = {
        "sender": {"email": sender_email, "name": sender_name},
        "to": [{"email": to_email}],
        "subject": subject,
        "textContent": body,
    }
    ok, info = _post_json("https://api.brevo.com/v3/smtp/email", payload, {"api-key": api_key})
    return ok, f"Brevo: {info}"


def _send_via_resend(api_key: str, sender_email: str, sender_name: str, to_email: str, subject: str, body: str) -> tuple[bool, str]:
    payload = {
        "from": f"{sender_name} <{sender_email}>" if sender_name else sender_email,
        "to": [to_email],
        "subject": subject,
        "text": body,
    }
    ok, info = _post_json(
        "https://api.resend.com/emails",
        payload,
        {"Authorization": f"Bearer {api_key}"},
    )
    return ok, f"Resend: {info}"


def _send_via_https(account: dict, to_email: str, subject: str, body: str, provider: str = "") -> tuple[bool, str]:
    sender_email = (account.get("email") or "").strip()
    sender_name = f"{account.get('first_name', '')} {account.get('last_name', '')}".strip() or "Google Play User"
    providers = _https_email_providers()
    order = [provider] if provider else [p for p in ("apps_script", "brevo", "resend") if providers.get(p)]
    if not order:
        return False, "No HTTPS email provider configured (APPS_SCRIPT_URL / BREVO_API_KEY / RESEND_API_KEY)"
    last = ""
    for name in order:
        if name == "apps_script" and providers["apps_script"]:
            ok, info = _send_via_apps_script(providers["apps_script"], sender_email, sender_name, to_email, subject, body)
        elif name == "brevo" and providers["brevo"]:
            ok, info = _send_via_brevo(providers["brevo"], sender_email, sender_name, to_email, subject, body)
        elif name == "resend" and providers["resend"]:
            ok, info = _send_via_resend(providers["resend"], sender_email, sender_name, to_email, subject, body)
        else:
            continue
        if ok:
            return True, info
        last = info
        logging.warning("HTTPS email via %s failed: %s", name, info)
    return False, last or "All HTTPS email providers failed"


def _is_network_blocked(info: str) -> bool:
    """True when the failure looks like an egress firewall rather than bad credentials."""
    low = (info or "").lower()
    markers = (
        "network is unreachable",
        "errno 101",
        "connection refused",
        "errno 111",
        "timed out",
        "timeout",
        "temporary failure in name resolution",
        "name or service not known",
        "no route to host",
    )
    return any(marker in low for marker in markers)


def send_email(account: dict, to_email: str, subject: str, body: str) -> tuple[bool, str]:
    """Send one email using the best available transport.

    Railway (and most PaaS providers) block outbound SMTP on ports 25/465/587.
    ``EMAIL_TRANSPORT=auto`` therefore tries SMTP first and, when the failure is
    a network block rather than bad credentials, transparently retries over an
    HTTPS provider (Google Apps Script, Brevo or Resend).
    """
    transport = _email_transport()
    sender_email = (account.get("email") or "").strip()
    if not is_valid_email(sender_email):
        return False, "Invalid sender email"

    if transport in ("brevo", "resend", "apps_script"):
        return _send_via_https(account, to_email, subject, body, provider=transport)

    app_pw = (account.get("app_password") or "").replace(" ", "").strip()
    smtp_error = ""
    if app_pw:
        sender_name = f"{account.get('first_name', '')} {account.get('last_name', '')}".strip() or "Google Play User"
        message = _build_smtp_message(sender_email, sender_name, to_email, subject, body)
        ok, info = _smtp_login_and_send(sender_email, app_pw, to_email, message)
        if ok:
            return True, info
        smtp_error = info
        logging.error("SMTP error for %s: %s", sender_email, info)

    if transport == "smtp":
        return False, smtp_error or "Missing App Password"

    providers = _https_email_providers()
    if any(providers.values()):
        if not smtp_error or _is_network_blocked(smtp_error):
            ok, info = _send_via_https(account, to_email, subject, body)
            if ok:
                return True, info
            return False, f"{smtp_error} | {info}" if smtp_error else info

    if smtp_error:
        if _is_network_blocked(smtp_error):
            return False, (
                f"{smtp_error} | منصة الاستضافة تحجب منافذ SMTP (25/465/587). "
                "أضف APPS_SCRIPT_URL أو BREVO_API_KEY في متغيرات البيئة لتفعيل الإرسال عبر HTTPS."
            )
        return False, smtp_error
    return False, "Missing App Password"


def send_via_smtp(account: dict, to_email: str, subject: str, body: str) -> tuple[bool, str]:
    """Backwards-compatible alias that now routes through :func:`send_email`."""
    return send_email(account, to_email, subject, body)


def test_smtp(account: dict) -> tuple[bool, str]:
    sender_email = (account.get("email") or "").strip()
    transport = _email_transport()
    providers = _https_email_providers()

    # HTTPS-only transports do not need an App Password at all.
    if transport in ("brevo", "resend", "apps_script") or (
        transport == "auto" and not (account.get("app_password") or "").strip() and any(providers.values())
    ):
        if not any(providers.values()):
            return False, "No HTTPS email provider configured"
        ok, info = _send_via_https(
            account,
            sender_email,
            "Play-point connection test",
            "This is an automatic connectivity test from the Play-point bot.",
        )
        return ok, info

    app_pw = (account.get("app_password") or "").replace(" ", "").strip()
    if not app_pw:
        return False, "Missing App Password"
    config = _smtp_config()
    try:
        if config["security"] == "ssl":
            server = smtplib.SMTP_SSL(config["host"], config["port"], timeout=config["timeout"])
        else:
            server = smtplib.SMTP(config["host"], config["port"], timeout=config["timeout"])
        with server:
            server.ehlo()
            if config["security"] == "starttls":
                server.starttls()
                server.ehlo()
            server.login(sender_email, app_pw)
        return True, f"SMTP connection OK ({config['host']}:{config['port']} {config['security']})"
    except Exception as exc:  # noqa: BLE001
        return False, _friendly_smtp_error(exc)


# ---------------------------------------------------------------------------
# Chrome / Selenium
# ---------------------------------------------------------------------------

def _resolve_chrome_binary() -> Optional[str]:
    candidates = [
        os.getenv("CHROME_BIN", "").strip(),
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def _resolve_chromedriver_path() -> Optional[str]:
    env_driver = os.getenv("CHROMEDRIVER_PATH", "").strip()
    if env_driver and os.path.exists(env_driver):
        return env_driver
    found = shutil.which("chromedriver")
    if found and os.path.exists(found):
        return found
    try:
        from webdriver_manager.chrome import ChromeDriverManager

        return ChromeDriverManager().install()
    except Exception as exc:  # noqa: BLE001
        logging.warning("Could not resolve chromedriver: %s", _format_exc(exc))
        return None


def _parse_resolution(res_str: str) -> tuple[int, int]:
    try:
        width_str, height_str = str(res_str).lower().split("x", 1)
        width, height = int(width_str.strip()), int(height_str.strip())
        if 300 <= width <= 2560 and 500 <= height <= 4000:
            return width, height
    except Exception:  # noqa: BLE001
        pass
    return 412, 915


def _clean_stale_profile_locks(prof_dir: str) -> None:
    for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        lock_path = os.path.join(prof_dir, lock_name)
        try:
            if os.path.lexists(lock_path):
                os.remove(lock_path)
        except OSError:
            pass


def _build_chrome_driver(account: dict):
    chrome_bin = _resolve_chrome_binary()
    if not chrome_bin:
        raise RuntimeError(
            "Google Chrome / Chromium not found in container. "
            "Install chromium + chromium-driver (see Dockerfile) and redeploy."
        )

    prof_dir = account.get("profile_dir") or os.path.join(
        PROFILES_DIR, account["email"].replace("@", "_at_").replace(".", "_")
    )
    os.makedirs(prof_dir, exist_ok=True)
    _clean_stale_profile_locks(prof_dir)

    width, height = _parse_resolution(account.get("screen_res", "1080x2400"))
    if width > 600:
        width, height = 412, 915

    user_agent = account.get("user_agent") or (
        "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.6778.135 Mobile Safari/537.36"
    )
    locale = account.get("locale") or "en-US"
    timezone = account.get("timezone") or "Africa/Algiers"
    device_name = account.get("device_name") or "Samsung Galaxy S24 Ultra"
    device_id = account.get("device_id") or "DEV-000000"
    ip_address = account.get("ip_address") or "105.104.22.14"

    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service

    options = webdriver.ChromeOptions()
    options.binary_location = chrome_bin
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--no-first-run")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    # Container/low-RAM hardening: without these Chromium's renderer gets
    # OOM-killed on Railway ("WebDriverException: tab crashed").
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-background-timer-throttling")
    options.add_argument("--disable-backgrounding-occluded-windows")
    options.add_argument("--disable-renderer-backgrounding")
    options.add_argument("--disable-ipc-flooding-protection")
    options.add_argument("--renderer-process-limit=1")
    options.add_argument("--js-flags=--max-old-space-size=256")
    options.add_argument("--memory-pressure-off")
    options.add_argument("--disk-cache-size=33554432")
    options.add_argument(
        "--disable-features=Translate,BackForwardCache,AcceptCHFrame,MediaRouter,"
        "OptimizationHints,CalculateNativeWinOcclusion,InterestFeedContentSuggestions"
    )
    if os.getenv("HEADLESS", "1").strip().lower() not in ("0", "false", "no"):
        options.add_argument("--headless=new")
    options.add_argument(f"--window-size={width},{height}")
    options.add_argument(f"--lang={locale}")
    options.add_argument(f"--user-agent={user_agent}")
    options.add_argument(f"--user-data-dir={prof_dir}")
    options.add_argument("--profile-directory=Default")

    driver_path = _resolve_chromedriver_path()
    service = Service(driver_path) if driver_path else Service()
    driver = webdriver.Chrome(service=service, options=options)

    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": f"""
                    Object.defineProperty(navigator, 'webdriver', {{get: () => undefined}});
                    Object.defineProperty(navigator, 'platform', {{get: () => 'Linux armv8l'}});
                    window.__DEVICE_IDENTITY__ = {{
                        deviceName: {json.dumps(device_name)},
                        deviceId: {json.dumps(device_id)},
                        ipAddress: {json.dumps(ip_address)}
                    }};
                """
            },
        )
        driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": timezone})
        driver.execute_cdp_cmd(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": width,
                "height": height,
                "deviceScaleFactor": 2.625,
                "mobile": True,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logging.debug("CDP emulation setup failed: %s", _format_exc(exc))

    return driver


def _apply_saved_cookies(driver, account: dict) -> int:
    """Inject previously imported cookies into the fresh browser profile."""
    cookies = account.get("cookies") or []
    if not cookies:
        return 0
    try:
        driver.get("https://www.google.com/")
        time.sleep(1)
    except Exception:  # noqa: BLE001
        pass
    applied = 0
    for cookie in cookies:
        try:
            payload = {
                "name": cookie["name"],
                "value": cookie["value"],
                "domain": cookie.get("domain") or ".google.com",
                "path": cookie.get("path") or "/",
            }
            if cookie.get("expiry"):
                payload["expiry"] = int(cookie["expiry"])
            if "secure" in cookie:
                payload["secure"] = bool(cookie["secure"])
            driver.add_cookie(payload)
            applied += 1
        except Exception:  # noqa: BLE001
            continue
    if applied:
        logging.info("Applied %s saved cookies for %s", applied, account.get("email"))
    return applied


def _capture_screenshot(driver, email: str) -> Optional[str]:
    try:
        _ensure_storage()
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", email)
        path = os.path.join(SCREENSHOTS_DIR, f"{safe}_{int(time.time())}.png")
        driver.save_screenshot(path)
        return path
    except Exception as exc:  # noqa: BLE001
        logging.warning("Screenshot failed: %s", _format_exc(exc))
        return None


def _page_text(driver) -> str:
    try:
        return ((driver.title or "") + " " + (driver.page_source or "")).lower()
    except Exception:  # noqa: BLE001
        return ""


def _is_insecure_browser_block(driver) -> bool:
    text = _page_text(driver)
    return (
        "couldn't sign you in" in text
        or "this browser or app may not be secure" in text
        or "browser or app may not be secure" in text
    )


def _has_real_captcha_challenge(driver) -> bool:
    """Detect a *visible* CAPTCHA challenge.

    Google preloads a hidden ``<img alt="CAPTCHA image ...">`` (with no ``src``)
    on the ordinary sign-in page. Treating that as a challenge made the bot ask
    the user for a code that did not exist, so the flow always ended in
    "Cancelled". We now require the widget to be actually displayed and, for a
    text CAPTCHA, to be paired with a visible text input.
    """
    try:
        from selenium.webdriver.common.by import By

        if _is_insecure_browser_block(driver):
            return False

        current = (driver.current_url or "").lower()
        # Google's real "unusual traffic" interstitial.
        if "/sorry/" in current or "recaptcha/api" in current:
            return True

        # A visible reCAPTCHA challenge frame (the checkbox is ~76px tall).
        for frame in driver.find_elements(
            By.CSS_SELECTOR,
            "iframe[src*='recaptcha'], iframe[title*='recaptcha' i], iframe[src*='captcha']",
        ):
            try:
                if not frame.is_displayed():
                    continue
                if (frame.size or {}).get("height", 0) >= 60:
                    return True
            except Exception:  # noqa: BLE001
                continue

        # A visible text CAPTCHA image must come with a visible text box.
        visible_image = False
        for image in driver.find_elements(
            By.CSS_SELECTOR, "img[src*='captcha'], img[alt*='captcha' i]"
        ):
            try:
                if image.is_displayed() and (image.get_attribute("src") or "").strip():
                    visible_image = True
                    break
            except Exception:  # noqa: BLE001
                continue
        if not visible_image:
            return False

        for field in driver.find_elements(
            By.CSS_SELECTOR,
            "input[type='text'], input[name*='captcha' i], input[id*='captcha' i]",
        ):
            try:
                if field.is_displayed() and field.is_enabled():
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False
    except Exception:  # noqa: BLE001
        return False


def _describe_verification_challenge(driver) -> tuple[str, str]:
    """Return ``(kind, prompt)`` describing the current Google challenge page.

    ``kind`` is one of ``phone`` (approve on the phone), ``code`` (type a code),
    ``tap`` (tap a number on the phone) or ``generic``.
    """
    text = _page_text(driver)

    if "check your phone" in text or "tap yes" in text or "notification" in text and "phone" in text:
        return (
            "phone",
            "Google أرسل إشعاراً إلى هاتفك. افتح الإشعار واضغط Yes، ثم اضغط الرقم الظاهر على الشاشة، "
            "وبعد أن يكتمل التحقق اضغط زر «تم التأكيد» بالأسفل.",
        )
    if "tap the number" in text or "tap the matching number" in text or "choose the number" in text:
        return (
            "tap",
            "Google يطلب تأكيداً على هاتفك: اضغط الرقم المطابق في هاتفك، ثم اضغط «تم التأكيد».",
        )
    if "enter the code" in text or "verification code" in text or "get a verification code" in text:
        return (
            "code",
            "أرسل رمز التحقق الذي وصلك (بريد/هاتف) هنا، أو اضغط «تم التأكيد» بعد إتمام التحقق.",
        )
    if "2-step" in text or "two-step" in text or "authenticator" in text:
        return ("code", "أدخل رمز المصادقة الثنائية (2FA) هنا.")
    return (
        "generic",
        "Google يطلب تحققاً إضافياً. اتبع التعليمات الظاهرة في الصورة، ثم اضغط «تم التأكيد».",
    )


def extract_and_save_profile_names(driver, account: dict) -> tuple[str, str]:
    first_name = (account.get("first_name") or "").strip()
    last_name = (account.get("last_name") or "").strip()

    try:
        driver.get("https://myaccount.google.com/profile/name?hl=en")
        time.sleep(3)
        inputs = driver.find_elements("css selector", "input[type='text']")
        values = [i.get_attribute("value").strip() for i in inputs if i.get_attribute("value")]
        if len(values) >= 2:
            first_name, last_name = values[0], values[1]
            update_account(account["email"], first_name=first_name, last_name=last_name)
            return first_name, last_name
        if len(values) == 1 and " " in values[0]:
            parts = values[0].split(None, 1)
            first_name, last_name = parts[0], parts[1]
            update_account(account["email"], first_name=first_name, last_name=last_name)
            return first_name, last_name
    except Exception:  # noqa: BLE001
        pass

    try:
        driver.get("https://myaccount.google.com/?hl=en")
        time.sleep(3)
        headings = driver.find_elements("css selector", "h1, [role='heading']")
        for heading in headings:
            text = (heading.text or "").strip()
            if text and "@" not in text and len(text.split()) >= 2 and len(text) < 60:
                parts = text.split(None, 1)
                first_name, last_name = parts[0], parts[1]
                update_account(account["email"], first_name=first_name, last_name=last_name)
                return first_name, last_name
    except Exception:  # noqa: BLE001
        pass

    if not first_name or not last_name:
        inferred_first, inferred_last = infer_name_from_email(account["email"])
        first_name = first_name or inferred_first
        last_name = last_name or inferred_last
        update_account(account["email"], first_name=first_name, last_name=last_name)

    return first_name, last_name


async def _interactive_google_login(driver, account: dict, challenge_callback=None, timeout: int = 300) -> tuple[bool, str]:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    # Restore imported cookies before touching the login page.
    if account.get("cookies"):
        _apply_saved_cookies(driver, account)

    driver.get("https://myaccount.google.com/?hl=en")
    await asyncio.sleep(4)

    current = driver.current_url or ""
    if "myaccount.google.com" in current and "signin" not in current and "ServiceLogin" not in current:
        extract_and_save_profile_names(driver, account)
        update_account(account["email"], logged_in=True, last_status="Session active")
        return True, "Logged in via saved session"

    if _is_insecure_browser_block(driver):
        update_account(account["email"], last_status="Blocked: Browser/IP not trusted by Google")
        return False, "Google blocked sign-in ('This browser or app may not be secure'). Use a trusted session profile or residential IP."

    driver.get("https://accounts.google.com/signin/v2/identifier?hl=en")
    try:
        email_input = WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.ID, "identifierId")))
        email_input.clear()
        email_input.send_keys(account["email"])
        email_input.send_keys(Keys.ENTER)
        await asyncio.sleep(3)
    except Exception as exc:  # noqa: BLE001
        return False, f"Failed to enter email: {_format_exc(exc)}"

    page = _page_text(driver)
    if "couldn't find your google account" in page or "enter a valid email" in page:
        return False, "Google rejected the email address"
    if _is_insecure_browser_block(driver):
        update_account(account["email"], last_status="Blocked: Browser/IP not trusted by Google")
        return False, "Google blocked sign-in ('This browser or app may not be secure')."

    if _has_real_captcha_challenge(driver) and challenge_callback:
        shot = _capture_screenshot(driver, account["email"])
        code = await challenge_callback(
            account["email"],
            "captcha",
            shot,
            "CAPTCHA challenge detected. Enter verification text/code or /cancel:",
        )
        if not code or code.lower() == "/cancel":
            return False, "Cancelled by user"
        try:
            captcha_box = driver.find_element(By.CSS_SELECTOR, "input[type='text']")
            captcha_box.send_keys(code)
            captcha_box.send_keys(Keys.ENTER)
            await asyncio.sleep(3)
        except Exception:  # noqa: BLE001
            pass

    try:
        password_input = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[name='Passwd'], input[type='password']"))
        )
        password = (account.get("password") or "").strip()
        if not password and challenge_callback:
            shot = _capture_screenshot(driver, account["email"])
            reply = await challenge_callback(
                account["email"],
                "password",
                shot,
                "Enter the Google account password (or /cancel):",
            )
            if reply and reply.lower() != "/cancel" and reply.lower() not in ("done", "ok"):
                password = reply.strip()
        if not password:
            return False, "Account password not provided"
        password_input.clear()
        password_input.send_keys(password)
        password_input.send_keys(Keys.ENTER)
        await asyncio.sleep(4)
    except Exception:  # noqa: BLE001
        pass

    notified_2fa = False
    started = time.time()
    while time.time() - started < timeout:
        if _is_insecure_browser_block(driver):
            update_account(account["email"], last_status="Blocked: Browser/IP not trusted by Google")
            return False, "Google blocked sign-in ('This browser or app may not be secure')."

        current = driver.current_url or ""

        if "rejected" in current or "/denied" in current:
            update_account(account["email"], last_status="Verification rejected by Google")
            return False, (
                "Google رفض التحقق: لم يتم الضغط على Yes في الهاتف خلال المهلة. "
                "أعد المحاولة وأكمل التأكيد من هاتفك بسرعة."
            )

        if any(domain in current for domain in ("myaccount.google.com", "mail.google.com", "support.google.com")):
            if "signin" not in current and "challenge" not in current:
                extract_and_save_profile_names(driver, account)
                update_account(account["email"], logged_in=True, last_status="Logged in and session saved")
                return True, "Logged in successfully"

        if "challenge" in current and not notified_2fa and challenge_callback:
            notified_2fa = True
            kind, prompt = _describe_verification_challenge(driver)
            shot = _capture_screenshot(driver, account["email"])
            reply = await challenge_callback(
                account["email"],
                "2fa",
                shot,
                prompt,
            )
            if reply and reply.lower() == "/cancel":
                return False, "2FA cancelled"
            if reply and reply.lower() not in ("done", "ok"):
                # Only type when the page really exposes an input field.
                try:
                    candidates = driver.find_elements(
                        By.CSS_SELECTOR,
                        "input[type='tel'], input[type='text'], input[type='number'], input[name*='code' i]",
                    )
                    for code_input in candidates:
                        if not code_input.is_displayed() or not code_input.is_enabled():
                            continue
                        code_input.clear()
                        code_input.send_keys(reply.strip())
                        code_input.send_keys(Keys.ENTER)
                        break
                except Exception:  # noqa: BLE001
                    pass
            await asyncio.sleep(4)

        await asyncio.sleep(3)

    return False, "2FA or verification timeout"


async def send_via_gethelp_form(account: dict, body_text: str, challenge_callback=None) -> tuple[bool, str]:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import Select, WebDriverWait

    driver = None

    def _safe_click(element) -> None:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
        time.sleep(0.2)
        try:
            element.click()
        except Exception:  # noqa: BLE001
            driver.execute_script("arguments[0].click();", element)

    def _safe_type(element, value: str) -> None:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
        time.sleep(0.2)
        try:
            element.click()
        except Exception:  # noqa: BLE001
            driver.execute_script("arguments[0].click();", element)
        try:
            element.clear()
        except Exception:  # noqa: BLE001
            pass
        try:
            element.send_keys(value)
        except Exception:  # noqa: BLE001
            driver.execute_script(
                "arguments[0].value = arguments[1];"
                "arguments[0].dispatchEvent(new Event('input', {bubbles: true}));"
                "arguments[0].dispatchEvent(new Event('change', {bubbles: true}));",
                element,
                value,
            )

    def _visible(elements):
        result = []
        for element in elements:
            try:
                if element.is_displayed() and element.is_enabled():
                    result.append(element)
            except Exception:  # noqa: BLE001
                pass
        return result

    def _click_text_match(phrases) -> bool:
        for element in driver.find_elements(By.XPATH, "//button | //div[@role='button'] | //span | //label | //a"):
            try:
                if not element.is_displayed():
                    continue
                text = (element.text or "").strip().lower()
                if not text or len(text) > 90:
                    continue
                if any(phrase in text for phrase in phrases):
                    _safe_click(element)
                    return True
            except Exception:  # noqa: BLE001
                pass
        return False

    account = await ensure_account_session_identity(account)
    try:
        driver = _build_chrome_driver(account)
        ok, message = await _interactive_google_login(driver, account, challenge_callback)
        if not ok:
            return False, message

        first_name, last_name = extract_and_save_profile_names(driver, account)

        driver.get(f"{GETHELP_URL}?hl=en")
        await asyncio.sleep(3.5)

        try:
            step1_inputs = WebDriverWait(driver, 15).until(
                lambda d: _visible(d.find_elements(By.CSS_SELECTOR, "input[type='text'], input:not([type])"))
            )
            if step1_inputs:
                _safe_type(step1_inputs[0], "Play point issues")
                try:
                    step1_inputs[0].send_keys(Keys.ENTER)
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(1.2)
                _click_text_match(["next step", "next", "التالي"])
                await asyncio.sleep(2.0)
        except Exception:  # noqa: BLE001
            pass

        if not _click_text_match(["play points issue", "play points", "نقاط play"]):
            _click_text_match(["other", "غير ذلك"])
        await asyncio.sleep(1.0)

        for select_element in driver.find_elements(By.TAG_NAME, "select"):
            try:
                if not select_element.is_displayed() or not select_element.is_enabled():
                    continue
                selector = Select(select_element)
                picked = False
                for option in selector.options:
                    option_text = (option.text or "").strip().lower()
                    if "google play apps" in option_text or "play pass" in option_text:
                        selector.select_by_visible_text(option.text)
                        picked = True
                        break
                if not picked and len(selector.options) > 1:
                    selector.select_by_index(1)
            except Exception:  # noqa: BLE001
                pass
        await asyncio.sleep(0.8)

        for _ in range(4):
            if _visible(driver.find_elements(By.CSS_SELECTOR, "textarea")):
                break
            if not _click_text_match(["next step", "next", "التالي"]):
                break
            await asyncio.sleep(1.5)

        _click_text_match(["email", "البريد الإلكتروني"])
        await asyncio.sleep(1.5)

        if not _visible(driver.find_elements(By.CSS_SELECTOR, "textarea")) and not _visible(
            driver.find_elements(By.CSS_SELECTOR, "input[type='text'], input:not([type])")
        ):
            _click_text_match(["next step", "next", "التالي"])
            await asyncio.sleep(1.5)

        for email_input in _visible(driver.find_elements(By.CSS_SELECTOR, "input[type='email']")):
            if not (email_input.get_attribute("value") or "").strip():
                _safe_type(email_input, account["email"])

        visible_inputs = _visible(driver.find_elements(By.CSS_SELECTOR, "input[type='text'], input:not([type])"))
        for index, element in enumerate(visible_inputs):
            current_value = (element.get_attribute("value") or "").strip()
            label = ((element.get_attribute("aria-label") or "") + " " + (element.get_attribute("placeholder") or "")).lower()
            try:
                label += " " + (
                    element.find_element(By.XPATH, "./ancestor::*[self::div or self::section][1]").text or ""
                ).lower()
            except Exception:  # noqa: BLE001
                pass
            if "transaction id" in label or "order id" in label or "gpa." in label:
                _safe_type(element, "N/A")
            elif index == 0 and not current_value:
                _safe_type(element, first_name)
            elif index == 1 and not current_value:
                _safe_type(element, last_name)
            elif not current_value:
                _safe_type(element, "N/A")

        visible_areas = _visible(driver.find_elements(By.CSS_SELECTOR, "textarea"))
        if not visible_areas:
            _capture_screenshot(driver, account["email"])
            return False, "Support form fields did not load"

        _safe_type(visible_areas[0], body_text)
        await asyncio.sleep(0.8)

        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        await asyncio.sleep(0.6)

        submitted = _click_text_match(["submit", "send", "إرسال"])
        if not submitted:
            for css in (
                "button[type='submit']",
                "input[type='submit']",
                "[aria-label*='submit' i]",
                "[aria-label*='send' i]",
                "[aria-label*='إرسال' i]",
            ):
                found = _visible(driver.find_elements(By.CSS_SELECTOR, css))
                if found:
                    _safe_click(found[0])
                    submitted = True
                    break

        if not submitted:
            _capture_screenshot(driver, account["email"])
            return False, "Submit button not found on support form"

        await asyncio.sleep(3.5)
        source = (driver.page_source or "").lower()
        if "we couldn't submit your form yet" in source or "please fix the fields above" in source:
            _capture_screenshot(driver, account["email"])
            return False, "Form validation error on submit"

        return True, "Submitted via Google Play GetHelp"
    except Exception as exc:  # noqa: BLE001
        logging.error("GetHelp form error: %s", _format_exc(exc))
        return False, f"GetHelp form error: {_format_exc(exc)}"
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:  # noqa: BLE001
                pass


async def send_via_gmail_web(account: dict, to_email: str, subject: str, body: str, challenge_callback=None) -> tuple[bool, str]:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    account = await ensure_account_session_identity(account)
    driver = None
    try:
        driver = _build_chrome_driver(account)
        ok, message = await _interactive_google_login(driver, account, challenge_callback)
        if not ok:
            return False, message

        driver.get("https://mail.google.com/mail/u/0/?hl=en")
        await asyncio.sleep(5)

        compose = WebDriverWait(driver, 25).until(
            EC.element_to_be_clickable((By.XPATH, "//div[text()='Compose'] | //div[@role='button' and @gh='cm']"))
        )
        compose.click()
        await asyncio.sleep(2)

        to_box = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "input[aria-label*='To'], input[peoplekit-id], textarea[aria-label*='To']")
            )
        )
        to_box.send_keys(to_email)
        to_box.send_keys(Keys.ENTER)
        await asyncio.sleep(1)

        subject_box = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[name='subjectbox'], input[aria-label*='Subject']"))
        )
        subject_box.send_keys(subject)

        body_box = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "div[aria-label='Message Body'], div[aria-label*='Message body']")
            )
        )
        body_box.send_keys(body)
        await asyncio.sleep(1)

        send_button = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.XPATH, "//div[text()='Send'] | //div[@role='button' and @aria-label*='Send']"))
        )
        send_button.click()
        await asyncio.sleep(4)
        return True, "Sent via Gmail Web"
    except Exception as exc:  # noqa: BLE001
        logging.error("Gmail web error: %s", _format_exc(exc))
        return False, _format_exc(exc)
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def _can_use_web_form(account: dict) -> bool:
    """True when the account has enough data to drive the Google web form."""
    return bool(
        account.get("logged_in")
        or account.get("password")
        or account.get("cookies")
    )


async def dispatch_report(
    account: dict,
    subject: str,
    body: str,
    method: str = "auto",
    challenge_callback=None,
) -> tuple[bool, str, str]:
    settings = load_settings()
    target_email = settings.get("target_email", GOOGLE_PLAY_SUPPORT)
    has_smtp = bool((account.get("app_password") or "").replace(" ", "").strip())

    async def _smtp_fallback(reason: str) -> tuple[bool, str, str]:
        if has_smtp:
            ok, info = await asyncio.to_thread(send_via_smtp, account, target_email, subject, body)
            if ok:
                return True, info, "smtp"
            return False, f"{reason} | {info}", method
        return False, reason, method

    if method == "web_form":
        ok, info = await send_via_gethelp_form(account, body, challenge_callback)
        return (True, info, "web_form") if ok else await _smtp_fallback(info)

    if method == "smtp":
        ok, info = await asyncio.to_thread(send_via_smtp, account, target_email, subject, body)
        if ok:
            return True, info, "smtp"
        # Railway/PaaS block outbound SMTP. When the failure is the firewall and
        # we have a browser session, fall back to the official Google web form.
        if _is_network_blocked(info) and _can_use_web_form(account):
            ok2, info2 = await send_via_gethelp_form(account, body, challenge_callback)
            if ok2:
                return True, info2, "web_form"
            return False, f"{info} | {info2}", "smtp"
        return False, info, "smtp"

    if method == "gmail_web":
        ok, info = await send_via_gmail_web(account, target_email, subject, body, challenge_callback)
        return (True, info, "gmail_web") if ok else await _smtp_fallback(info)

    # auto: prefer an authenticated browser session, otherwise SMTP.
    if account.get("logged_in") or account.get("password") or account.get("cookies"):
        ok, info = await send_via_gethelp_form(account, body, challenge_callback)
        if ok:
            return True, info, "web_form"
        return await _smtp_fallback(info)
    if has_smtp:
        ok, info = await asyncio.to_thread(send_via_smtp, account, target_email, subject, body)
        if ok:
            return True, info, "smtp"
        if _is_network_blocked(info) and _can_use_web_form(account):
            ok2, info2 = await send_via_gethelp_form(account, body, challenge_callback)
            if ok2:
                return True, info2, "web_form"
            return False, f"{info} | {info2}", "smtp"
        return False, info, "smtp"
    return False, "No valid credentials for account", method
