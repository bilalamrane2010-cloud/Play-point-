import asyncio
import json
import logging
import os
import random
import shutil
import smtplib
import subprocess
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

from openai import AsyncOpenAI

GOOGLE_PLAY_SUPPORT = "googleplay-support@google.com"
GETHELP_URL = "https://support.google.com/googleplay/gethelp"

DATA_DIR = "bot_data"
ACCOUNTS_FILE = os.path.join(DATA_DIR, "accounts.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
STATS_FILE = os.path.join(DATA_DIR, "stats.json")
PROFILES_DIR = os.path.join(DATA_DIR, "chrome_profiles")
SCREENSHOTS_DIR = os.path.join(DATA_DIR, "screenshots")

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
        "screen_res": "412x915",
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
        "screen_res": "412x915",
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
        "screen_res": "393x873",
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
        "screen_res": "412x915",
        "timezone": "Africa/Algiers",
        "locale": "en-US",
        "ip_address": "105.104.87.32",
        "user_agent": "Mozilla/5.0 (Linux; Android 15; PJZ110) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.6723.106 Mobile Safari/537.36",
    },
]

_BROWSER_LOCK = asyncio.Lock()


def _format_exc(exc):
    raw = (str(exc) or "").strip()
    if "Stacktrace:" in raw:
        raw = raw.split("Stacktrace:", 1)[0].strip()
    raw = raw.replace("Message:", "", 1).strip() if raw.startswith("Message:") else raw
    if not raw:
        return f"{type(exc).__name__}"
    return f"{type(exc).__name__}: {raw}" if not raw.startswith(type(exc).__name__) else raw


def _is_browser_crash_error(err_or_exc):
    msg = str(err_or_exc).lower()
    return any(
        token in msg
        for token in (
            "tab crashed",
            "target page, context or browser has been closed",
            "target closed",
            "browser has been closed",
            "target frame detached",
            "disconnected",
            "session deleted because of page crash",
            "chrome not reachable",
            "renderer process crashed",
            "connection refused",
            "maxretryerror",
        )
    )


def _get_ai_client_and_model():
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None, None
    base_url = os.getenv("OPENAI_BASE_URL", "").strip()
    if not base_url and api_key.lower().startswith("gsk_"):
        base_url = "https://api.groq.com/openai/v1"
    default_model = "llama-3.3-70b-versatile" if (base_url and "groq.com" in base_url) else "gpt-4o-mini"
    model = os.getenv("OPENAI_MODEL", "").strip() or default_model
    client = AsyncOpenAI(api_key=api_key, base_url=base_url or None)
    return client, model


def _ensure_storage():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(PROFILES_DIR, exist_ok=True)
    os.makedirs(SCREENSHOTS_DIR, exist_ok=True)


def _ensure_file(path, default):
    _ensure_storage()
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default, f, ensure_ascii=False, indent=2)


def infer_name_from_email(email):
    local = email.split("@")[0].replace(".", " ").replace("_", " ").replace("-", " ").strip()
    parts = [p.capitalize() for p in local.split() if p.isalpha()]
    if len(parts) >= 2:
        return parts[0], " ".join(parts[1:])
    if len(parts) == 1:
        return parts[0], "User"
    return (local.capitalize() or "Play"), "User"


def load_accounts():
    _ensure_file(ACCOUNTS_FILE, [])
    with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_accounts(accounts):
    _ensure_storage()
    tmp_path = f"{ACCOUNTS_FILE}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(accounts, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, ACCOUNTS_FILE)


def get_account(email):
    for a in load_accounts():
        if a["email"].lower() == email.lower():
            return a
    return None


def add_account(
    email,
    first_name="",
    last_name="",
    app_password="",
    password="",
    auth_type="email_code",
    device_name="",
    device_id="",
    ip_address="",
    user_agent="",
    timezone="",
    locale="",
    screen_res="",
    android_version="",
    build_id="",
):
    email_clean = email.strip()
    accounts = load_accounts()
    if any(a["email"].lower() == email_clean.lower() for a in accounts):
        return False

    inf_first, inf_last = infer_name_from_email(email_clean)
    fn = first_name.strip() or inf_first
    ln = last_name.strip() or inf_last

    base_dev = random.choice(REAL_DEVICES_POOL).copy()
    prof_slug = email_clean.replace("@", "_at_").replace(".", "_")
    accounts.append({
        "email": email_clean,
        "first_name": fn,
        "last_name": ln,
        "app_password": app_password.strip(),
        "password": password.strip(),
        "auth_type": auth_type,
        "logged_in": False,
        "cookies": [],
        "profile_dir": os.path.join(PROFILES_DIR, prof_slug),
        "device_name": device_name.strip() or base_dev["device_name"],
        "device_id": device_id.strip() or f"DEV-{random.randint(100000, 999999)}",
        "ip_address": ip_address.strip() or os.getenv("DEVICE_IP", "").strip() or base_dev["ip_address"],
        "user_agent": user_agent.strip() or base_dev["user_agent"],
        "timezone": timezone.strip() or base_dev["timezone"],
        "locale": locale.strip() or base_dev["locale"],
        "screen_res": screen_res.strip() or base_dev["screen_res"],
        "android_version": android_version.strip() or base_dev["android_version"],
        "build_id": build_id.strip() or base_dev["build_id"],
        "identity_updated_at": time.time(),
        "added_at": time.time(),
        "sent": 0,
        "last_status": "جديد",
    })
    save_accounts(accounts)
    return True


