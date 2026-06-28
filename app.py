#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
מנהל הסוכנים - שירות web (Railway / כל אירוח Python).

זרימה: webhook (הודעת וואטסאפ נכנסת מ-PayCall) -> בדיקת הרשאה ->
Claude מפענח לכלי -> ביצוע על סוכן הרווחה (עריכת עובדים/חגים ב-GitHub,
שאילתת עלות) -> תשובה בוואטסאפ + רישום שימוש/עלות (SQLite).

משתני סביבה נדרשים:
  PAYCALL_TOKEN   - טוקן ה-WhatsApp של PayCall/CallIndex
  ANTHROPIC_KEY   - מפתח ה-API של Anthropic (המוח)
  GITHUB_TOKEN    - טוקן GitHub (Contents read/write על המאגר)
  GITHUB_REPO     - owner/repo של סוכן הרווחה (למשל nshemesh5-eng/welfare-reminders)
  ADMIN_PHONE     - מספר האדמין בפורמט בינלאומי, למשל 972505509091
  ANTHROPIC_MODEL - (רשות) ברירת מחדל: claude-haiku-4-5-20251001
  DATA_DIR        - (רשות) תיקיית אחסון ל-SQLite. ברירת מחדל /data, נפילה ל-/tmp
"""
import os
import re
import json
import time
import base64
import sqlite3
import urllib.request
import urllib.parse
from datetime import datetime, timezone

from flask import Flask, request

app = Flask(__name__)

PAYCALL_TOKEN = os.environ.get("PAYCALL_TOKEN", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")
ADMIN_PHONE = os.environ.get("ADMIN_PHONE", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

PRICE_IN_PER_M = 1.0
PRICE_OUT_PER_M = 5.0
USD_TO_ILS = 3.7

# בלם בטיחות: מקסימום הודעות יוצאות בחלון זמן (מונע שליחה חופשית/ספאם)
MAX_SENDS_PER_HOUR = int(os.environ.get("MAX_SENDS_PER_HOUR", "20"))


# ---------- אחסון (SQLite) ----------
def _db_path():
    for d in (os.environ.get("DATA_DIR", "/data"), "/tmp"):
        try:
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, "usage.db")
            open(p, "a").close()
            return p
        except Exception:
            continue
    return "usage.db"


DB_PATH = _db_path()


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS usage(
        agent TEXT, month TEXT, ai_tokens INTEGER DEFAULT 0,
        messages INTEGER DEFAULT 0, cost_ils REAL DEFAULT 0,
        PRIMARY KEY(agent, month))""")
    conn.execute("CREATE TABLE IF NOT EXISTS sends(ts INTEGER)")
    return conn


def within_rate_limit():
    """בודק שלא חרגנו ממכסת השליחות בשעה האחרונה."""
    conn = _db()
    cutoff = int(time.time()) - 3600
    conn.execute("DELETE FROM sends WHERE ts < ?", (cutoff,))
    n = conn.execute("SELECT COUNT(*) FROM sends").fetchone()[0]
    conn.commit()
    conn.close()
    return n < MAX_SENDS_PER_HOUR


def record_send():
    conn = _db()
    conn.execute("INSERT INTO sends(ts) VALUES(?)", (int(time.time()),))
    conn.commit()
    conn.close()


def log_usage(agent, in_tok, out_tok, messages=0):
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    cost = ((in_tok / 1e6) * PRICE_IN_PER_M + (out_tok / 1e6) * PRICE_OUT_PER_M) * USD_TO_ILS
    conn = _db()
    conn.execute("INSERT OR IGNORE INTO usage(agent, month) VALUES(?,?)", (agent, month))
    conn.execute("""UPDATE usage SET ai_tokens=ai_tokens+?, messages=messages+?,
                    cost_ils=cost_ils+? WHERE agent=? AND month=?""",
                 (in_tok + out_tok, messages, round(cost, 4), agent, month))
    conn.commit()
    conn.close()


def get_usage_text(agent="welfare"):
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    conn = _db()
    row = conn.execute("SELECT ai_tokens, messages, cost_ils FROM usage WHERE agent=? AND month=?",
                       (agent, month)).fetchone()
    conn.close()
    tok, msgs, cost = (row or (0, 0, 0))
    return (f"שימוש לסוכן הרווחה ({month}):\nהודעות: {msgs}\n"
            f"טוקני AI: {tok}\nעלות מוערכת: ₪{cost:.1f}")


