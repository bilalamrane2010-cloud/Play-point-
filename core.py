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


def _format_exc(exc):
    raw = (str(exc) or "").strip()
    if "Stacktrace:" in raw:
        raw = raw.split("Stacktrace:", 1)[0].strip()
    raw = raw.replace("Message:", "", 1).strip() if raw.startswith("Message:") else raw
    if not raw:
        return f"{type(exc).__name__}"
    return f"{type(exc).__name__}: {raw}" if not raw.startswith(type(exc).__name__) else raw


def _is_tab_crash_error(err_or_exc):
    msg = str(err_or_exc).lower()
    return any(
        token in msg
        for token in (
            "tab crashed",
            "target frame detached",
            "disconnected",
            "session deleted because of page crash",
            "chrome not reachable",
            "renderer process crashed",
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
            item = {
                "name": str(c["name"]),
                "value": str(c["value"]),
                "domain": str(c.get("domain") or ".google.com"),
                "path": str(c.get("path") or "/"),
            }
            if c.get("secure") is not None:
                item["secure"] = bool(c["secure"])
            if c.get("httpOnly") is not None:
                item["httpOnly"] = bool(c["httpOnly"])
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


def _resolve_chromedriver_path():
    env_drv = os.getenv("CHROMEDRIVER_PATH", "").strip()
    if env_drv and os.path.exists(env_drv):
        return env_drv
    for name in ("chromedriver",):
        p = shutil.which(name)
        if p and os.path.exists(p):
            return p
    from webdriver_manager.chrome import ChromeDriverManager
    return ChromeDriverManager().install()


def _detect_chrome_major_version(chrome_bin):
    try:
        out = subprocess.check_output([chrome_bin, "--version"], stderr=subprocess.STDOUT, text=True, timeout=5)
        for part in out.strip().split():
            major = part.split(".", 1)[0]
            if major.isdigit():
                return int(major)
    except Exception:
        pass
    return None


def _parse_resolution(res_str):
    try:
        w_str, h_str = str(res_str).lower().split("x", 1)
        w, h = int(w_str.strip()), int(h_str.strip())
        if 300 <= w <= 2560 and 500 <= h <= 4000:
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
            ["pkill", "-9", "-f", "chromium|chrome"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        subprocess.run(
            ["pkill", "-9", "-f", "chromedriver"],
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


def _inject_saved_cookies(driver, account):
    cookies = account.get("cookies") or []
    if not cookies:
        return False
    try:
        driver.get("https://accounts.google.com/robots.txt")
        time.sleep(0.5)
        injected = 0
        for c in cookies:
            cookie_dict = {
                "name": c["name"],
                "value": c["value"],
                "domain": c.get("domain", ".google.com"),
                "path": c.get("path", "/"),
            }
            try:
                driver.add_cookie(cookie_dict)
                injected += 1
            except Exception:
                pass
        return injected > 0
    except Exception:
        return False


def _save_driver_cookies(driver, account):
    try:
        raw_cookies = driver.get_cookies() or []
        google_cookies = [
            {
                "name": c["name"],
                "value": c["value"],
                "domain": c.get("domain", ".google.com"),
                "path": c.get("path", "/"),
                "secure": bool(c.get("secure", True)),
                "httpOnly": bool(c.get("httpOnly", False)),
            }
            for c in raw_cookies
            if "google" in (c.get("domain") or "")
        ]
        if google_cookies:
            update_account(account["email"], cookies=google_cookies)
    except Exception:
        pass


def _build_chrome_driver(account):
    chrome_bin = _resolve_chrome_binary()
    if not chrome_bin:
        raise RuntimeError("Chrome/Chromium not installed in container")

    prof_dir = account.get("profile_dir") or os.path.join(
        PROFILES_DIR, account["email"].replace("@", "_at_").replace(".", "_")
    )
    os.makedirs(prof_dir, exist_ok=True)
    _clean_stale_profile_locks(prof_dir)
    _cleanup_stray_browser_processes()

    width, height = _parse_resolution(account.get("screen_res", "1080x2400"))
    if width > 600:
        width, height = 412, 915

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

    low_mem_args = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-software-rasterizer",
        "--disable-features=VizDisplayCompositor,IsolateOrigins,site-per-process,Translate,BackForwardCache",
        "--disable-background-networking",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        "--disable-breakpad",
        "--disable-extensions",
        "--disable-sync",
        "--disable-default-apps",
        "--disable-component-update",
        "--disable-domain-reliability",
        "--disable-client-side-phishing-detection",
        "--no-first-run",
        "--no-default-browser-check",
        "--renderer-process-limit=1",
        "--disk-cache-size=16777216",
        "--media-cache-size=8388608",
        "--metrics-recording-only",
        "--mute-audio",
        "--js-flags=--max-old-space-size=256",
        f"--window-size={width},{height}",
        f"--lang={locale}",
        f"--user-agent={ua}",
    ]
    if proxy_url:
        low_mem_args.append(f"--proxy-server={proxy_url}")

    major_ver = _detect_chrome_major_version(chrome_bin)
    driver = None

    try:
        import undetected_chromedriver as uc

        uc_opts = uc.ChromeOptions()
        uc_opts.binary_location = chrome_bin
        uc_opts.user_data_dir = prof_dir
        uc_opts.add_argument("--profile-directory=Default")
        for arg in low_mem_args:
            uc_opts.add_argument(arg)

        uc_kwargs = {
            "options": uc_opts,
            "browser_executable_path": chrome_bin,
            "headless": True,
            "use_subprocess": True,
        }
        if major_ver:
            uc_kwargs["version_main"] = major_ver

        driver = uc.Chrome(**uc_kwargs)
    except Exception as uc_err:
        logging.warning(f"undetected_chromedriver fallback: {_format_exc(uc_err)}")
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service

        opts = webdriver.ChromeOptions()
        opts.binary_location = chrome_bin
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        opts.add_argument("--remote-debugging-pipe")
        opts.add_argument("--headless=new")
        opts.add_argument(f"--user-data-dir={prof_dir}")
        opts.add_argument("--profile-directory=Default")
        for arg in low_mem_args:
            opts.add_argument(arg)

        driver_path = _resolve_chromedriver_path()
        driver = webdriver.Chrome(service=Service(driver_path), options=opts)

    try:
        driver.set_page_load_timeout(25)
        driver.set_script_timeout(15)
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": f"""
                    Object.defineProperty(navigator, 'webdriver', {{get: () => undefined}});
                    Object.defineProperty(navigator, 'platform', {{get: () => 'Linux armv8l'}});
                    Object.defineProperty(navigator, 'hardwareConcurrency', {{get: () => 8}});
                    Object.defineProperty(navigator, 'deviceMemory', {{get: () => 8}});
                    window.__DEVICE_IDENTITY__ = {{
                        deviceName: {json.dumps(dev_name)},
                        deviceId: {json.dumps(dev_id)},
                        ipAddress: {json.dumps(ip_addr)}
                    }};
                """
            },
        )
        driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": tz})
        driver.execute_cdp_cmd(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": width,
                "height": height,
                "deviceScaleFactor": 2.625,
                "mobile": True,
            },
        )
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd(
            "Network.setBlockedURLs",
            {
                "urls": [
                    "*.png",
                    "*.jpg",
                    "*.jpeg",
                    "*.gif",
                    "*.webp",
                    "*.mp4",
                    "*.woff",
                    "*.woff2",
                    "*doubleclick.net*",
                    "*google-analytics.com*",
                    "*googletagmanager.com*",
                ]
            },
        )
    except Exception:
        pass

    return driver


def _close_extra_tabs(driver):
    try:
        handles = list(driver.window_handles)
        if len(handles) > 1:
            keep = handles[0]
            for h in handles[1:]:
                try:
                    driver.switch_to.window(h)
                    driver.close()
                except Exception:
                    pass
            driver.switch_to.window(keep)
    except Exception:
        pass


def _capture_screenshot(driver, email):
    _ensure_storage()
    time.sleep(0.4)
    fname = f"{email.replace('@', '_')}_{int(time.time())}.png"
    path = os.path.join(SCREENSHOTS_DIR, fname)
    try:
        driver.save_screenshot(path)
        return path
    except Exception:
        return None


def _is_insecure_browser_block(driver):
    try:
        txt = ((driver.title or "") + " " + (driver.page_source or "")).lower()
        return (
            "couldn't sign you in" in txt
            or "this browser or app may not be secure" in txt
            or "browser or app may not be secure" in txt
            or "try using a different browser" in txt
        )
    except Exception:
        return False


def _has_real_captcha_challenge(driver):
    try:
        from selenium.webdriver.common.by import By

        if _is_insecure_browser_block(driver):
            return False

        pwd_boxes = [
            p for p in driver.find_elements(By.CSS_SELECTOR, "input[type='password'], input[name='Passwd']")
            if p.is_displayed()
        ]
        if pwd_boxes:
            return False

        page_txt = ((driver.title or "") + " " + (driver.page_source or "")).lower()
        if "enter your password" in page_txt or "show password" in page_txt:
            return False

        cur = (driver.current_url or "").lower()
        if "recaptcha" in cur or "captcha" in cur:
            return True

        frames = [
            fr for fr in driver.find_elements(
                By.CSS_SELECTOR,
                "iframe[src*='recaptcha'], iframe[title*='recaptcha' i], iframe[src*='captcha']",
            )
            if fr.is_displayed()
        ]
        if frames:
            return True

        imgs = [
            im for im in driver.find_elements(
                By.CSS_SELECTOR,
                "img[src*='captcha'], img[alt*='captcha' i], img[src*='challenge']",
            )
            if im.is_displayed()
        ]
        if imgs:
            return True

        return False
    except Exception:
        return False


def _click_next_if_present(driver):
    try:
        from selenium.webdriver.common.by import By
        btns = [
            b for b in driver.find_elements(
                By.XPATH,
                "//button[contains(., 'Next') or contains(., 'next') or contains(., 'التالي') or @id='identifierNext' or @id='passwordNext'] | "
                "//div[@role='button'][contains(., 'Next') or contains(., 'التالي')]",
            )
            if b.is_displayed() and b.is_enabled()
        ]
        if btns:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btns[0])
            time.sleep(0.15)
            try:
                btns[0].click()
            except Exception:
                driver.execute_script("arguments[0].click();", btns[0])
            return True
    except Exception:
        pass
    return False


def extract_and_save_profile_names(driver, account):
    first_name = (account.get("first_name") or "").strip()
    last_name = (account.get("last_name") or "").strip()

    if first_name and last_name and first_name != "Play":
        return first_name, last_name

    try:
        driver.get("https://myaccount.google.com/profile/name?hl=en")
        time.sleep(1.2)
        inputs = driver.find_elements("css selector", "input[type='text']")
        vals = [i.get_attribute("value").strip() for i in inputs if i.get_attribute("value")]
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


async def _interactive_google_login(driver, account, challenge_callback=None, timeout=180):
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    try:
        driver.set_page_load_timeout(25)
    except Exception:
        pass

    _inject_saved_cookies(driver, account)

    driver.get("https://myaccount.google.com/?hl=en")
    await asyncio.sleep(1.5)
    _close_extra_tabs(driver)

    if "myaccount.google.com" in driver.current_url and "signin" not in driver.current_url:
        _save_driver_cookies(driver, account)
        extract_and_save_profile_names(driver, account)
        update_account(account["email"], logged_in=True, last_status="جلسة نشطة ✅")
        return True, "Logged in"

    if _is_insecure_browser_block(driver):
        update_account(account["email"], logged_in=False, last_status="محظور من المتصفح")
        return False, "Browser sign-in blocked by Google"

    driver.get("https://accounts.google.com/signin/v2/identifier?hl=en")
    try:
        email_input = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.ID, "identifierId"))
        )
        email_input.clear()
        email_input.send_keys(account["email"])
        email_input.send_keys(Keys.ENTER)
        await asyncio.sleep(1.0)
        if "identifierId" in (driver.page_source or "") and len(driver.find_elements(By.CSS_SELECTOR, "input[type='password'], input[name='Passwd']")) == 0:
            _click_next_if_present(driver)
        await asyncio.sleep(1.5)
    except Exception as e:
        return False, f"Failed to enter email: {_format_exc(e)}"

    if _is_insecure_browser_block(driver):
        update_account(account["email"], logged_in=False, last_status="محظور من المتصفح")
        return False, "Browser sign-in blocked by Google"

    if _has_real_captcha_challenge(driver) and challenge_callback:
        shot = _capture_screenshot(driver, account["email"])
        code = await challenge_callback(
            account["email"],
            "captcha",
            shot,
            "أدخل رمز التحقق الظاهر في الصورة أو أرسل /cancel:",
        )
        if not code or str(code).strip().lower() == "/cancel":
            return False, "Cancelled by user"
        try:
            cap_box = driver.find_element(By.CSS_SELECTOR, "input[type='text']")
            cap_box.send_keys(str(code).strip())
            cap_box.send_keys(Keys.ENTER)
            await asyncio.sleep(1.5)
        except Exception:
            pass

    try:
        pwd_input = WebDriverWait(driver, 12).until(
            lambda d: next(
                (
                    el for el in d.find_elements(By.CSS_SELECTOR, "input[name='Passwd'], input[type='password']")
                    if el.is_displayed() and el.is_enabled()
                ),
                None,
            )
        )
        if pwd_input:
            pwd_to_use = (account.get("password") or "").strip()
            if not pwd_to_use and challenge_callback:
                shot = _capture_screenshot(driver, account["email"])
                reply = await challenge_callback(
                    account["email"],
                    "password",
                    shot,
                    "أرسل كلمة مرور الحساب أو /cancel:",
                )
                if reply and str(reply).strip().lower() != "/cancel" and str(reply).strip().lower() not in ("done", "ok"):
                    pwd_to_use = str(reply).strip()
            if not pwd_to_use:
                return False, "Account password not provided"
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", pwd_input)
            time.sleep(0.15)
            pwd_input.click()
            pwd_input.clear()
            pwd_input.send_keys(pwd_to_use)
            pwd_input.send_keys(Keys.ENTER)
            await asyncio.sleep(0.9)
            if driver.find_elements(By.CSS_SELECTOR, "input[name='Passwd'], input[type='password']"):
                _click_next_if_present(driver)
            await asyncio.sleep(1.5)
    except Exception:
        pass

    notified_2fa = False
    challenge_task = None
    start = time.time()
    while time.time() - start < timeout:
        if _is_insecure_browser_block(driver):
            if challenge_task and not challenge_task.done():
                challenge_task.cancel()
            update_account(account["email"], logged_in=False, last_status="محظور من المتصفح")
            return False, "Browser sign-in blocked by Google"

        cur = driver.current_url or ""
        if (
            ("myaccount.google.com" in cur or "mail.google.com" in cur or "support.google.com" in cur)
            and "signin" not in cur
            and "challenge" not in cur
        ):
            if challenge_task and not challenge_task.done():
                challenge_task.cancel()
            await asyncio.sleep(0.6)
            _save_driver_cookies(driver, account)
            extract_and_save_profile_names(driver, account)
            update_account(account["email"], logged_in=True, last_status="جلسة نشطة ✅")
            return True, "Logged in"

        if "challenge" in cur and not notified_2fa and challenge_callback:
            await asyncio.sleep(0.6)
            notified_2fa = True
            shot = _capture_screenshot(driver, account["email"])
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
                    code_input = driver.find_element(By.CSS_SELECTOR, "input[type='tel'], input[type='text'], input[type='number']")
                    code_input.clear()
                    code_input.send_keys(str(user_reply).strip())
                    code_input.send_keys(Keys.ENTER)
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


async def _run_gethelp_form_once(driver, account, body_text, first_name, last_name):
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import Select, WebDriverWait

    def _safe_click(el):
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", el)
        time.sleep(0.15)
        try:
            el.click()
        except Exception:
            driver.execute_script("arguments[0].click();", el)

    def _safe_type(el, val):
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", el)
        time.sleep(0.15)
        try:
            el.click()
        except Exception:
            driver.execute_script("arguments[0].click();", el)
        try:
            el.clear()
        except Exception:
            pass
        try:
            el.send_keys(val)
        except Exception:
            driver.execute_script(
                "arguments[0].value = arguments[1];"
                "arguments[0].dispatchEvent(new Event('input', {bubbles: true}));"
                "arguments[0].dispatchEvent(new Event('change', {bubbles: true}));",
                el,
                val,
            )

    def _visible_elements(css_sel):
        out = []
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, css_sel):
                try:
                    if el.is_displayed() and el.is_enabled():
                        out.append(el)
                except Exception:
                    pass
        except Exception:
            pass
        return out

    def _wait_any_form_control(timeout=10):
        try:
            return WebDriverWait(driver, timeout).until(
                lambda d: any(
                    el.is_displayed() and el.is_enabled()
                    for el in d.find_elements(By.CSS_SELECTOR, "textarea, input[type='text'], input[type='email'], input:not([type])")
                )
            )
        except Exception:
            return False

    def _click_by_texts(texts_lower):
        for el in driver.find_elements(By.XPATH, "//button | //div[@role='button'] | //span | //label"):
            try:
                if not el.is_displayed():
                    continue
                t = (el.text or "").strip().lower()
                if not t:
                    continue
                if any(k in t for k in texts_lower):
                    _safe_click(el)
                    return True
            except Exception:
                pass
        return False

    def _select_any_dropdown():
        for sel_el in driver.find_elements(By.TAG_NAME, "select"):
            try:
                if not sel_el.is_displayed() or not sel_el.is_enabled():
                    continue
                s = Select(sel_el)
                chosen = False
                for opt in s.options:
                    ot = (opt.text or "").strip().lower()
                    if "google play apps" in ot or "play pass" in ot:
                        s.select_by_visible_text(opt.text)
                        chosen = True
                        break
                if not chosen and len(s.options) > 1:
                    s.select_by_index(1)
            except Exception:
                pass

    driver.get(f"{GETHELP_URL}?hl=en")
    await asyncio.sleep(1.5)
    _close_extra_tabs(driver)

    if _is_insecure_browser_block(driver):
        raise RuntimeError("Browser sign-in blocked by Google")

    try:
        vis_inputs = WebDriverWait(driver, 12).until(
            lambda d: [
                el for el in d.find_elements(By.CSS_SELECTOR, "input[type='text'], input:not([type])")
                if el.is_displayed() and el.is_enabled()
            ]
        )
        if vis_inputs:
            _safe_type(vis_inputs[0], "Play point issues")
            try:
                vis_inputs[0].send_keys(Keys.ENTER)
            except Exception:
                pass
            await asyncio.sleep(1.0)
            _click_by_texts(["next step", "الخطوة التالية", "next", "التالي"])
            await asyncio.sleep(1.2)
    except Exception:
        pass

    if not _click_by_texts(["play points issue", "play points", "نقاط play", "رصيد play"]):
        _click_by_texts(["other", "غير ذلك"])
    await asyncio.sleep(0.6)
    _select_any_dropdown()
    await asyncio.sleep(0.6)

    for _ in range(4):
        if _visible_elements("textarea"):
            break
        if _click_by_texts(["next step", "الخطوة التالية", "next", "التالي"]):
            await asyncio.sleep(1.1)
        else:
            break

    _click_by_texts(["email", "البريد الإلكتروني"])
    _wait_any_form_control(timeout=10)

    if not _visible_elements("textarea") and not _visible_elements("input[type='text']"):
        _click_by_texts(["next step", "الخطوة التالية", "next", "التالي"])
        _wait_any_form_control(timeout=10)

    for e_inp in _visible_elements("input[type='email']"):
        if not (e_inp.get_attribute("value") or "").strip():
            _safe_type(e_inp, account["email"])

    vis_text_inputs = _visible_elements("input[type='text'], input:not([type])")
    for idx, inp in enumerate(vis_text_inputs):
        cur_val = (inp.get_attribute("value") or "").strip()
        parent_txt = ""
        try:
            parent_txt = (inp.find_element(By.XPATH, "./ancestor::*[self::div or self::section][1]").text or "").lower()
        except Exception:
            pass
        aria_lbl = (inp.get_attribute("aria-label") or inp.get_attribute("placeholder") or "").lower()
        combined_lbl = f"{parent_txt} {aria_lbl}"
        if "transaction id" in combined_lbl or "order id" in combined_lbl or "gpa." in combined_lbl:
            _safe_type(inp, "N/A")
        elif idx == 0 and not cur_val:
            _safe_type(inp, first_name)
        elif idx == 1 and not cur_val:
            _safe_type(inp, last_name)
        elif not cur_val:
            _safe_type(inp, "N/A")

    vis_areas = _visible_elements("textarea")
    if vis_areas:
        _safe_type(vis_areas[0], body_text)
        await asyncio.sleep(0.5)

    visible_btns = [
        (el.text or "").strip()
        for el in driver.find_elements(By.XPATH, "//button | //div[@role='button']")
        if el.is_displayed() and (el.text or "").strip()
    ]
    _ = await analyze_gethelp_step_with_ai(
        step_summary="Final GetHelp Email form ready for submission after filling Transaction ID='N/A' and issue description.",
        available_actions=", ".join(visible_btns[:15]),
        body_text=body_text,
        first_name=first_name,
        last_name=last_name,
    )

    try:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        await asyncio.sleep(0.5)
    except Exception:
        pass

    clicked_submit = _click_by_texts(["submit", "إرسال", "send"])
    if not clicked_submit:
        for css_sub in (
            "button[type='submit']",
            "input[type='submit']",
            "[aria-label*='submit' i]",
            "[aria-label*='إرسال' i]",
            "[aria-label*='send' i]",
        ):
            subs = _visible_elements(css_sub)
            if subs:
                _safe_click(subs[0])
                clicked_submit = True
                break
    if not clicked_submit:
        for el in driver.find_elements(By.XPATH, "//button | //div[@role='button'] | //input[@type='button'] | //input[@type='submit']"):
            try:
                if not el.is_displayed():
                    continue
                blob = ((el.text or "") + " " + (el.get_attribute("aria-label") or "")).strip().lower()
                if any(k in blob for k in ("submit", "send", "إرسال")):
                    _safe_click(el)
                    clicked_submit = True
                    break
            except Exception:
                pass

    if not clicked_submit:
        _capture_screenshot(driver, account["email"])
        raise RuntimeError("Submit button not found on GetHelp form")

    try:
        WebDriverWait(driver, 10).until(
            lambda d: any(
                phrase in (d.page_source or "").lower()
                for phrase in (
                    "we couldn't submit your form yet",
                    "please fix the fields above",
                    "thank you",
                    "submitted",
                    "sent",
                    "we've received",
                    "request received",
                )
            )
        )
    except Exception:
        await asyncio.sleep(1.5)

    page_lower = (driver.page_source or "").lower()
    if "we couldn't submit your form yet" in page_lower or "please fix the fields above" in page_lower:
        _capture_screenshot(driver, account["email"])
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
        _capture_screenshot(driver, account["email"])
        raise RuntimeError("Form did not confirm submission")

    return True, "Submitted via Support Form"


async def send_via_gethelp_form(account, body_text, challenge_callback=None):
    account = await ensure_account_session_identity(account)
    driver = None
    try:
        driver = _build_chrome_driver(account)
        ok, msg = await _interactive_google_login(driver, account, challenge_callback)
        if not ok:
            return False, msg

        first_name, last_name = extract_and_save_profile_names(driver, account)

        try:
            return await _run_gethelp_form_once(driver, account, body_text, first_name, last_name)
        except Exception as nav_err:
            if _is_tab_crash_error(nav_err):
                logging.warning(f"Tab crash detected, retrying once: {_format_exc(nav_err)}")
                try:
                    driver.quit()
                except Exception:
                    pass
                driver = _build_chrome_driver(account)
                ok_r, msg_r = await _interactive_google_login(driver, account, challenge_callback)
                if not ok_r:
                    return False, msg_r
                first_name, last_name = extract_and_save_profile_names(driver, account)
                return await _run_gethelp_form_once(driver, account, body_text, first_name, last_name)
            raise
    except Exception as e:
        err = _format_exc(e)
        logging.error(f"GetHelp form error: {err}")
        return False, err
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


async def send_via_gmail_web(account, to_email, subject, body, challenge_callback=None):
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    account = await ensure_account_session_identity(account)
    driver = None
    try:
        driver = _build_chrome_driver(account)
        ok, msg = await _interactive_google_login(driver, account, challenge_callback)
        if not ok:
            return False, msg

        driver.get("https://mail.google.com/mail/u/0/?hl=en")
        _close_extra_tabs(driver)

        compose = WebDriverWait(driver, 25).until(
            EC.element_to_be_clickable((By.XPATH, "//div[text()='Compose']"))
        )
        compose.click()
        await asyncio.sleep(1.0)

        driver.find_element(By.CSS_SELECTOR, "input[aria-label*='To'], input[peoplekit-id]").send_keys(to_email)
        driver.find_element(By.NAME, "subjectbox").send_keys(subject)
        driver.find_element(By.CSS_SELECTOR, "div[aria-label='Message Body']").send_keys(body)
        driver.find_element(By.XPATH, "//div[text()='Send']").click()
        await asyncio.sleep(2.0)
        return True, "Sent via Gmail Web"
    except Exception as e:
        err = _format_exc(e)
        logging.error(f"Gmail web error: {err}")
        return False, err
    finally:
        if driver:
            try:
                driver.quit()
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
