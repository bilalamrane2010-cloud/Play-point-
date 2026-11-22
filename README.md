# Play-point Bot

بوت تيليجرام لإدارة حسابات Google Play وإرسال طلبات الاستئناف، مع فاصل إجباري **6 ساعات** بين كل إرسال والذي يليه عبر كل الحسابات.

## الملفات

| الملف | الوظيفة |
|---|---|
| `main.py` | نقطة التشغيل: أوامر تيليجرام، لوحة التحكم، الجدولة |
| `core.py` | المحرك: الحسابات، التخزين، SMTP، Chromium، توليد التقارير |
| `requirements.txt` | المكتبات المطلوبة |
| `Dockerfile` | بناء صورة التشغيل (Chromium + الخطوط) |
| `.dockerignore` | يمنع نسخ بيانات الحسابات داخل الصورة |
| `.gitignore` | يستثني ملفات Python المؤقتة |

## التشغيل محلياً

```bash
pip install -r requirements.txt
cp .env.example .env      # ثم عدّل القيم داخله
export $(grep -v '^#' .env | xargs)
python main.py
```

## التشغيل عبر Docker

```bash
docker build -t play-point .
docker run -d --env-file .env -v $(pwd)/bot_data:/app/bot_data play-point
```

> ✅ **المشروع يعمل فوراً بدون أي إعداد.** التوكن ومفتاح الذكاء الاصطناعي
> وقائمة المستخدمين المسموح لهم كلها مضمّنة داخل الملفات.
> متغيرات البيئة اختيارية فقط إذا أردت تغيير قيمة.

## متغيرات البيئة (كلها اختيارية — القيم الافتراضية مضمّنة)

| المتغير | الافتراضي | الوصف |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | مضمّن في `main.py` | توكن البوت — يعمل بدون أي إعداد |
| `OPENAI_API_KEY` | مضمّن في `core.py` | مفتاح Groq — يعمل بدون أي إعداد |
| `OPENAI_MODEL` | `openai/gpt-oss-120b` | اسم الموديل |
| `ALLOWED_USER_IDS` | فارغ (الجميع) | آيديهات تيليجرام المسموح لها |
| `BOT_DATA_DIR` | `bot_data` | مجلد تخزين البيانات |
| `EMAIL_TRANSPORT` | `auto` | `auto` / `smtp` / `brevo` / `resend` / `apps_script` |
| `APPS_SCRIPT_URL` | فارغ | رابط Web App لإرسال البريد عبر HTTPS |
| `APPS_SCRIPT_SECRET` | فارغ | نفس `SECRET` داخل ملف Apps Script |
| `BREVO_API_KEY` | فارغ | بديل HTTPS للإرسال |
| `RESEND_API_KEY` | فارغ | بديل HTTPS للإرسال |

## النشر على Railway

1. ارفع الملفات إلى المستودع.
2. أضف **Volume** واربطه على المسار `/app/bot_data` (ضروري، وإلا تُفقد الحسابات مع كل نشر).
3. أضف متغيرات البيئة إن رغبت بتجاوز القيم الافتراضية.
4. Redeploy.

> البوت يعمل بـ long polling ولا يحتاج منفذاً — تجاهل تحذير "no port detected".

## ⚠️ مهم: Railway يحجب منافذ SMTP

منصة Railway تحجب الاتصال الخارجي على المنافذ **25 / 465 / 587**، لذلك أي إرسال
بريد مباشر عبر SMTP يفشل برسالة:

```
OSError: [Errno 101] Network is unreachable
```

الحل: الإرسال عبر **HTTPS** باستخدام Google Apps Script (يرسل من نفس حساب Gmail
الخاص بك، مجاناً، وبدون دومين). البوت يدعم الآن أربع قنوات ويختار الأفضل تلقائياً:

| القناة | المتغير | ملاحظات |
|---|---|---|
| Google Apps Script | `APPS_SCRIPT_URL` + `APPS_SCRIPT_SECRET` | **الأفضل** — يرسل من جيميلك مباشرة |
| Brevo | `BREVO_API_KEY` | 300 رسالة/يوم مجاناً، يتطلب توثيق المرسل |
| Resend | `RESEND_API_KEY` | يتطلب دومين موثّق |
| SMTP مباشر | — | يعمل محلياً وعلى VPS فقط |

### إعداد Google Apps Script (5 دقائق، مرة واحدة)

1. افتح <https://script.google.com> ← **New project**.
2. امسح الكود الموجود والصق محتوى ملف `google_apps_script.gs`.
3. غيّر `SECRET` إلى نص عشوائي طويل.
4. **Deploy** ← **New deployment** ← النوع **Web app**:
   - Execute as: **Me**
   - Who has access: **Anyone**
5. اضغط **Deploy** ثم **Authorize access** ووافق على إرسال البريد.
6. انسخ رابط **Web app URL** (ينتهي بـ `/exec`).
7. في Railway ← Variables أضف:
   ```
   APPS_SCRIPT_URL    = https://script.google.com/macros/s/.../exec
   APPS_SCRIPT_SECRET = نفس النص العشوائي
   ```
8. Redeploy.

بعد ذلك سيعمل الإرسال حتى مع حجب SMTP، وستظهر في لوحة التحكم:
`• قناة البريد: SMTP ثم HTTPS تلقائياً ✅`

### App Password (16 حرفاً) — ليس كلمة مرور الحساب

Gmail لا يقبل كلمة مرور الحساب العادية عبر SMTP، ويجيب بـ:

```
535 5.7.8 Username and Password not accepted
```

يجب إنشاء **App Password**: <https://myaccount.google.com/apppasswords>
(يتطلب تفعيل التحقق بخطوتين). الناتج 16 حرفاً مثل `abcd efgh ijkl mnop` —
الصقها كما هي، والبوت يحذف المسافات تلقائياً.

## ملاحظة أمنية

التوكنات مضبوطة داخل الكود بناءً على طلبك. إذا كان المستودع **عاماً** فسيتمكن أي شخص من رؤيتها،
وقد يقوم GitHub تلقائياً بإبطال توكن تيليجرام عند اكتشافه في مستودع عام.
**اجعل المستودع خاصاً (Private)** أو استخدم متغيرات البيئة بدلاً من القيم المضمّنة.