# ---------- עזרי טלפון ----------
def normalize_phone(p):
    digits = re.sub(r"\D", "", p or "")
    return digits[-9:] if len(digits) >= 9 else digits


def is_authorized(sender):
    return normalize_phone(sender) == normalize_phone(ADMIN_PHONE)


# ---------- GitHub ----------
def _gh_req(method, path, payload=None):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {GITHUB_TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "agent-manager")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def gh_get_file(path):
    info = _gh_req("GET", f"contents/{path}")
    return base64.b64decode(info["content"]).decode("utf-8"), info["sha"]


def gh_put_file(path, new_content, sha, message):
    payload = {"message": message,
               "content": base64.b64encode(new_content.encode("utf-8")).decode(),
               "sha": sha}
    return _gh_req("PUT", f"contents/{path}", payload)


# ---------- פעולות על סוכן הרווחה ----------
def act_list_employees():
    content, _ = gh_get_file("employees.csv")
    out = []
    for r in content.splitlines()[1:]:
        parts = r.split(",")
        if len(parts) >= 2 and r.strip() and "דוגמה" not in r:
            out.append(f"{parts[0]} ({parts[1]})")
    return "רשימת העובדים:\n" + "\n".join(out) if out else "אין עובדים ברשימה."


def act_add_employee(name, birthdate):
    content, sha = gh_get_file("employees.csv")
    lines = content.rstrip("\n").split("\n")
    lines.append(f"{name},{birthdate},,")
    gh_put_file("employees.csv", "\n".join(lines) + "\n", sha, f"Add employee {name}")
    return f"נוסף העובד {name} עם תאריך לידה {birthdate}. ✅"


def act_remove_employee(name):
    content, sha = gh_get_file("employees.csv")
    lines = content.rstrip("\n").split("\n")
    kept = [lines[0]] + [l for l in lines[1:] if not l.startswith(name + ",")]
    if len(kept) == len(lines):
        return f"לא נמצא עובד בשם {name}."
    gh_put_file("employees.csv", "\n".join(kept) + "\n", sha, f"Remove employee {name}")
    return f"הוסר העובד {name}. ✅"


def act_set_holiday(holiday, active):
    content, sha = gh_get_file("holidays_config.csv")
    lines = content.rstrip("\n").split("\n")
    flag = "כן" if active else "לא"
    found = False
    for i in range(1, len(lines)):
        parts = lines[i].split(",")
        if parts and parts[0].strip() == holiday.strip():
            lines[i] = f"{parts[0]},{flag}"
            found = True
    if not found:
        return f"לא נמצא חג בשם {holiday}."
    gh_put_file("holidays_config.csv", "\n".join(lines) + "\n", sha, f"Set holiday {holiday}={flag}")
    return f"החג {holiday} {'הופעל' if active else 'כובה'}. ✅"


# ---------- PayCall שליחה ----------
def send_whatsapp(to, text):
    # בלם בטיחות: לא חורגים ממכסת השליחות בשעה
    if not within_rate_limit():
        print(f"RATE LIMIT reached ({MAX_SENDS_PER_HOUR}/h) - blocked send to {to}")
        return 0
    payload = json.dumps({"method": "sendMessage", "token": PAYCALL_TOKEN,
                          "phone": to, "body": text}).encode("utf-8")
    req = urllib.request.Request("https://wapp.callindex.co.il/", data=payload,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            record_send()
            return r.status
    except Exception as e:
        print("send error:", e)
        return 0


# ---------- Claude (המוח) ----------
TOOLS = [
    {"name": "list_employees", "description": "החזרת רשימת כל העובדים ותאריכי הלידה",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "add_employee", "description": "הוספת עובד חדש",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string"}, "birthdate": {"type": "string", "description": "DD/MM"}},
         "required": ["name", "birthdate"]}},
    {"name": "remove_employee", "description": "הסרת עובד קיים לפי שם",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}},
                      "required": ["name"]}},
    {"name": "set_holiday", "description": "הפעלה/כיבוי של חג",
     "input_schema": {"type": "object", "properties": {
         "holiday": {"type": "string"}, "active": {"type": "boolean"}},
         "required": ["holiday", "active"]}},
    {"name": "get_usage", "description": "כמה הודעות/טוקנים/עלות לסוכן הרווחה החודש",
     "input_schema": {"type": "object", "properties": {}}},
]

