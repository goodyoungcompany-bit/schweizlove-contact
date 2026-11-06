"""
app.py - Kontaktformular-Dienst fuer schweizlove.ch (Flask auf Railway)
=======================================================================
Nimmt die Nachricht vom Formular entgegen, prueft sie, speichert sie in Supabase und schickt sie per
E-Mail (Resend) an CONTACT_TO. Die Zieladresse steht NUR in der Railway-Variable CONTACT_TO:
nicht im Code, nicht in der Website, nicht in Antworten oder Logs.

Railway-Variablen:
  CONTACT_TO              Zieladresse (Pflicht)
  RESEND_API_KEY          Schluessel von resend.com (Pflicht)
  SUPABASE_URL            wie bei der Wetter-Pipeline
  SUPABASE_KEY            service_role bzw. Secret-Key (Pflicht, sonst blockt die Sicherheitsregel das Speichern)
  SITE_ORIGINS            erlaubte Seiten, Standard: https://schweizlove.ch,https://www.schweizlove.ch
  CONTACT_FROM            optional, Standard: SchweizLove Kontaktformular <onboarding@resend.dev>
  CONTACT_TABLE           optional, Standard: schweizlove_contact
  CONTACT_RETENTION_DAYS  optional, Standard: 180
  CONTACT_RATE_PER_IP     optional, Standard: 5 pro Stunde
  CONTACT_RATE_TOTAL      optional, Standard: 60 pro Stunde
"""

import os
import re
import time
import logging
import threading
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, request, jsonify

logging.basicConfig(format="%(asctime)s  %(levelname)s  %(message)s", level=logging.INFO)
log = logging.getLogger("contact")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024          # Anfragen ueber 20 KB werden abgelehnt

CONTACT_TO = os.environ.get("CONTACT_TO", "").strip()
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
CONTACT_FROM = os.environ.get("CONTACT_FROM", "SchweizLove Kontaktformular <onboarding@resend.dev>").strip()
TABLE = os.environ.get("CONTACT_TABLE", "schweizlove_contact").strip()
RETENTION_DAYS = int(os.environ.get("CONTACT_RETENTION_DAYS", "180"))
RATE_PER_IP = int(os.environ.get("CONTACT_RATE_PER_IP", "5"))
RATE_TOTAL = int(os.environ.get("CONTACT_RATE_TOTAL", "60"))
ORIGINS = {o.strip().rstrip("/") for o in os.environ.get(
    "SITE_ORIGINS", "https://schweizlove.ch,https://www.schweizlove.ch").split(",") if o.strip()}

MIN_ZEIT_MS = 3000                 # schneller als 3 s nach dem Laden = Bot
MAX_ZEIT_MS = 24 * 3600 * 1000     # Seite aelter als 24 h = abgelaufen
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")

_sb = None
_lock = threading.Lock()
_pro_ip = defaultdict(deque)
_gesamt = deque()


def sb():
    global _sb
    if _sb is None:
        from supabase import create_client
        _sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    return _sb


def antwort(ok: bool, fehler: str = "", status: int = 200):
    return jsonify({"ok": ok, "error": fehler}), status


def client_ip() -> str:
    """Rechtester X-Forwarded-For-Eintrag = vom Railway-Proxy gesetzt (ein Client kann nur links anhaengen)."""
    teile = [t.strip() for t in request.headers.get("X-Forwarded-For", "").split(",") if t.strip()]
    return teile[-1] if teile else (request.remote_addr or "?")


def rate_ok(ip: str) -> bool:
    jetzt = time.time()
    grenze = jetzt - 3600
    with _lock:
        d = _pro_ip[ip]
        while d and d[0] < grenze:
            d.popleft()
        while _gesamt and _gesamt[0] < grenze:
            _gesamt.popleft()
        if len(d) >= RATE_PER_IP or len(_gesamt) >= RATE_TOTAL:
            return False
        d.append(jetzt)
        _gesamt.append(jetzt)
        if len(_pro_ip) > 5000:                      # Speicher begrenzen
            for k in [k for k, v in _pro_ip.items() if not v]:
                del _pro_ip[k]
        return True


def zeile(text) -> str:
    """Einzeiliger Text: Zeilenumbrueche/Tabs raus, damit niemand Mail-Kopfzeilen einschleusen kann."""
    return re.sub(r"[\r\n\t]+", " ", str(text or "")).strip()


