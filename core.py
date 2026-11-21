import os
import json
import time
import random
import asyncio
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
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
        "device_id": "DEV-S24U-98241",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    },
    {
        "device_name": "Google Pixel 9 Pro XL",
        "device_id": "DEV-P9PXL-55109",
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    },
    {
        "device_name": "OnePlus 13",
        "device_id": "DEV-OP13-73024",
        "user_agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    },
    {
        "device_name": "Xiaomi 14 Ultra",
        "device_id": "DEV-XM14U-61982",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    },
    {
        "device_name": "iPhone 16 Pro Max",
        "device_id": "DEV-IP16PM-84712",
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    },
]


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


def load_accounts():
    _ensure_file(ACCOUNTS_FILE, [])
    with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_accounts(accounts):
    _ensure_storage()
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump(accounts, f, ensure_ascii=False, indent=2)


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
    login_mode="auto",
):
    accounts = load_accounts()
    if any(a["email"].lower() == email.lower() for a in accounts):
        return False
    prof_slug = email.replace("@", "_at_").replace(".", "_")
    accounts.append({
        "email": email.strip(),
        "first_name": first_name.strip(),
        "last_name": last_name.strip(),
        "app_password": app_password.strip(),
        "password": password.strip(),
        "login_mode": login_mode,
        "logged_in": False,
        "profile_dir": os.path.join(PROFILES_DIR, prof_slug),
        "device_name": "",
        "device_id": "",
        "user_agent": "",
        "identity_updated_at": 0,
        "added_at": time.time(),
        "sent": 0,
        "last_status": "New",
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


def load_settings():
    _ensure_file(SETTINGS_FILE, {
        "auto_send_enabled": False,
        "auto_send_interval": 60,
        "send_method": "web_form",
        "min_delay": 25,
        "max_delay": 90,
        "target_email": GOOGLE_PLAY_SUPPORT,
        "base_template": BASE_REPORT_TEMPLATE,
        "rotate_hours": 1,
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
    if not client:
        item = random.choice(REAL_DEVICES_POOL).copy()
        item["device_id"] = f"ID-{random.randint(100000, 999999)}"
        return item
    prompt = (
        "Generate a real-world existing device_name (such as Samsung Galaxy S24 Ultra, Google Pixel 9 Pro, "
        "OnePlus 13, iPhone 16 Pro, Xiaomi 14, Dell XPS 15, MacBook Pro), a fictional unique device_id, "
        "and a realistic Chrome browser user_agent string. Output in English only as valid JSON: "
        '{"device_name": "...", "device_id": "...", "user_agent": "..."}'
    )
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.95,
            max_tokens=220,
        )
        raw = resp.choices[0].message.content.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        data = json.loads(raw)
        if all(k in data and str(data[k]).strip() for k in ("device_name", "device_id", "user_agent")):
            return {
                "device_name": str(data["device_name"]).strip(),
                "device_id": str(data["device_id"]).strip(),
                "user_agent": str(data["user_agent"]).strip(),
            }
    except Exception as e:
        logging.error(f"AI device identity error: {e}")
    item = random.choice(REAL_DEVICES_POOL).copy()
    item["device_id"] = f"ID-{random.randint(100000, 999999)}"
    return item


async def ensure_account_session_identity(account, force_rotate=False):
    if account.get("logged_in"):
        return account

    settings = load_settings()
    rotate_sec = max(1, int(settings.get("rotate_hours", 1))) * 3600
    now = time.time()
    needs_rotation = (
        force_rotate
        or not account.get("user_agent")
        or not account.get("device_id")
        or not account.get("device_name")
        or (now - account.get("identity_updated_at", 0) >= rotate_sec)
    )

    if not needs_rotation:
        return account

    identity = await generate_ai_device_identity()
    updates = {
        "device_name": identity["device_name"],
        "device_id": identity["device_id"],
        "user_agent": identity["user_agent"],
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
        parts = template.split("
", 1)
        subject = parts[0].replace("Subject:", "").strip()
        body = parts[1].strip() if len(parts) > 1 else template
        return {"subject": subject, "body": body}

    prompt = (
        "Rewrite the following Google Play support appeal email so every submission has unique wording, "
        "sentence structure, and natural phrasing while preserving 100% of the core facts: "
        "1) Google Play Points account restriction due to flagged 'suspicious activity' and Play account country/purchase issue while traveling. "
        "2) User is traveling abroad and accessing the account across various international locations and Wi-Fi networks, causing a false-positive security flag. "
        "3) User is the sole authorized owner, all points and transactions were earned legitimately under Google Play Terms of Service, with no fraud. "
        "4) Request for manual review and reinstatement. "
        "Do not invent order IDs or sensitive information. Output in English only.

"
        f"ORIGINAL TEXT:
{template}

"
        "Return strictly in this format:
"
        "SUBJECT: <rewritten subject>
"
        "BODY:
<rewritten body>"
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
                body = "
".join(lines[i + 1:]).strip()
                break
        return {"subject": subject, "body": body}
    except Exception as e:
        logging.error(f"AI report generation failed: {e}")
        parts = template.split("
", 1)
        subject = parts[0].replace("Subject:", "").strip()
        body = parts[1].strip() if len(parts) > 1 else template
        return {"subject": subject, "body": body}


def send_via_smtp(account, to_email, subject, body):
    if not account.get("app_password"):
        return False, "Missing App Password"
    msg = MIMEMultipart()
    sender_name = f"{account.get('first_name', '')} {account.get('last_name', '')}".strip() or "Google Play User"
    msg["From"] = formataddr((sender_name, account["email"]))
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(account["email"], account["app_password"])
            server.send_message(msg)
        return True, "Sent successfully via SMTP"
    except Exception as e:
        logging.error(f"SMTP error: {e}")
        return False, str(e)


def test_smtp(account):
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(account["email"], account["app_password"])
        return True, "SMTP connection OK"
    except Exception as e:
        return False, str(e)


def _build_chrome_driver(account):
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager

    opts = webdriver.ChromeOptions()
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument("--lang=en-US")

    if account.get("user_agent"):
        opts.add_argument(f"--user-agent={account['user_agent']}")

    prof_dir = account.get("profile_dir") or os.path.join(
        PROFILES_DIR, account["email"].replace("@", "_at_").replace(".", "_")
    )
    os.makedirs(prof_dir, exist_ok=True)
    opts.add_argument(f"--user-data-dir={prof_dir}")
    opts.add_argument("--profile-directory=Default")

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=opts,
    )
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {
            "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        },
    )
    return driver


def _capture_screenshot(driver, email):
    _ensure_storage()
    fname = f"{email.replace('@', '_')}_{int(time.time())}.png"
    path = os.path.join(SCREENSHOTS_DIR, fname)
    driver.save_screenshot(path)
    return path


async def _interactive_google_login(driver, account, challenge_callback=None, timeout=180):
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    driver.get("https://myaccount.google.com/?hl=en")
    await asyncio.sleep(4)

    if "myaccount.google.com" in driver.current_url and "signin" not in driver.current_url:
        update_account(account["email"], logged_in=True, last_status="Session active")
        return True, "Logged in via saved session"

    driver.get("https://accounts.google.com/signin/v2/identifier?hl=en")
    try:
        email_input = WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.ID, "identifierId"))
        )
        email_input.clear()
        email_input.send_keys(account["email"])
        email_input.send_keys(Keys.ENTER)
        await asyncio.sleep(3)
    except Exception as e:
        return False, f"Failed to enter email: {e}"

    if "challenge" in driver.current_url or "captcha" in driver.page_source.lower():
        if challenge_callback:
            shot = _capture_screenshot(driver, account["email"])
            code = await challenge_callback(
                account["email"],
                "captcha",
                shot,
                "CAPTCHA verification required. Enter the code shown in the screenshot or /cancel:",
            )
            if not code or code.lower() == "/cancel":
                return False, "Cancelled by user"
            try:
                cap_box = driver.find_element(By.CSS_SELECTOR, "input[type='text']")
                cap_box.send_keys(code)
                cap_box.send_keys(Keys.ENTER)
                await asyncio.sleep(3)
            except Exception:
                pass

    try:
        pwd_input = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.NAME, "Passwd"))
        )
        if not account.get("password"):
            return False, "Account password not set"
        pwd_input.clear()
        pwd_input.send_keys(account["password"])
        pwd_input.send_keys(Keys.ENTER)
        await asyncio.sleep(4)
    except Exception:
        pass

    notified_2fa = False
    start = time.time()
    while time.time() - start < timeout:
        cur = driver.current_url
        if "myaccount.google.com" in cur or "mail.google.com" in cur or "support.google.com" in cur:
            if "signin" not in cur and "challenge" not in cur:
                update_account(account["email"], logged_in=True, last_status="Logged in and session saved")
                return True, "Logged in successfully"

        if "challenge" in cur and not notified_2fa and challenge_callback:
            notified_2fa = True
            shot = _capture_screenshot(driver, account["email"])
            user_reply = await challenge_callback(
                account["email"],
                "2fa",
                shot,
                "2FA verification required: approve on your device or send the verification code (or /cancel):",
            )
            if user_reply and user_reply.lower() == "/cancel":
                return False, "2FA cancelled"
            if user_reply and user_reply.lower() not in ("done", "ok"):
                try:
                    code_input = driver.find_element(By.CSS_SELECTOR, "input[type='tel'], input[type='text']")
                    code_input.clear()
                    code_input.send_keys(user_reply.strip())
                    code_input.send_keys(Keys.ENTER)
                except Exception:
                    pass

        await asyncio.sleep(3)

    return False, "2FA or CAPTCHA timeout"