SYSTEM = ("אתה מנהל הסוכנים של החברה. המשתמש כותב לך בעברית בוואטסאפ ומבקש לנהל את "
          "סוכן הרווחה (תזכורות ימי הולדת וחגים). בחר בכלי המתאים לבקשה. אם הבקשה לא "
          "ברורה או לא קשורה - בקש הבהרה בקצרה. ענה תמיד בעברית, קצר וברור.")


def anthropic_call(messages, with_tools=True):
    payload = {"model": ANTHROPIC_MODEL, "max_tokens": 1024, "system": SYSTEM, "messages": messages}
    if with_tools:
        payload["tools"] = TOOLS
    req = urllib.request.Request("https://api.anthropic.com/v1/messages",
                                 data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("x-api-key", ANTHROPIC_KEY)
    req.add_header("anthropic-version", "2023-06-01")
    req.add_header("content-type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def execute_tool(name, args):
    if name == "list_employees":
        return act_list_employees()
    if name == "add_employee":
        return act_add_employee(args["name"], args["birthdate"])
    if name == "remove_employee":
        return act_remove_employee(args["name"])
    if name == "set_holiday":
        return act_set_holiday(args["holiday"], bool(args["active"]))
    if name == "get_usage":
        return get_usage_text()
    return "פעולה לא מוכרת."


def handle_message(text):
    messages = [{"role": "user", "content": text}]
    resp = anthropic_call(messages)
    in_tok = resp.get("usage", {}).get("input_tokens", 0)
    out_tok = resp.get("usage", {}).get("output_tokens", 0)

    tool_uses = [b for b in resp.get("content", []) if b.get("type") == "tool_use"]
    if not tool_uses:
        log_usage("manager", in_tok, out_tok)
        texts = [b["text"] for b in resp.get("content", []) if b.get("type") == "text"]
        return " ".join(texts) or "לא הבנתי את הבקשה, אפשר לנסח שוב?"

    tool_results = []
    for tu in tool_uses:
        result = execute_tool(tu["name"], tu.get("input", {}))
        tool_results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": result})

    messages.append({"role": "assistant", "content": resp["content"]})
    messages.append({"role": "user", "content": tool_results})
    final = anthropic_call(messages, with_tools=False)
    in_tok += final.get("usage", {}).get("input_tokens", 0)
    out_tok += final.get("usage", {}).get("output_tokens", 0)
    log_usage("manager", in_tok, out_tok)
    texts = [b["text"] for b in final.get("content", []) if b.get("type") == "text"]
    return " ".join(texts) or "בוצע."


# ---------- קליטת הודעה נכנסת מ-PayCall ----------
def parse_incoming(req):
    # PayCall שולח form-urlencoded
    data = req.form.to_dict() if req.form else {}
    if not data:
        raw = req.get_data(as_text=True) or ""
        ctype = (req.headers.get("Content-Type") or "").lower()
        if "application/json" in ctype:
            try:
                data = json.loads(raw)
            except Exception:
                data = {}
        else:
            data = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
    sender = data.get("author") or data.get("chatId") or data.get("phone") or data.get("from") or ""
    text = data.get("body") or data.get("text") or data.get("message") or ""
    from_me = str(data.get("fromMe", "0")).lower() in ("1", "true")
    return str(sender), str(text).strip(), from_me


@app.route("/", methods=["GET"])
def health():
    return "agent-manager is running", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    sender, text, from_me = parse_incoming(request)
    print(f"incoming from={sender} from_me={from_me} text={text!r}")
    if from_me or not text:
        return "ignored", 200
    if not is_authorized(sender):
        # שתיקה מוחלטת לבלתי-מורשים - לא שולחים שום תגובה (מונע ספאם לאנשי קשר)
        print(f"ignored unauthorized sender: {sender}")
        return "unauthorized", 200
    try:
        reply = handle_message(text)
    except Exception as e:
        print("error:", repr(e))
        reply = "אירעה שגיאה בעיבוד הבקשה. נסה שוב או נסח אחרת."
    send_whatsapp(sender, reply)
    log_usage("manager", 0, 0, messages=1)
    return "ok", 200


# ---------- דיווח שימוש מסוכנים אחרים ----------
@app.route("/report", methods=["POST"])
def report():
    """כל סוכן מדווח שימוש: {key, agent, in_tokens, out_tokens, messages}."""
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    if str(data.get("key", "")) != ADMIN_PHONE:
        return "unauthorized", 403
    log_usage(str(data.get("agent", "unknown")),
              int(data.get("in_tokens", 0) or 0),
              int(data.get("out_tokens", 0) or 0),
              int(data.get("messages", 0) or 0))
    return "ok", 200


# ---------- דשבורד בקרה ----------
@app.route("/dashboard", methods=["GET"])
def dashboard():
    if request.args.get("key", "") != ADMIN_PHONE:
        return "unauthorized - add ?key=YOUR_ADMIN_PHONE", 403
    conn = _db()
    rows = conn.execute("SELECT agent, SUM(ai_tokens), SUM(messages), SUM(cost_ils) "
                        "FROM usage GROUP BY agent ORDER BY SUM(cost_ils) DESC").fetchall()
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    mrows = conn.execute("SELECT agent, ai_tokens, messages, cost_ils FROM usage "
                         "WHERE month=? ORDER BY cost_ils DESC", (month,)).fetchall()
    conn.close()

    total_cost = sum((r[3] or 0) for r in rows)
    total_tok = sum((r[1] or 0) for r in rows)
    total_msg = sum((r[2] or 0) for r in rows)
    top = rows[0][0] if rows else "—"

    def table(data, cols):
        h = "<tr>" + "".join(f"<th>{c}</th>" for c in cols) + "</tr>"
        b = ""
        for r in data:
            mark = " class='top'" if (r[0] == top and len(r) == 4) else ""
            b += f"<tr{mark}><td>{r[0]}</td><td>{int(r[1] or 0):,}</td>" \
                 f"<td>{int(r[2] or 0)}</td><td>₪{(r[3] or 0):.2f}</td></tr>"
        return f"<table>{h}{b}</table>"

    html = f"""<!DOCTYPE html><html lang="he" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>דשבורד הסוכנים</title><style>
body{{font-family:system-ui,Arial;background:#0f1420;color:#e8edf6;margin:0;padding:24px}}
h1{{font-size:22px}} h2{{color:#9aa7bd;font-size:15px;margin-top:28px}}
.kpis{{display:flex;gap:14px;flex-wrap:wrap;margin:16px 0}}
.kpi{{background:#171f2e;border:1px solid #2a3650;border-radius:12px;padding:12px 18px;min-width:130px}}
.kpi .v{{font-size:22px;font-weight:700}} .kpi .l{{font-size:12px;color:#9aa7bd}}
table{{width:100%;border-collapse:collapse;background:#171f2e;border:1px solid #2a3650;border-radius:10px;overflow:hidden}}
th,td{{text-align:right;padding:10px;border-bottom:1px solid #2a3650;font-size:14px}}
th{{color:#9aa7bd}} tr.top td{{background:rgba(251,191,36,.12)}}
.muted{{color:#9aa7bd;font-size:12px;margin-top:18px}}</style></head><body>
<h1>🛰️ דשבורד הסוכנים</h1>
<div class="kpis">
<div class="kpi"><div class="v">{len(rows)}</div><div class="l">סוכנים</div></div>
<div class="kpi"><div class="v">{total_msg}</div><div class="l">הודעות (סה"כ)</div></div>
<div class="kpi"><div class="v">{int(total_tok):,}</div><div class="l">טוקני AI (סה"כ)</div></div>
<div class="kpi"><div class="v">₪{total_cost:.2f}</div><div class="l">עלות כוללת</div></div>
<div class="kpi"><div class="v">🔥 {top}</div><div class="l">הכי בזבזן</div></div>
</div>
<h2>סיכום כולל לפי סוכן</h2>
{table(rows, ["סוכן","טוקני AI","הודעות","עלות"])}
<h2>החודש ({month})</h2>
{table(mrows, ["סוכן","טוקני AI","הודעות","עלות"])}
<p class="muted">עלויות מוערכות לפי מחירון. רענן את הדף לנתונים עדכניים.</p>
</body></html>"""
    return html


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
