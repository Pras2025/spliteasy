import os, io, datetime, sqlite3
from collections import defaultdict

from flask import (Flask, render_template, request, redirect,
                   url_for, session, send_file, send_from_directory, flash)
from flask_login import (LoginManager, UserMixin,
                         login_user, logout_user, login_required, current_user)
from werkzeug.middleware.proxy_fix import ProxyFix

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                 Table, TableStyle, HRFlowable)
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_RIGHT, TA_CENTER

import requests as http_requests  # plain requests lib — no flask-dance

# ── App ───────────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "spliteasy-dev-secret-change-in-prod")

# Session cookie must work over HTTPS on Render
app.config.update(
    SESSION_COOKIE_SECURE   = os.environ.get("RENDER", False),
    SESSION_COOKIE_HTTPONLY = True,
    SESSION_COOKIE_SAMESITE = "Lax",
    SESSION_COOKIE_NAME     = "spliteasy_session",
)

# Trust Render's HTTPS proxy
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, "expense.db")

GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_AUTH_URL      = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL     = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL  = "https://www.googleapis.com/oauth2/v2/userinfo"
GOOGLE_SCOPES        = "openid email profile"

# ── Flask-Login ───────────────────────────────────────────────────────────────
login_manager = LoginManager(app)
login_manager.login_view = "login_page"

class User(UserMixin):
    def __init__(self, id, name, email, avatar):
        self.id     = str(id)
        self.name   = name
        self.email  = email
        self.avatar = avatar

@login_manager.user_loader
def load_user(user_id):
    conn = get_connection(); c = conn.cursor()
    c.execute("SELECT * FROM users WHERE id=?", (user_id,))
    row = c.fetchone(); conn.close()
    if row:
        return User(row["id"], row["name"], row["email"], row["avatar"] or "")
    return None