def remove_account(email):
    accounts = load_accounts()
    new_list = [a for a in accounts if a["email"].lower() != email.lower()]
    if len(new_list) == len(accounts):
        return False
    save_accounts(new_list)
    return True


def update_account(email, **kwargs):
    accounts = load_accounts()
    for a in accounts:
        if a["email"].lower() == email.lower():
            a.update(kwargs)
            save_accounts(accounts)
            return True
    return False


def import_cookies_for_account(email, raw_json_str):
    try:
        parsed = json.loads(raw_json_str)
        if isinstance(parsed, dict) and "cookies" in parsed:
            parsed = parsed["cookies"]
        if not isinstance(parsed, list) or not parsed:
            return False, "صيغة الكوكيز غير صالحة"
        cleaned = []
        for c in parsed:
            if not isinstance(c, dict) or not c.get("name") or not c.get("value"):
                continue
            same_site = str(c.get("sameSite") or "Lax").capitalize()
            if same_site not in ("Strict", "Lax", "None"):
                same_site = "Lax"
            item = {
                "name": str(c["name"]),
                "value": str(c["value"]),
                "domain": str(c.get("domain") or ".google.com"),
                "path": str(c.get("path") or "/"),
                "secure": bool(c.get("secure", True)),
                "httpOnly": bool(c.get("httpOnly", False)),
                "sameSite": same_site,
            }
            if c.get("expirationDate"):
                try:
                    item["expires"] = float(c["expirationDate"])
                except Exception:
                    pass
            cleaned.append(item)
        if not cleaned:
            return False, "لا توجد كوكيز صالحة داخل الملف"
        ok = update_account(email, cookies=cleaned, logged_in=True, last_status="جلسة مستوردة ✅")
        return (True, f"تم استيراد {len(cleaned)} كوكيز بنجاح") if ok else (False, "الحساب غير موجود")
    except Exception as e:
        return False, f"خطأ في قراءة JSON: {_format_exc(e)}"


def load_settings():
    _ensure_file(SETTINGS_FILE, {
        "auto_send_enabled": False,
        "auto_send_interval": 60,
        "send_method": "auto",
        "min_delay": 25,
        "max_delay": 90,
        "target_email": GOOGLE_PLAY_SUPPORT,
        "base_template": BASE_REPORT_TEMPLATE,
        "rotate_hours": 1,
        "auto_login_on_add": True,
        "smtp_fallback_enabled": True,
    })
    with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_settings(s):
    _ensure_storage()
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)


def load_stats():
    _ensure_file(STATS_FILE, {"total_sent": 0, "failed": 0, "history": []})
    with open(STATS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_stats(s):
    _ensure_storage()
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)


def record_stat(email, success, channel, subject):
    s = load_stats()
    if success:
        s["total_sent"] = s.get("total_sent", 0) + 1
    else:
        s["failed"] = s.get("failed", 0) + 1
    s.setdefault("history", []).append({
        "email": email,
        "success": success,
        "channel": channel,
        "subject": subject,
        "time": time.time(),
    })
    s["history"] = s["history"][-250:]
    save_stats(s)


async def generate_ai_device_identity():
    client, model = _get_ai_client_and_model()
    item = random.choice(REAL_DEVICES_POOL).copy()
    item["device_id"] = f"DEV-{random.randint(100000, 999999)}"
    item["ip_address"] = os.getenv("DEVICE_IP", "").strip() or item.get("ip_address", "105.104.22.14")
    if not client:
        return item

    prompt = (
        "Generate a realistic mobile device profile as valid JSON with keys: "
        "device_name, device_id, android_version, build_id, screen_res, timezone, locale, ip_address, user_agent. "
        "Use real-world phone names (Samsung Galaxy S24 Ultra, Google Pixel 9 Pro XL, Redmi Note 12 Pro 5G, OnePlus 13). "
        "Output JSON only."
    )
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.85,
            max_tokens=260,
        )
        raw = resp.choices[0].message.content.strip().replace("```json", "").replace("```", "").strip()
        data = json.loads(raw)
        for k in ("device_name", "device_id", "android_version", "build_id", "screen_res", "timezone", "locale", "ip_address", "user_agent"):
            if data.get(k) and str(data[k]).strip():
                item[k] = str(data[k]).strip()
        return item
    except Exception as e:
        logging.error(f"AI device identity error: {_format_exc(e)}")
        return item