def maskieren(text: str) -> str:
    """Entfernt die Zieladresse aus Fehlertexten, bevor sie gespeichert oder geloggt werden."""
    text = str(text or "")
    if CONTACT_TO:
        text = re.sub(re.escape(CONTACT_TO), "<CONTACT_TO>", text, flags=re.I)
    if RESEND_API_KEY:
        text = text.replace(RESEND_API_KEY, "<KEY>")
    return text[:500]


def mail_senden(name: str, email: str, nachricht: str, lang: str) -> None:
    if not (CONTACT_TO and RESEND_API_KEY):
        raise RuntimeError("Mailversand nicht konfiguriert (CONTACT_TO oder RESEND_API_KEY fehlt)")
    absender = name or email
    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
        json={
            "from": CONTACT_FROM,
            "to": [CONTACT_TO],
            "reply_to": email,
            "subject": f"Kontaktformular schweizlove.ch: {absender}"[:120],
            "text": f"Neue Nachricht über das Kontaktformular\n\nName: {name or '-'}\nE-Mail: {email}\n"
                    f"Sprache: {lang}\n\n{nachricht}\n",
        },
        timeout=20,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Resend {r.status_code}: {r.text[:300]}")


def alte_loeschen() -> None:
    grenze = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat()
    try:
        sb().table(TABLE).delete().lt("created_at", grenze).execute()
    except Exception as e:
        log.warning(f"Kontakt: Aufraeumen fehlgeschlagen ({type(e).__name__})")


@app.after_request
def cors(resp):
    origin = (request.headers.get("Origin") or "").rstrip("/")
    if origin in ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Max-Age"] = "600"
    resp.headers["Vary"] = "Origin"
    return resp


@app.get("/")
def start():
    return "ok", 200


@app.route("/api/contact", methods=["POST", "OPTIONS"])
def contact():
    origin = (request.headers.get("Origin") or "").rstrip("/")
    if request.method == "OPTIONS":
        return ("", 204) if origin in ORIGINS else ("", 403)
    if origin not in ORIGINS:
        return antwort(False, "herkunft", 403)

    daten = request.get_json(silent=True)
    if not isinstance(daten, dict):
        return antwort(False, "ungueltig", 400)

    # Spam-Falle: gefuelltes Feld "website" -> so tun, als waere alles gut, aber nichts speichern/senden
    if str(daten.get("website") or "").strip():
        return antwort(True)

    try:
        vergangen = float(daten.get("elapsed"))
    except (TypeError, ValueError):
        return antwort(False, "ungueltig", 400)
    if vergangen < MIN_ZEIT_MS:
        return antwort(False, "zu_schnell", 400)
    if vergangen > MAX_ZEIT_MS:
        return antwort(False, "ungueltig", 400)

    name = zeile(daten.get("name"))[:100]
    email = zeile(daten.get("email"))[:200]
    nachricht = str(daten.get("message") or "").replace("\r\n", "\n").strip()
    lang = zeile(daten.get("lang") or "de")[:5]

    if daten.get("consent") is not True:
        return antwort(False, "zustimmung", 400)
    if not EMAIL_RE.match(email) or not (10 <= len(nachricht) <= 4000):
        return antwort(False, "ungueltig", 400)
    if len(re.findall(r"https?://|www\.", nachricht, flags=re.I)) >= 4:
        return antwort(False, "links", 400)

    if not rate_ok(client_ip()):
        return antwort(False, "zu_viele", 429)

    gespeichert_id, mail_fehler = None, None
    try:
        gespeichert_id = sb().table(TABLE).insert(
            {"name": name or None, "email": email, "message": nachricht, "lang": lang}
        ).execute().data[0]["id"]
    except Exception as e:
        log.error(f"Kontakt: Speichern fehlgeschlagen ({maskieren(type(e).__name__ + ': ' + str(e))})")

    try:
        mail_senden(name, email, nachricht, lang)
    except Exception as e:
        mail_fehler = maskieren(e)
        log.error(f"Kontakt: Mailversand fehlgeschlagen ({mail_fehler})")

    if gespeichert_id:
        try:
            sb().table(TABLE).update({"mailed": mail_fehler is None, "mail_error": mail_fehler}
                                     ).eq("id", gespeichert_id).execute()
        except Exception:
            pass
        alte_loeschen()

    if not gespeichert_id and mail_fehler:
        return antwort(False, "server", 500)          # weder gespeichert noch gesendet
    return antwort(True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