# ── DB ────────────────────────────────────────────────────────────────────────
def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_connection(); c = conn.cursor()

    # Create users table
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        google_id TEXT UNIQUE NOT NULL,
        name      TEXT NOT NULL,
        email     TEXT NOT NULL,
        avatar    TEXT
    )""")

    # Create events table
    c.execute("""CREATE TABLE IF NOT EXISTS events (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id      INTEGER,
        name         TEXT NOT NULL,
        participants TEXT NOT NULL
    )""")

    # Create expenses table
    c.execute("""CREATE TABLE IF NOT EXISTS expenses (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id     INTEGER NOT NULL,
        name         TEXT NOT NULL,
        amount       REAL NOT NULL,
        payer        TEXT NOT NULL,
        participants TEXT NOT NULL
    )""")

    # ── Migration: add user_id column if it does not exist yet ──
    cols = [row[1] for row in c.execute("PRAGMA table_info(events)").fetchall()]
    if "user_id" not in cols:
        c.execute("ALTER TABLE events ADD COLUMN user_id INTEGER")
        app.logger.info("Migration: added user_id column to events")

    conn.commit(); conn.close()

def get_or_create_user(google_id, name, email, avatar):
    conn = get_connection(); c = conn.cursor()
    c.execute("SELECT * FROM users WHERE google_id=?", (google_id,))
    row = c.fetchone()
    if row:
        c.execute("UPDATE users SET name=?,email=?,avatar=? WHERE google_id=?",
                  (name, email, avatar, google_id))
        uid = row["id"]
    else:
        c.execute("INSERT INTO users (google_id,name,email,avatar) VALUES (?,?,?,?)",
                  (google_id, name, email, avatar))
        uid = c.lastrowid
    conn.commit(); conn.close()
    return uid

# ── PWA ───────────────────────────────────────────────────────────────────────
@app.route("/sw.js")
def service_worker():
    return send_from_directory(os.path.join(app.root_path, "static"),
                               "sw.js", mimetype="application/javascript")

@app.route("/manifest.json")
def manifest():
    return send_from_directory(os.path.join(app.root_path, "static"),
                               "manifest.json", mimetype="application/manifest+json")

@app.route("/offline")
def offline():
    return render_template("offline.html")

# ── Google OAuth (manual, no flask-dance) ────────────────────────────────────
def get_redirect_uri():
    """Build the exact redirect URI — always HTTPS on Render."""
    if request.headers.get("X-Forwarded-Proto") == "https" or \
       "onrender.com" in request.host:
        return f"https://{request.host}/google/callback"
    return url_for("google_callback", _external=True)

@app.route("/login")
def login_page():
    if current_user.is_authenticated:
        return redirect(url_for("home"))
    return render_template("login.html")

@app.route("/google/login")
def google_login():
    if not GOOGLE_CLIENT_ID:
        flash("Google sign-in is not configured yet.", "error")
        return redirect(url_for("login_page"))
    redirect_uri = get_redirect_uri()
    import urllib.parse, secrets
    state = secrets.token_urlsafe(16)
    session["oauth_state"] = state
    params = urllib.parse.urlencode({
        "client_id":     GOOGLE_CLIENT_ID,
        "redirect_uri":  redirect_uri,
        "response_type": "code",
        "scope":         GOOGLE_SCOPES,
        "state":         state,
        "access_type":   "offline",
        "prompt":        "select_account",
    })
    return redirect(f"{GOOGLE_AUTH_URL}?{params}")

@app.route("/google/callback")
def google_callback():
    # Check for error from Google
    error = request.args.get("error")
    if error:
        flash(f"Google sign-in was cancelled: {error}", "error")
        return redirect(url_for("login_page"))

    # Validate state (skip strict check if session was lost across request)
    received_state = request.args.get("state", "")
    expected_state = session.pop("oauth_state", None)
    if expected_state and received_state != expected_state:
        app.logger.warning(f"State mismatch: expected={expected_state} got={received_state}")
        flash("Session mismatch. Please try signing in again.", "error")
        return redirect(url_for("login_page"))

    code = request.args.get("code")
    if not code:
        flash("No authorisation code received.", "error")
        return redirect(url_for("login_page"))

    redirect_uri = get_redirect_uri()

    # Exchange code for token
    try:
        token_resp = http_requests.post(GOOGLE_TOKEN_URL, data={
            "code":          code,
            "client_id":     GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri":  redirect_uri,
            "grant_type":    "authorization_code",
        }, timeout=10)
        token_resp.raise_for_status()
        token_data = token_resp.json()
    except Exception as e:
        app.logger.error(f"Token exchange failed: {e}")
        flash("Sign-in failed during token exchange. Please try again.", "error")
        return redirect(url_for("login_page"))

    access_token = token_data.get("access_token")
    if not access_token:
        flash("No access token received from Google.", "error")
        return redirect(url_for("login_page"))

    # Fetch user profile
    try:
        info_resp = http_requests.get(GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
        info_resp.raise_for_status()
        info = info_resp.json()
    except Exception as e:
        app.logger.error(f"Userinfo fetch failed: {e}")
        flash("Could not fetch your Google profile. Please try again.", "error")
        return redirect(url_for("login_page"))

    google_id = info.get("id")
    name      = info.get("name", "User")
    email     = info.get("email", "")
    avatar    = info.get("picture", "")

    if not google_id:
        flash("Google did not return a valid user ID.", "error")
        return redirect(url_for("login_page"))

    uid  = get_or_create_user(google_id, name, email, avatar)
    user = User(uid, name, email, avatar)
    login_user(user, remember=True)
    return redirect(url_for("home"))

@app.route("/logout")
@login_required
def logout():
    logout_user()
    session.clear()
    return redirect(url_for("login_page"))

# ── Home ──────────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET", "POST"])
@login_required
def home():
    conn = get_connection(); c = conn.cursor()
    if request.method == "POST":
        c.execute("INSERT INTO events (user_id,name,participants) VALUES (?,?,?)",
                  (current_user.id,
                   request.form["event_name"].strip(),
                   request.form["participants"].strip()))
        conn.commit()
    c.execute("SELECT id,name FROM events WHERE user_id=? OR user_id IS NULL ORDER BY id DESC",
              (current_user.id,))
    events = c.fetchall(); conn.close()
    return render_template("index.html", events=events)

# ── Open Event ────────────────────────────────────────────────────────────────
@app.route("/event/<int:event_id>")
@login_required
def open_event(event_id):
    conn = get_connection(); c = conn.cursor()
    c.execute("SELECT * FROM events WHERE id=? AND user_id=?",
              (event_id, current_user.id))
    event = c.fetchone(); conn.close()
    if not event:
        return redirect(url_for("home"))
    session["event_id"]    = event["id"]
    session["event_name"]  = event["name"]
    session["participants"] = [p.strip() for p in event["participants"].split(",") if p.strip()]
    return redirect(url_for("dashboard"))

# ── Dashboard ─────────────────────────────────────────────────────────────────
@app.route("/dashboard")
@login_required
def dashboard():
    if "event_id" not in session: return redirect(url_for("home"))
    conn = get_connection(); c = conn.cursor()
    c.execute("SELECT amount FROM expenses WHERE event_id=?", (session["event_id"],))
    rows = c.fetchall(); conn.close()
    return render_template("dashboard.html",
        event_name=session["event_name"],
        total_amount=round(sum(r["amount"] for r in rows), 2),
        participant_count=len(session["participants"]),
        expense_count=len(rows))

# ── Add Expense ───────────────────────────────────────────────────────────────
@app.route("/add_expense", methods=["GET", "POST"])
@login_required
def add_expense():
    if "event_id" not in session: return redirect(url_for("home"))
    if request.method == "POST":
        conn = get_connection(); c = conn.cursor()
        c.execute("""INSERT INTO expenses
                     (event_id,name,amount,payer,participants)
                     VALUES (?,?,?,?,?)""",
                  (session["event_id"],
                   request.form["expense_name"],
                   float(request.form["amount"]),
                   request.form["payer"],
                   ",".join(request.form.getlist("participants"))))
        conn.commit(); conn.close()
        return redirect(url_for("show_expenses"))
    return render_template("add_expense.html",
        event_name=session["event_name"],
        participants=session["participants"])

# ── Show Expenses ─────────────────────────────────────────────────────────────
@app.route("/expenses")
@login_required
def show_expenses():
    if "event_id" not in session: return redirect(url_for("home"))
    conn = get_connection(); c = conn.cursor()
    c.execute("SELECT * FROM expenses WHERE event_id=? ORDER BY id DESC",
              (session["event_id"],))
    rows = c.fetchall(); conn.close()
    expenses = [{"id":r["id"],"name":r["name"],"amount":r["amount"],
                 "payer":r["payer"],"participants":r["participants"].split(",")}
                for r in rows]
    return render_template("expenses.html",
        event_name=session["event_name"], expenses=expenses)

# ── Delete Expense ────────────────────────────────────────────────────────────
@app.route("/delete/<int:expense_id>")
@login_required
def delete_expense(expense_id):
    if "event_id" not in session: return redirect(url_for("home"))
    conn = get_connection(); c = conn.cursor()
    c.execute("DELETE FROM expenses WHERE id=? AND event_id=?",
              (expense_id, session["event_id"]))
    conn.commit(); conn.close()
    return redirect(url_for("show_expenses"))

# ── Delete Event ──────────────────────────────────────────────────────────────
@app.route("/delete_event/<int:event_id>")
@login_required
def delete_event(event_id):
    conn = get_connection(); c = conn.cursor()
    c.execute("SELECT id FROM events WHERE id=? AND user_id=?",
              (event_id, current_user.id))
    if c.fetchone():
        c.execute("DELETE FROM expenses WHERE event_id=?", (event_id,))
        c.execute("DELETE FROM events WHERE id=?", (event_id,))
        conn.commit()
    conn.close()
    if session.get("event_id") == event_id:
        session.clear()
    return redirect(url_for("home"))

# ── Settlement helpers ────────────────────────────────────────────────────────
def compute_settlement(expenses, participants):
    paid = defaultdict(float)
    consumed = defaultdict(float)
    breakdown = defaultdict(list)
    for exp in expenses:
        share = exp["amount"] / len(exp["participants"])
        paid[exp["payer"]] += exp["amount"]
        for p in exp["participants"]:
            consumed[p] += share
            breakdown[p].append({"expense": exp["name"], "share": round(share, 2)})
    results = []
    for person in participants:
        balance = paid[person] - consumed[person]
        results.append({"person": person, "paid": round(paid[person], 2),
                        "consumed": round(consumed[person], 2),
                        "balance": round(balance, 2), "details": breakdown[person]})
    creditors, debtors = [], []
    for r in results:
        if   r["balance"] >  0.01: creditors.append({"name": r["person"], "amount": r["balance"]})
        elif r["balance"] < -0.01: debtors.append({"name": r["person"], "amount": abs(r["balance"])})
    transactions = []; i = j = 0
    while i < len(debtors) and j < len(creditors):
        amt = min(debtors[i]["amount"], creditors[j]["amount"])
        if amt > 0.01:
            transactions.append({"from": debtors[i]["name"],
                                  "to": creditors[j]["name"],
                                  "amount": round(amt, 2)})
        debtors[i]["amount"] -= amt; creditors[j]["amount"] -= amt
        if debtors[i]["amount"] < 0.01: i += 1
        if creditors[j]["amount"] < 0.01: j += 1
    return results, transactions

# ── Settlement page ───────────────────────────────────────────────────────────
@app.route("/settlement")
@login_required
def settlement():
    if "event_id" not in session: return redirect(url_for("home"))
    conn = get_connection(); c = conn.cursor()
    c.execute("SELECT * FROM expenses WHERE event_id=?", (session["event_id"],))
    rows = c.fetchall(); conn.close()
    expenses = [{"name": r["name"], "amount": r["amount"], "payer": r["payer"],
                 "participants": r["participants"].split(",")} for r in rows]
    results, transactions = compute_settlement(expenses, session["participants"])
    return render_template("settlement.html",
        event_name=session["event_name"],
        results=results, transactions=transactions)

# ── PDF ───────────────────────────────────────────────────────────────────────
def build_pdf(event_name, participants, expenses, results, transactions):
    buf = io.BytesIO()
    PAGE_W, PAGE_H = A4
    MARGIN = 18*mm
    doc = SimpleDocTemplate(buf, pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=MARGIN)
    COL = PAGE_W - 2*MARGIN

    ACCENT = colors.HexColor("#7c6dff")
    GREEN  = colors.HexColor("#34d399")
    RED    = colors.HexColor("#f87171")
    INK    = colors.HexColor("#1a1a2e")
    INK2   = colors.HexColor("#44444a")
    INK3   = colors.HexColor("#8a8a94")
    SURF2  = colors.HexColor("#f7f7f8")
    BORDER = colors.HexColor("#e4e4e8")

    def sty(name, **kw):
        d = dict(fontName="Helvetica", fontSize=9, textColor=INK2, leading=14)
        d.update(kw); return ParagraphStyle(name, **d)

    S_TITLE = sty("t",  fontName="Helvetica-Bold", fontSize=20, textColor=INK, leading=24)
    S_H2    = sty("h2", fontName="Helvetica-Bold", fontSize=11, textColor=INK, spaceBefore=16, spaceAfter=8)
    S_LABEL = sty("lb", fontName="Helvetica-Bold", fontSize=7.5, textColor=INK3)
    S_BOLD  = sty("b",  fontName="Helvetica-Bold", fontSize=9, textColor=INK)
    S_RIGHT = sty("r",  fontSize=9, textColor=INK2, alignment=TA_RIGHT)
    S_BOLDR = sty("br", fontName="Helvetica-Bold", fontSize=9, textColor=INK, alignment=TA_RIGHT)
    S_GREEN = sty("g",  fontName="Helvetica-Bold", fontSize=9, textColor=GREEN, alignment=TA_RIGHT)
    S_RED   = sty("rd", fontName="Helvetica-Bold", fontSize=9, textColor=RED,   alignment=TA_RIGHT)
    S_FOOT  = sty("ft", fontSize=7.5, textColor=INK3, alignment=TA_CENTER)
    S_NORM  = sty("n")

    story = []
    date_str = datetime.date.today().strftime("%d %B %Y")

    hd = Table([[
        Paragraph(event_name, S_TITLE),
        Paragraph(f"Settlement Report<br/><font color='#8a8a94' size='8'>{date_str}</font>",
                  sty("hr", fontSize=10, textColor=INK2, alignment=TA_RIGHT, leading=16))
    ]], colWidths=[COL*0.62, COL*0.38])
    hd.setStyle(TableStyle([
        ("VALIGN",(0,0),(-1,-1),"BOTTOM"),
        ("BOTTOMPADDING",(0,0),(-1,-1),10),
        ("LINEBELOW",(0,0),(-1,-1),0.5,BORDER),
    ]))
    story.append(hd); story.append(Spacer(1,12))

    total = sum(e["amount"] for e in expenses)
    avg   = total/len(participants) if participants else 0
    st = Table([[
        [Paragraph("TOTAL SPENT",S_LABEL), Paragraph(f"Rs. {total:,.2f}", sty("sv",fontName="Helvetica-Bold",fontSize=16,textColor=GREEN,leading=20))],
        [Paragraph("PARTICIPANTS",S_LABEL), Paragraph(str(len(participants)), sty("sv2",fontName="Helvetica-Bold",fontSize=16,textColor=ACCENT,leading=20))],
        [Paragraph("EXPENSES",S_LABEL), Paragraph(str(len(expenses)), sty("sv3",fontName="Helvetica-Bold",fontSize=16,textColor=INK,leading=20))],
        [Paragraph("AVG/PERSON",S_LABEL), Paragraph(f"Rs. {avg:,.2f}", sty("sv4",fontName="Helvetica-Bold",fontSize=16,textColor=INK,leading=20))],
    ]], colWidths=[COL/4]*4)
    st.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,-1),SURF2),("BOX",(0,0),(-1,-1),0.5,BORDER),
        ("INNERGRID",(0,0),(-1,-1),0.5,BORDER),
        ("TOPPADDING",(0,0),(-1,-1),10),("BOTTOMPADDING",(0,0),(-1,-1),10),
        ("LEFTPADDING",(0,0),(-1,-1),12),("VALIGN",(0,0),(-1,-1),"TOP"),
    ]))
    story.append(st); story.append(Spacer(1,18))

    story.append(Paragraph("Final Settlement", S_H2))
    if not transactions:
        ok = Table([[Paragraph("Everyone is fully settled — no transfers needed.",
                               sty("ok",fontSize=9,textColor=GREEN))]],colWidths=[COL])
        ok.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,-1),colors.HexColor("#ecfdf3")),
            ("BOX",(0,0),(-1,-1),0.5,colors.HexColor("#a7f3d0")),
            ("TOPPADDING",(0,0),(-1,-1),10),("BOTTOMPADDING",(0,0),(-1,-1),10),
            ("LEFTPADDING",(0,0),(-1,-1),14)]))
        story.append(ok)
    else:
        th = [[Paragraph("FROM",S_LABEL), Paragraph("",S_LABEL),
               Paragraph("TO",S_LABEL),
               Paragraph("AMOUNT",sty("lbr",fontSize=7.5,textColor=INK3,alignment=TA_RIGHT))]]
        tr = [[Paragraph(t["from"],S_BOLD),
               Paragraph("→",sty("ar",fontSize=10,textColor=INK3,alignment=TA_CENTER)),
               Paragraph(t["to"],S_BOLD),
               Paragraph(f"Rs. {t['amount']:,.2f}",S_RED)] for t in transactions]
        tt = Table(th+tr, colWidths=[COL*.32,COL*.08,COL*.32,COL*.28])
        ts = TableStyle([
            ("BACKGROUND",(0,0),(-1,0),SURF2),("LINEBELOW",(0,0),(-1,0),0.5,BORDER),
            ("BOX",(0,0),(-1,-1),0.5,BORDER),("INNERGRID",(0,1),(-1,-1),0.3,BORDER),
            ("TOPPADDING",(0,0),(-1,-1),8),("BOTTOMPADDING",(0,0),(-1,-1),8),
            ("LEFTPADDING",(0,0),(-1,-1),12),("VALIGN",(0,0),(-1,-1),"MIDDLE"),
        ])
        for i in range(1,len(tr)+1):
            if i%2==0: ts.add("BACKGROUND",(0,i),(-1,i),SURF2)
        tt.setStyle(ts); story.append(tt)

    story.append(Spacer(1,18))
    story.append(Paragraph("Expense Log", S_H2))
    eh = [[Paragraph("DESCRIPTION",S_LABEL), Paragraph("PAID BY",S_LABEL),
           Paragraph("SPLIT AMONG",S_LABEL),
           Paragraph("AMOUNT",sty("lbr2",fontSize=7.5,textColor=INK3,alignment=TA_RIGHT))]]
    er = [[Paragraph(e["name"],S_BOLD), Paragraph(e["payer"],S_NORM),
           Paragraph(", ".join(e["participants"])+"<br/><font color='#8a8a94' size='7.5'>Rs.{:.2f} each</font>".format(
               e["amount"]/len(e["participants"])), sty("pc",fontSize=8.5,textColor=INK2,leading=13)),
           Paragraph(f"Rs. {e['amount']:,.2f}",S_BOLDR)] for e in expenses]
    et = Table(eh+er, colWidths=[COL*.28,COL*.16,COL*.36,COL*.20])
    ets = TableStyle([
        ("BACKGROUND",(0,0),(-1,0),SURF2),("LINEBELOW",(0,0),(-1,0),0.5,BORDER),
        ("BOX",(0,0),(-1,-1),0.5,BORDER),("INNERGRID",(0,1),(-1,-1),0.3,BORDER),
        ("TOPPADDING",(0,0),(-1,-1),8),("BOTTOMPADDING",(0,0),(-1,-1),8),
        ("LEFTPADDING",(0,0),(-1,-1),12),("VALIGN",(0,0),(-1,-1),"TOP"),
    ])
    for i in range(1,len(er)+1):
        if i%2==0: ets.add("BACKGROUND",(0,i),(-1,i),SURF2)
    et.setStyle(ets); story.append(et); story.append(Spacer(1,18))

    story.append(Paragraph("Participant Summary", S_H2))
    ph = [[Paragraph("PARTICIPANT",S_LABEL),
           Paragraph("PAID",sty("pr",fontSize=7.5,textColor=INK3,alignment=TA_RIGHT)),
           Paragraph("CONSUMED",sty("pc2",fontSize=7.5,textColor=INK3,alignment=TA_RIGHT)),
           Paragraph("BALANCE",sty("pb",fontSize=7.5,textColor=INK3,alignment=TA_RIGHT))]]
    def bal_p(b):
        if b>0.01:  return Paragraph(f"+Rs. {b:,.2f}",S_GREEN)
        if b<-0.01: return Paragraph(f"−Rs. {abs(b):,.2f}",S_RED)
        return Paragraph("Settled",sty("ps",fontSize=9,textColor=INK3,alignment=TA_RIGHT))
    pr = [[Paragraph(r["person"],S_BOLD),
           Paragraph(f"Rs. {r['paid']:,.2f}",S_RIGHT),
           Paragraph(f"Rs. {r['consumed']:,.2f}",S_RIGHT),
           bal_p(r["balance"])] for r in results]
    pt = Table(ph+pr, colWidths=[COL*.34,COL*.22,COL*.22,COL*.22])
    pts = TableStyle([
        ("BACKGROUND",(0,0),(-1,0),SURF2),("LINEBELOW",(0,0),(-1,0),0.5,BORDER),
        ("BOX",(0,0),(-1,-1),0.5,BORDER),("INNERGRID",(0,1),(-1,-1),0.3,BORDER),
        ("TOPPADDING",(0,0),(-1,-1),9),("BOTTOMPADDING",(0,0),(-1,-1),9),
        ("LEFTPADDING",(0,0),(-1,-1),12),("VALIGN",(0,0),(-1,-1),"MIDDLE"),
    ])
    for i in range(1,len(pr)+1):
        if i%2==0: pts.add("BACKGROUND",(0,i),(-1,i),SURF2)
    pt.setStyle(pts); story.append(pt)

    story.append(Spacer(1,24))
    story.append(HRFlowable(width=COL, thickness=0.5, color=BORDER))
    story.append(Spacer(1,8))
    story.append(Paragraph(
        f"Generated by SplitEasy  ·  Developed by Prashant Umrao  ·  {date_str}",
        S_FOOT))
    doc.build(story)
    buf.seek(0); return buf

@app.route("/download_pdf")
@login_required
def download_pdf():
    if "event_id" not in session: return redirect(url_for("home"))
    conn = get_connection(); c = conn.cursor()
    c.execute("SELECT * FROM expenses WHERE event_id=?", (session["event_id"],))
    rows = c.fetchall(); conn.close()
    expenses = [{"name":r["name"],"amount":r["amount"],"payer":r["payer"],
                 "participants":r["participants"].split(",")} for r in rows]
    results, transactions = compute_settlement(expenses, session["participants"])
    buf = build_pdf(session["event_name"], session["participants"],
                    expenses, results, transactions)
    safe = session["event_name"].replace(" ","_")
    return send_file(buf, as_attachment=True,
        download_name=f"{safe}_settlement.pdf",
        mimetype="application/pdf")

# ── Debug error handler (shows real error in response) ───────────────────────
@app.errorhandler(500)
def internal_error(e):
    import traceback
    tb = traceback.format_exc()
    app.logger.error(f"500 error: {tb}")
    # Show readable error page with traceback
    return render_template("error.html", error=str(e), traceback=tb), 500

init_db()
if __name__ == "__main__":
    app.run(debug=True, port=5015)