async def ensure_account_session_identity(account, force_rotate=False):
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
        or (now - account.get("identity_updated_at", 0) >= rotate_sec)
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


async def generate_report():
    settings = load_settings()
    template = settings.get("base_template", BASE_REPORT_TEMPLATE)
    client, model = _get_ai_client_and_model()

    if not client:
        parts = template.split("\n", 1)
        subject = parts[0].replace("Subject:", "").strip()
        body = parts[1].strip() if len(parts) > 1 else template
        return {"subject": subject, "body": body}

    prompt = (
        "You are a strict text rephraser with zero creative freedom. "
        "Rewrite ONLY the provided appeal text using fresh phrasing and sentence structure while staying strictly bound to these exact points and nothing outside them: "
        "1) Requesting a manual review of a restriction on the Google Play Points account due to flagged suspicious activity while traveling. "
        "2) Accessing the account from different international locations and Wi-Fi networks during travel caused a false-positive security flag. "
        "3) Confirming sole ownership of the account and that all points and transactions were earned legitimately under Google Play Terms of Service with no violations. "
        "4) Politely asking to review and reinstate Play Points access. "
        "STRICT RULES: Do NOT add new facts, apologies, promises, order IDs, dates, amounts, personal details, or outside topics. "
        "Do NOT omit the core appeal points. Output in English only.\n\n"
        f"ORIGINAL TEXT:\n{template}\n\n"
        "Return strictly in this format:\n"
        "SUBJECT: <rephrased subject>\n"
        "BODY:\n<rephrased body>"
    )
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=1.0,
            max_tokens=750,
        )
        text = resp.choices[0].message.content.strip()
        subject = "Formal Appeal: Play Points Account Restriction Review"
        body = text
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if line.upper().startswith("SUBJECT:"):
                subject = line.split(":", 1)[1].strip()
            elif line.upper().startswith("BODY:"):
                body = "\n".join(lines[i + 1:]).strip()
                break
        return {"subject": subject, "body": body}
    except Exception as e:
        logging.error(f"AI report generation failed: {_format_exc(e)}")
        parts = template.split("\n", 1)
        subject = parts[0].replace("Subject:", "").strip()
        body = parts[1].strip() if len(parts) > 1 else template
        return {"subject": subject, "body": body}


def send_via_smtp(account, to_email, subject, body):
    app_pw = (account.get("app_password") or "").replace(" ", "").strip()
    if not app_pw:
        return False, "Missing App Password"
    msg = MIMEMultipart()
    sender_name = f"{account.get('first_name', '')} {account.get('last_name', '')}".strip() or "Google Play User"
    msg["From"] = formataddr((sender_name, account["email"]))
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(account["email"], app_pw)
            server.send_message(msg)
        return True, "Sent via Email (SMTP)"
    except Exception as e:
        err = _format_exc(e)
        logging.error(f"SMTP error: {err}")
        return False, err


def test_smtp(account):
    app_pw = (account.get("app_password") or "").replace(" ", "").strip()
    if not app_pw:
        return False, "Missing App Password"
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(account["email"], app_pw)
        return True, "OK"
    except Exception as e:
        return False, _format_exc(e)


