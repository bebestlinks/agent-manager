# PROJECT.md — מערכת הסוכנים (העברת ידע / Handover)

מסמך זה מסכם את כל המערכת כדי שאפשר יהיה להמשיך לעבוד עליה בכל עת,
מכל מחשב, וכדי שכל שיחת Claude/Cowork עתידית תוכל לקרוא אותו ולהבין מיד את ההקשר.

> אם אתה Claude שקורא את זה בשיחה חדשה: זו מערכת חיה ופועלת. קרא את כל המסמך לפני שינויים.

---

## סקירה כללית

שני סוכנים, שניהם בענן ותחת חשבון החברה:

1. **סוכן הרווחה (welfare-reminders)** — שולח תזכורות וואטסאפ אוטומטיות לימי הולדת ולחגים.
2. **מנהל הסוכנים (agent-manager)** — בוט וואטסאפ שמדברים איתו בשפה חופשית; Claude מפענח ומבצע פעולות על סוכן הרווחה.

---

## איפה הכל יושב (חשבונות)

| רכיב | מיקום |
|---|---|
| קוד שני הסוכנים | GitHub, חשבון **bebestlinks** |
| הרצת סוכן הרווחה | GitHub Actions (cron יומי) |
| הרצת מנהל הסוכנים | **Railway** (פרויקט zucchini-curiosity, שירות web) |
| וואטסאפ (שליחה+קבלה) | **PayCall / CallIndex** |
| מוח ה-AI | **Anthropic / Claude Console** |

---

## סוכן הרווחה — welfare-reminders

- מאגר: `github.com/bebestlinks/welfare-reminders`
- קבצים: `send_reminders.py` (לוגיקה), `employees.csv` (עובדים, פורמט DD/MM),
  `holidays_config.csv` (חגים פעילים כן/לא), `.github/workflows/daily.yml` (התזמון).
- רץ כל יום ב-**05:30 UTC = 08:30 שעון קיץ ישראל** (בחורף 07:30 — לשנות cron ל-`30 6 * * *`).
- שולח דרך PayCall: `POST https://wapp.callindex.co.il/` עם `{method:"sendMessage", token, phone, body}`.
- Secret נדרש ב-Actions: `PAYCALL_TOKEN`.
- חגים מחושבים אוטומטית (ספריית pyluach): 14 יום לפני ערב חג (תלושי שי),
  5 ימי עבודה לפני (ארגון), יום עבודה לפני הרמת כוסית (וידוא). ימי עבודה = ראשון–חמישי.
- נמעני התזכורות (קבועים בקוד): מנהל 0505509091, רווחה 0523738214.

## מנהל הסוכנים — agent-manager

- מאגר: `github.com/bebestlinks/agent-manager`
- רץ על Railway (Flask + gunicorn). קובץ ראשי: `app.py`.
- **כתובת webhook (נתונה ל-PayCall):** `https://web-production-6b58c.up.railway.app/webhook`
- זרימה: PayCall שולח הודעה נכנסת (form-urlencoded: author=שולח, body=טקסט, fromMe)
  → בדיקת הרשאה → Claude (tool-use) מפענח → מבצע פעולה (עריכת employees/holidays ב-GitHub
  דרך ה-API) → משיב בוואטסאפ דרך PayCall. שימוש/עלות נרשם ב-SQLite.
- כלים (פקודות) קיימים: list_employees, add_employee, remove_employee, set_holiday, get_usage.
- מודל: `claude-haiku-4-5-20251001` (זול; אפשר לשדרג ל-Sonnet במשתנה ANTHROPIC_MODEL).

### משתני סביבה ב-Railway (השמות; הערכים מוצפנים שם)
`PAYCALL_TOKEN`, `ANTHROPIC_KEY`, `GITHUB_TOKEN` (Contents r/w על welfare-reminders),
`GITHUB_REPO=bebestlinks/welfare-reminders`, `ADMIN_PHONE=972505509091`, `ANTHROPIC_MODEL`.

---

## איך עושים שינויים

- **שינוי תוכן (עובדים/חגים):** פשוט לכתוב למנהל בוואטסאפ. או לערוך את ה-CSV ב-GitHub.
- **שינוי לוגיקה/קוד:** עורכים את הקוד ב-GitHub (welfare: `send_reminders.py`,
  manager: `app.py`). **Railway פורס אוטומטית** כל push למאגר agent-manager.
  סוכן הרווחה רץ ישירות מהמאגר ב-GitHub Actions.
- רק האדמין (ADMIN_PHONE) מורשה לדבר עם המנהל כרגע (שלב 1).

---

## סטטוס ושלבים הבאים

- ✅ שלב 1: סוכן רווחה פעיל + מנהל סוכנים בוואטסאפ פעיל.
- ⬜ שלב 2: הרשאות לעובדים (טבלת מי-ניגש-לאיזה-סוכן לפי טלפון ב-DynamoDB/DB; הרחבת בקרת הגישה ב-app.py).
- ⬜ שלב 3: חיבור סוכנים נוספים + פאנל ניהול עם מעקב עלויות (אב-טיפוס קיים בתיקייה `agent-panel/`).

## דברי ניקוי פתוחים
- להסיר את העובד "לקוח בדיקה" ואת הנמען 0528352004 מסוכן הרווחה.
- לוודא ש-GitHub Actions מופעל במאגר תחת bebestlinks (לפעמים נכבה אחרי העברת בעלות).
- קבצי שורש מיותרים במאגרים (daily.yml, workflow_paycall.yml) — לא מזיקים, אפשר למחוק.