def _extract_profile_names_if_missing(driver, account):
    if account.get("first_name") and account.get("last_name"):
        return account["first_name"], account["last_name"]
    try:
        driver.get("https://myaccount.google.com/profile/name?hl=en")
        time.sleep(3)
        inputs = driver.find_elements("css selector", "input[type='text']")
        vals = [i.get_attribute("value").strip() for i in inputs if i.get_attribute("value")]
        if len(vals) >= 2:
            update_account(account["email"], first_name=vals[0], last_name=vals[1])
            return vals[0], vals[1]
    except Exception:
        pass
    local_part = account["email"].split("@")[0]
    return account.get("first_name") or local_part, account.get("last_name") or "User"


async def send_via_gethelp_form(account, body_text, challenge_callback=None):
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    account = await ensure_account_session_identity(account)
    driver = None
    try:
        driver = _build_chrome_driver(account)
        ok, msg = await _interactive_google_login(driver, account, challenge_callback)
        if not ok:
            return False, msg

        first_name, last_name = _extract_profile_names_if_missing(driver, account)

        driver.get(f"{GETHELP_URL}?hl=en")
        await asyncio.sleep(4)

        try:
            step1_input = WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='text'], textarea"))
            )
            step1_input.clear()
            step1_input.send_keys("Play point issues")
            await asyncio.sleep(1)
            next_btn = driver.find_element(By.XPATH, "//button[contains(., 'Next')]")
            next_btn.click()
            await asyncio.sleep(3)
        except Exception:
            pass

        try:
            chip = WebDriverWait(driver, 12).until(
                EC.element_to_be_clickable((By.XPATH, "//*[contains(text(), 'Play Points issue')]"))
            )
            chip.click()
            await asyncio.sleep(2)
        except Exception:
            pass

        for _ in range(3):
            try:
                ns = driver.find_element(By.XPATH, "//button[contains(., 'Next step')]")
                ns.click()
                await asyncio.sleep(2)
            except Exception:
                break

        try:
            email_opt = WebDriverWait(driver, 12).until(
                EC.element_to_be_clickable((By.XPATH, "//*[contains(text(), 'Email')]"))
            )
            email_opt.click()
            await asyncio.sleep(3)
        except Exception:
            pass

        text_inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='text']")
        if len(text_inputs) >= 3:
            text_inputs[0].clear()
            text_inputs[0].send_keys(first_name)
            text_inputs[1].clear()
            text_inputs[1].send_keys(last_name)
            text_inputs[2].clear()
            text_inputs[2].send_keys("N/A")

        issue_area = WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "textarea"))
        )
        issue_area.clear()
        issue_area.send_keys(body_text)
        await asyncio.sleep(1)

        submit_btn = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.XPATH, "//button[contains(., 'Submit')]"))
        )
        submit_btn.click()
        await asyncio.sleep(4)
        return True, "Submitted via Google Play GetHelp"
    except Exception as e:
        logging.error(f"GetHelp form error: {e}")
        return False, f"GetHelp form error: {e}"
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