def _resolve_chrome_binary():
    candidates = [
        os.getenv("CHROME_BIN", "").strip(),
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def _parse_resolution(res_str):
    try:
        w_str, h_str = str(res_str).lower().split("x", 1)
        w, h = int(w_str.strip()), int(h_str.strip())
        if 300 <= w <= 2560 and 500 <= h <= 4000:
            if w > 600:
                return 412, 915
            return w, h
    except Exception:
        pass
    return 412, 915


def _clean_stale_profile_locks(prof_dir):
    for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        lp = os.path.join(prof_dir, lock_name)
        try:
            if os.path.lexists(lp):
                os.remove(lp)
        except Exception:
            pass


def _cleanup_stray_browser_processes():
    try:
        subprocess.run(
            ["pkill", "-9", "-f", "chromium|chrome|chromedriver"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except Exception:
        pass


def _get_proxy_url():
    for env_key in ("DEVICE_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        val = (os.getenv(env_key) or "").strip()
        if val:
            return val
    return ""


async def _launch_playwright_context(pw, account):
    prof_dir = account.get("profile_dir") or os.path.join(
        PROFILES_DIR, account["email"].replace("@", "_at_").replace(".", "_")
    )
    os.makedirs(prof_dir, exist_ok=True)
    _clean_stale_profile_locks(prof_dir)

    width, height = _parse_resolution(account.get("screen_res", "412x915"))
    ua = account.get("user_agent") or (
        "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.6778.135 Mobile Safari/537.36"
    )
    locale = account.get("locale") or "en-US"
    tz = account.get("timezone") or "Africa/Algiers"
    dev_name = account.get("device_name") or "Samsung Galaxy S24 Ultra"
    dev_id = account.get("device_id") or "DEV-000000"
    ip_addr = account.get("ip_address") or "105.104.22.14"
    proxy_url = _get_proxy_url()
    chrome_bin = _resolve_chrome_binary()

    launch_args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-software-rasterizer",
        "--disable-blink-features=AutomationControlled",
        "--disable-features=IsolateOrigins,site-per-process,Translate,BackForwardCache",
        "--disable-background-networking",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        "--disable-breakpad",
        "--disable-extensions",
        "--disable-sync",
        "--disable-default-apps",
        "--disable-component-update",
        "--no-first-run",
        "--no-default-browser-check",
        "--mute-audio",
        "--js-flags=--max-old-space-size=256",
    ]

    kwargs = {
        "user_data_dir": prof_dir,
        "headless": True,
        "args": launch_args,
        "user_agent": ua,
        "viewport": {"width": width, "height": height},
        "device_scale_factor": 2.625,
        "is_mobile": True,
        "has_touch": True,
        "locale": locale,
        "timezone_id": tz,
        "ignore_https_errors": True,
    }
    if chrome_bin:
        kwargs["executable_path"] = chrome_bin
    if proxy_url:
        kwargs["proxy"] = {"server": proxy_url}

    context = await pw.chromium.launch_persistent_context(**kwargs)

    await context.add_init_script(
        f"""
        Object.defineProperty(navigator, 'webdriver', {{ get: () => undefined }});
        Object.defineProperty(navigator, 'platform', {{ get: () => 'Linux armv8l' }});
        Object.defineProperty(navigator, 'hardwareConcurrency', {{ get: () => 8 }});
        Object.defineProperty(navigator, 'deviceMemory', {{ get: () => 8 }});
        window.chrome = window.chrome || {{ runtime: {{}} }};
        window.__DEVICE_IDENTITY__ = {{
            deviceName: {json.dumps(dev_name)},
            deviceId: {json.dumps(dev_id)},
            ipAddress: {json.dumps(ip_addr)}
        }};
        """
    )

    async def _route_interceptor(route):
        rtype = route.request.resource_type
        url = route.request.url.lower()
        if rtype in ("media", "font") or any(
            bad in url for bad in ("doubleclick.net", "google-analytics.com", "googletagmanager.com")
        ):
            await route.abort()
        else:
            await route.continue_()

    await context.route("**/*", _route_interceptor)

    saved_cookies = account.get("cookies") or []
    if saved_cookies:
        try:
            await context.add_cookies(saved_cookies)
        except Exception:
            pass

    page = context.pages[0] if context.pages else await context.new_page()
    page.set_default_timeout(20000)
    page.set_default_navigation_timeout(25000)
    return context, page


async def _save_context_cookies(context, account):
    try:
        cookies = await context.cookies()
        google_cookies = [c for c in cookies if "google" in (c.get("domain") or "")]
        if google_cookies:
            update_account(account["email"], cookies=google_cookies)
    except Exception:
        pass


async def _capture_pw_screenshot(page, email):
    _ensure_storage()
    fname = f"{email.replace('@', '_')}_{int(time.time())}.png"
    path = os.path.join(SCREENSHOTS_DIR, fname)
    try:
        await page.screenshot(path=path, full_page=False)
        return path
    except Exception:
        return None


async def _is_insecure_browser_block_pw(page):
    try:
        title = (await page.title()) or ""
        body_txt = await page.inner_text("body")
        txt = f"{title} {body_txt}".lower()
        return (
            "couldn't sign you in" in txt
            or "this browser or app may not be secure" in txt
            or "browser or app may not be secure" in txt
            or "try using a different browser" in txt
        )
    except Exception:
        return False


async def _has_real_captcha_pw(page):
    try:
        if await _is_insecure_browser_block_pw(page):
            return False
        pwd_loc = page.locator("input[type='password'], input[name='Passwd']")
        if await pwd_loc.count() > 0 and await pwd_loc.first.is_visible():
            return False
        cur = (page.url or "").lower()
        if "recaptcha" in cur or "captcha" in cur:
            return True
        cap_frame = page.locator("iframe[src*='recaptcha'], iframe[title*='recaptcha' i], iframe[src*='captcha']")
        if await cap_frame.count() > 0 and await cap_frame.first.is_visible():
            return True
        cap_img = page.locator("img[src*='captcha'], img[alt*='captcha' i], img[src*='challenge']")
        if await cap_img.count() > 0 and await cap_img.first.is_visible():
            return True
        return False
    except Exception:
        return False


async def _extract_profile_names_pw(page, account):
    first_name = (account.get("first_name") or "").strip()
    last_name = (account.get("last_name") or "").strip()
    if first_name and last_name and first_name != "Play":
        return first_name, last_name

    try:
        await page.goto("https://myaccount.google.com/profile/name?hl=en", wait_until="domcontentloaded")
        await asyncio.sleep(1.0)
        inputs = page.locator("input[type='text']")
        cnt = await inputs.count()
        vals = []
        for i in range(cnt):
            v = (await inputs.nth(i).input_value()).strip()
            if v:
                vals.append(v)
        if len(vals) >= 2:
            first_name, last_name = vals[0], vals[1]
            update_account(account["email"], first_name=first_name, last_name=last_name)
            return first_name, last_name
        if len(vals) == 1 and " " in vals[0]:
            p = vals[0].split(None, 1)
            first_name, last_name = p[0], p[1]
            update_account(account["email"], first_name=first_name, last_name=last_name)
            return first_name, last_name
    except Exception:
        pass

    if not first_name or not last_name:
        inf_f, inf_l = infer_name_from_email(account["email"])
        first_name = first_name or inf_f
        last_name = last_name or inf_l
        update_account(account["email"], first_name=first_name, last_name=last_name)
    return first_name, last_name


async def _interactive_google_login_pw(context, page, account, challenge_callback=None, timeout=180):
    await page.goto("https://myaccount.google.com/?hl=en", wait_until="domcontentloaded")
    await asyncio.sleep(1.2)

    if "myaccount.google.com" in page.url and "signin" not in page.url:
        await _save_context_cookies(context, account)
        await _extract_profile_names_pw(page, account)
        update_account(account["email"], logged_in=True, last_status="جلسة نشطة ✅")
        return True, "Logged in"

    if await _is_insecure_browser_block_pw(page):
        update_account(account["email"], logged_in=False, last_status="محظور من المتصفح")
        return False, "Browser sign-in blocked by Google"

    await page.goto("https://accounts.google.com/signin/v2/identifier?hl=en", wait_until="domcontentloaded")
    try:
        email_input = page.locator("#identifierId, input[type='email']").first
        await email_input.wait_for(state="visible", timeout=15000)
        await email_input.fill(account["email"])
        await email_input.press("Enter")
        await asyncio.sleep(1.2)
        if await email_input.is_visible():
            next_btn = page.locator("#identifierNext, button:has-text('Next'), button:has-text('التالي')").first
            if await next_btn.count() > 0:
                await next_btn.click()
                await asyncio.sleep(1.2)
    except Exception as e:
        return False, f"Failed to enter email: {_format_exc(e)}"

    if await _is_insecure_browser_block_pw(page):
        update_account(account["email"], logged_in=False, last_status="محظور من المتصفح")
        return False, "Browser sign-in blocked by Google"

    if await _has_real_captcha_pw(page) and challenge_callback:
        shot = await _capture_pw_screenshot(page, account["email"])
        code = await challenge_callback(
            account["email"],
            "captcha",
            shot,
            "أدخل رمز التحقق الظاهر في الصورة أو أرسل /cancel:",
        )
        if not code or str(code).strip().lower() == "/cancel":
            return False, "Cancelled by user"
        try:
            cap_box = page.locator("input[type='text']").first
            await cap_box.fill(str(code).strip())
            await cap_box.press("Enter")
            await asyncio.sleep(1.5)
        except Exception:
            pass

    try:
        pwd_input = page.locator("input[name='Passwd'], input[type='password']").first
        await pwd_input.wait_for(state="visible", timeout=12000)
        pwd_to_use = (account.get("password") or "").strip()
        if not pwd_to_use and challenge_callback:
            shot = await _capture_pw_screenshot(page, account["email"])
            reply = await challenge_callback(
                account["email"],
                "password",
                shot,
                "أرسل كلمة مرور الحساب أو /cancel:",
            )
            if reply and str(reply).strip().lower() not in ("/cancel", "done", "ok"):
                pwd_to_use = str(reply).strip()
        if not pwd_to_use:
            return False, "Account password not provided"

        await pwd_input.fill(pwd_to_use)
        await pwd_input.press("Enter")
        await asyncio.sleep(1.0)
        if await pwd_input.is_visible():
            pnext = page.locator("#passwordNext, button:has-text('Next'), button:has-text('التالي')").first
            if await pnext.count() > 0:
                await pnext.click()
                await asyncio.sleep(1.2)
    except Exception:
        pass

    notified_2fa = False
    challenge_task = None
    start = time.time()
    while time.time() - start < timeout:
        if await _is_insecure_browser_block_pw(page):
            if challenge_task and not challenge_task.done():
                challenge_task.cancel()
            update_account(account["email"], logged_in=False, last_status="محظور من المتصفح")
            return False, "Browser sign-in blocked by Google"

        cur = page.url or ""
        if (
            ("myaccount.google.com" in cur or "mail.google.com" in cur or "support.google.com" in cur)
            and "signin" not in cur
            and "challenge" not in cur
        ):
            if challenge_task and not challenge_task.done():
                challenge_task.cancel()
            await asyncio.sleep(0.5)
            await _save_context_cookies(context, account)
            await _extract_profile_names_pw(page, account)
            update_account(account["email"], logged_in=True, last_status="جلسة نشطة ✅")
            return True, "Logged in"

        if "challenge" in cur and not notified_2fa and challenge_callback:
            await asyncio.sleep(0.5)
            notified_2fa = True
            shot = await _capture_pw_screenshot(page, account["email"])
            challenge_task = asyncio.create_task(
                challenge_callback(
                    account["email"],
                    "2fa",
                    shot,
                    "يرجى تأكيد الدخول من هاتفك أو إرسال الرمز هنا:",
                )
            )

        if challenge_task and challenge_task.done():
            try:
                user_reply = challenge_task.result()
            except Exception:
                user_reply = None
            challenge_task = None
            if user_reply and str(user_reply).strip().lower() == "/cancel":
                return False, "2FA cancelled"
            if user_reply and str(user_reply).strip().lower() not in ("done", "ok", ""):
                try:
                    code_input = page.locator("input[type='tel'], input[type='text'], input[type='number']").first
                    await code_input.fill(str(user_reply).strip())
                    await code_input.press("Enter")
                    await asyncio.sleep(1.2)
                except Exception:
                    pass

        await asyncio.sleep(0.6)

    if challenge_task and not challenge_task.done():
        challenge_task.cancel()
    return False, "Verification timeout"


async def analyze_gethelp_step_with_ai(step_summary, available_actions, body_text, first_name="", last_name=""):
    client, model = _get_ai_client_and_model()
    fallback = {
        "action": "submit",
        "target_text": "Submit",
        "input_value": body_text,
    }
    if not client:
        return fallback

    prompt = (
        "You are a deterministic step controller for a Google Play Help support form. "
        "You have NO freedom to invent actions or change the appeal topic. "
        "Allowed actions: 'type_issue', 'select_chip', 'select_area', 'click_next', 'select_email', 'fill_form', 'submit'. "
        "Stick strictly to the Play Points restriction appeal points (traveling abroad, Wi-Fi/location changes causing false-positive suspicious activity flag, legitimate earned points, manual review request). "
        f"Current step context: {step_summary}\n"
        f"Visible elements/options: {available_actions}\n"
        f"First Name: {first_name} | Last Name: {last_name}\n"
        f"Appeal Body: {body_text}\n"
        'Reply ONLY as compact JSON: {"action": "...", "target_text": "...", "input_value": "..."}'
    )
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=180,
        )
        raw = resp.choices[0].message.content.strip().replace("```json", "").replace("```", "").strip()
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return {
                "action": str(parsed.get("action") or fallback["action"]).strip(),
                "target_text": str(parsed.get("target_text") or fallback["target_text"]).strip(),
                "input_value": str(parsed.get("input_value") or "").strip(),
            }
    except Exception as e:
        logging.error(f"AI GetHelp step analyzer error: {_format_exc(e)}")
    return fallback


async def _run_gethelp_form_pw(page, account, body_text, first_name, last_name):
    async def _click_by_texts(texts_lower):
        candidates = page.locator("button, [role='button'], label, span")
        cnt = await candidates.count()
        for i in range(min(cnt, 120)):
            el = candidates.nth(i)
            try:
                if not await el.is_visible():
                    continue
                txt = ((await el.inner_text()) or "").strip().lower()
                if not txt or len(txt) > 80:
                    continue
                if any(k in txt for k in texts_lower):
                    await el.scroll_into_view_if_needed()
                    await el.click(timeout=4000)
                    return True
            except Exception:
                pass
        return False

    await page.goto(f"{GETHELP_URL}?hl=en", wait_until="domcontentloaded")
    await asyncio.sleep(1.5)

    if await _is_insecure_browser_block_pw(page):
        raise RuntimeError("Browser sign-in blocked by Google")

    try:
        issue_inp = page.locator("input[type='text'], input:not([type])").first
        await issue_inp.wait_for(state="visible", timeout=12000)
        await issue_inp.fill("Play point issues")
        await issue_inp.press("Enter")
        await asyncio.sleep(1.0)
        await _click_by_texts(["next step", "الخطوة التالية", "next", "التالي"])
        await asyncio.sleep(1.2)
    except Exception:
        pass

    if not await _click_by_texts(["play points issue", "play points", "نقاط play", "رصيد play"]):
        await _click_by_texts(["other", "غير ذلك"])
    await asyncio.sleep(0.6)

    selects = page.locator("select")
    scnt = await selects.count()
    for i in range(scnt):
        sel = selects.nth(i)
        try:
            if not await sel.is_visible():
                continue
            opts = sel.locator("option")
            ocnt = await opts.count()
            chosen = False
            for j in range(ocnt):
                ot = ((await opts.nth(j).inner_text()) or "").strip().lower()
                if "google play apps" in ot or "play pass" in ot:
                    val = await opts.nth(j).get_attribute("value")
                    if val is not None:
                        await sel.select_option(value=val)
                    else:
                        await sel.select_option(index=j)
                    chosen = True
                    break
            if not chosen and ocnt > 1:
                await sel.select_option(index=1)
        except Exception:
            pass
    await asyncio.sleep(0.6)

    for _ in range(4):
        if await page.locator("textarea:visible").count() > 0:
            break
        if await _click_by_texts(["next step", "الخطوة التالية", "next", "التالي"]):
            await asyncio.sleep(1.1)
        else:
            break

    await _click_by_texts(["email", "البريد الإلكتروني"])
    try:
        await page.locator("textarea:visible, input[type='text']:visible").first.wait_for(state="visible", timeout=10000)
    except Exception:
        await _click_by_texts(["next step", "الخطوة التالية", "next", "التالي"])
        try:
            await page.locator("textarea:visible, input[type='text']:visible").first.wait_for(state="visible", timeout=10000)
        except Exception:
            pass

    email_inputs = page.locator("input[type='email']:visible")
    for i in range(await email_inputs.count()):
        einp = email_inputs.nth(i)
        if not (await einp.input_value()).strip():
            await einp.fill(account["email"])

    text_inputs = page.locator("input[type='text']:visible, input:not([type]):visible")
    tcnt = await text_inputs.count()
    for idx in range(tcnt):
        inp = text_inputs.nth(idx)
        cur_val = (await inp.input_value()).strip()
        aria_lbl = ((await inp.get_attribute("aria-label")) or (await inp.get_attribute("placeholder")) or "").lower()
        parent_txt = ""
        try:
            parent_txt = (await inp.locator("xpath=./ancestor::*[self::div or self::section][1]").inner_text()).lower()
        except Exception:
            pass
        combined_lbl = f"{parent_txt} {aria_lbl}"
        if "transaction id" in combined_lbl or "order id" in combined_lbl or "gpa." in combined_lbl:
            await inp.fill("N/A")
        elif idx == 0 and not cur_val:
            await inp.fill(first_name)
        elif idx == 1 and not cur_val:
            await inp.fill(last_name)
        elif not cur_val:
            await inp.fill("N/A")

    areas = page.locator("textarea:visible")
    if await areas.count() > 0:
        await areas.first.fill(body_text)
        await asyncio.sleep(0.4)

    _ = await analyze_gethelp_step_with_ai(
        step_summary="Final GetHelp Email form ready for submission after filling Transaction ID='N/A' and issue description.",
        available_actions="Submit",
        body_text=body_text,
        first_name=first_name,
        last_name=last_name,
    )

    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await asyncio.sleep(0.4)

    clicked_submit = await _click_by_texts(["submit", "إرسال", "send"])
    if not clicked_submit:
        sub_loc = page.locator(
            "button[type='submit']:visible, input[type='submit']:visible, "
            "[aria-label*='submit' i]:visible, [aria-label*='إرسال' i]:visible, [aria-label*='send' i]:visible"
        )
        if await sub_loc.count() > 0:
            await sub_loc.first.click()
            clicked_submit = True

    if not clicked_submit:
        await _capture_pw_screenshot(page, account["email"])
        raise RuntimeError("Submit button not found on GetHelp form")

    await asyncio.sleep(2.5)
    page_lower = (await page.content()).lower()
    if "we couldn't submit your form yet" in page_lower or "please fix the fields above" in page_lower:
        await _capture_pw_screenshot(page, account["email"])
        raise RuntimeError("Form validation error after clicking Submit")

    confirmation_markers = (
        "thank you",
        "we've received",
        "request received",
        "your request has been submitted",
        "form submitted",
        "تم إرسال",
        "تم استلام",
    )
    if any(m in page_lower for m in confirmation_markers):
        return True, "Submitted via Support Form"

    if "describe your issue" in page_lower:
        await _capture_pw_screenshot(page, account["email"])
        raise RuntimeError("Form did not confirm submission")

    return True, "Submitted via Support Form"


async def login_account_browser(account, challenge_callback=None):
    from playwright.async_api import async_playwright

    account = await ensure_account_session_identity(account)
    async with _BROWSER_LOCK:
        _cleanup_stray_browser_processes()
        async with async_playwright() as pw:
            context = None
            try:
                context, page = await _launch_playwright_context(pw, account)
                ok, msg = await _interactive_google_login_pw(context, page, account, challenge_callback)
                return ok, msg
            except Exception as e:
                err = _format_exc(e)
                logging.error(f"Login error: {err}")
                return False, err
            finally:
                if context:
                    try:
                        await context.close()
                    except Exception:
                        pass


async def send_via_gethelp_form(account, body_text, challenge_callback=None):
    from playwright.async_api import async_playwright

    account = await ensure_account_session_identity(account)
    async with _BROWSER_LOCK:
        _cleanup_stray_browser_processes()
        async with async_playwright() as pw:
            for attempt in range(2):
                context = None
                try:
                    context, page = await _launch_playwright_context(pw, account)
                    ok, msg = await _interactive_google_login_pw(context, page, account, challenge_callback)
                    if not ok:
                        return False, msg

                    first_name, last_name = await _extract_profile_names_pw(page, account)
                    return await _run_gethelp_form_pw(page, account, body_text, first_name, last_name)
                except Exception as e:
                    err = _format_exc(e)
                    if attempt == 0 and _is_browser_crash_error(err):
                        logging.warning(f"Browser crash on GetHelp, retrying once: {err}")
                        continue
                    logging.error(f"GetHelp form error: {err}")
                    return False, err
                finally:
                    if context:
                        try:
                            await context.close()
                        except Exception:
                            pass
    return False, "GetHelp form failed"


async def send_via_gmail_web(account, to_email, subject, body, challenge_callback=None):
    from playwright.async_api import async_playwright

    account = await ensure_account_session_identity(account)
    async with _BROWSER_LOCK:
        _cleanup_stray_browser_processes()
        async with async_playwright() as pw:
            context = None
            try:
                context, page = await _launch_playwright_context(pw, account)
                ok, msg = await _interactive_google_login_pw(context, page, account, challenge_callback)
                if not ok:
                    return False, msg

                await page.goto("https://mail.google.com/mail/u/0/?hl=en", wait_until="domcontentloaded")
                compose = page.locator("div:text-is('Compose')").first
                await compose.wait_for(state="visible", timeout=25000)
                await compose.click()
                await asyncio.sleep(1.0)

                await page.locator("input[aria-label*='To'], input[peoplekit-id]").first.fill(to_email)
                await page.locator("input[name='subjectbox']").first.fill(subject)
                await page.locator("div[aria-label='Message Body']").first.fill(body)
                await page.locator("div:text-is('Send')").first.click()
                await asyncio.sleep(2.0)
                return True, "Sent via Gmail Web"
            except Exception as e:
                err = _format_exc(e)
                logging.error(f"Gmail web error: {err}")
                return False, err
            finally:
                if context:
                    try:
                        await context.close()
                    except Exception:
                        pass


async def dispatch_report(account, subject, body, method="auto", challenge_callback=None):
    settings = load_settings()
    target_email = settings.get("target_email", GOOGLE_PLAY_SUPPORT)
    allow_smtp_fallback = settings.get("smtp_fallback_enabled", True)
    has_smtp = bool((account.get("app_password") or "").strip())

    def _try_smtp_fallback(reason):
        if allow_smtp_fallback and has_smtp:
            s_ok, s_info = send_via_smtp(account, target_email, subject, body)
            if s_ok:
                return True, s_info, "smtp"
            return False, f"{reason} | {s_info}", method
        return False, reason, method

    if method == "smtp":
        ok, info = send_via_smtp(account, target_email, subject, body)
        return ok, info, "smtp"

    if method == "web_form":
        ok, info = await send_via_gethelp_form(account, body, challenge_callback)
        if ok:
            return True, info, "web_form"
        return _try_smtp_fallback(info)

    if method == "gmail_web":
        ok, info = await send_via_gmail_web(account, target_email, subject, body, challenge_callback)
        if ok:
            return True, info, "gmail_web"
        return _try_smtp_fallback(info)

    if account.get("logged_in") or account.get("password") or account.get("cookies"):
        ok, info = await send_via_gethelp_form(account, body, challenge_callback)
        if ok:
            return True, info, "web_form"
        if allow_smtp_fallback and has_smtp:
            s_ok, s_info = send_via_smtp(account, target_email, subject, body)
            if s_ok:
                return True, s_info, "smtp"
        return False, info, "web_form"

    if has_smtp:
        ok, info = send_via_smtp(account, target_email, subject, body)
        return ok, info, "smtp"

    return False, "لا توجد بيانات دخول صالحة للحساب", method
