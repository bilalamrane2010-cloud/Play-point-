/**
 * Play-point — Gmail relay for Railway
 * ---------------------------------------------------------------------------
 * Railway (and most PaaS hosts) block outbound SMTP on ports 25 / 465 / 587.
 * This tiny Google Apps Script receives an HTTPS POST from the bot and sends
 * the message from YOUR OWN Gmail account, so Google Play sees a real sender.
 *
 * SETUP (5 minutes, one time)
 * 1. Open https://script.google.com  →  New project
 * 2. Delete the sample code and paste this whole file.
 * 3. Change SECRET below to any long random string (keep it private).
 * 4. Deploy → New deployment → type "Web app"
 *      Execute as:        Me
 *      Who has access:    Anyone
 *    → Deploy → Authorize access → allow Gmail sending.
 * 5. Copy the "Web app URL" (ends with /exec).
 * 6. In Railway → your service → Variables, add:
 *      APPS_SCRIPT_URL    = <that URL>
 *      APPS_SCRIPT_SECRET = <the same SECRET you set below>
 *    Then redeploy.
 */

var SECRET = 'CHANGE_ME_TO_A_LONG_RANDOM_STRING';

function doPost(e) {
  try {
    var data = JSON.parse(e.postData.contents);

    if (SECRET !== 'CHANGE_ME_TO_A_LONG_RANDOM_STRING' && data.secret !== SECRET) {
      return _json({ ok: false, error: 'unauthorized' });
    }
    if (!data.to || !data.subject) {
      return _json({ ok: false, error: 'missing to/subject' });
    }

    var options = {
      name: data.from_name || 'Google Play User',
      htmlBody: (data.body || '').replace(/\n/g, '<br>')
    };

    // Send from the script owner's own Gmail address.
    if (data.from_email) {
      options.from = data.from_email;
    }

    MailApp.sendEmail(data.to, data.subject, data.body || '', options);

    return _json({ ok: true, sent: true, remainingQuota: MailApp.getRemainingDailyQuota() });
  } catch (err) {
    return _json({ ok: false, error: String(err) });
  }
}

function doGet() {
  return _json({ ok: true, service: 'play-point-gmail-relay' });
}

function _json(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