async def send_via_gmail_web(account, to_email, subject, body, challenge_callback=None):
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    account = await ensure_account_session_identity(account)
    driver = None
    try:
        driver = _build_chrome_driver(account)
        ok, msg = await _interactive_google_login(driver, account, challenge_callback)
        if not ok:
            return False, msg

        driver.get("https://mail.google.com/mail/u/0/?hl=en")
        await asyncio.sleep(5)

        compose = WebDriverWait(driver, 25).until(
            EC.element_to_be_clickable((By.XPATH, "//div[text()='Compose']"))
        )
        compose.click()
        await asyncio.sleep(2)

        driver.find_element(By.CSS_SELECTOR, "input[aria-label*='To'], input[peoplekit-id]").send_keys(to_email)
        driver.find_element(By.NAME, "subjectbox").send_keys(subject)
        driver.find_element(By.CSS_SELECTOR, "div[aria-label='Message Body']").send_keys(body)
        driver.find_element(By.XPATH, "//div[text()='Send']").click()
        await asyncio.sleep(4)
        return True, "Sent via Gmail Web"
    except Exception as e:
        logging.error(f"Gmail web error: {e}")
        return False, str(e)
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


async def dispatch_report(account, subject, body, method="auto", challenge_callback=None):
    settings = load_settings()
    target_email = settings.get("target_email", GOOGLE_PLAY_SUPPORT)

    if method == "web_form":
        return await send_via_gethelp_form(account, body, challenge_callback)
    if method == "smtp":
        return send_via_smtp(account, target_email, subject, body)
    if method == "gmail_web":
        return await send_via_gmail_web(account, target_email, subject, body, challenge_callback)

    if account.get("logged_in") or account.get("password"):
        return await send_via_gethelp_form(account, body, challenge_callback)
    if account.get("app_password"):
        return send_via_smtp(account, target_email, subject, body)
    return False, "No valid credentials for account"
