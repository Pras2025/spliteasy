from flask import Flask, render_template, request, redirect, url_for, session, send_file, send_from_directory
from collections import defaultdict
import sqlite3
import io
import datetime
import os

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER

app = Flask(__name__)
app.secret_key = "expense_manager_secret_key"

# ── Colors ────────────────────────────────────────────────────────────────────
ACCENT   = colors.HexColor("#5b4cff")
GREEN    = colors.HexColor("#17b26a")
RED      = colors.HexColor("#e53e3e")
AMBER    = colors.HexColor("#d97706")
INK      = colors.HexColor("#0f0f10")
INK2     = colors.HexColor("#44444a")
INK3     = colors.HexColor("#8a8a94")
SURFACE2 = colors.HexColor("#f7f7f8")
BORDER   = colors.HexColor("#e4e4e8")
WHITE    = colors.white

# ── DB ────────────────────────────────────────────────────────────────────────
def get_connection():
    conn = sqlite3.connect("expense.db")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_connection()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, participants TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id INTEGER NOT NULL, name TEXT NOT NULL,
        amount REAL NOT NULL, payer TEXT NOT NULL, participants TEXT NOT NULL,
        FOREIGN KEY(event_id) REFERENCES events(id))""")
    conn.commit(); conn.close()

# ── Home ──────────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET","POST"])
def home():
    conn = get_connection(); cursor = conn.cursor()
    if request.method == "POST":
        cursor.execute("INSERT INTO events (name,participants) VALUES (?,?)",
            (request.form["event_name"].strip(), request.form["participants"].strip()))
        conn.commit()
    cursor.execute("SELECT id,name FROM events ORDER BY id DESC")
    events = cursor.fetchall(); conn.close()
    return render_template("index.html", events=events)

# ── Open Event ────────────────────────────────────────────────────────────────
@app.route("/event/<int:event_id>")
def open_event(event_id):
    conn = get_connection(); cursor = conn.cursor()
    cursor.execute("SELECT * FROM events WHERE id=?", (event_id,))
    event = cursor.fetchone(); conn.close()
    if not event: return redirect(url_for("home"))
    session["event_id"] = event["id"]
    session["event_name"] = event["name"]
    session["participants"] = [p.strip() for p in event["participants"].split(",") if p.strip()]
    return redirect(url_for("dashboard"))

# ── Dashboard ─────────────────────────────────────────────────────────────────
@app.route("/dashboard")
def dashboard():
    if "event_id" not in session: return redirect(url_for("home"))
    conn = get_connection(); cursor = conn.cursor()
    cursor.execute("SELECT amount FROM expenses WHERE event_id=?", (session["event_id"],))
    rows = cursor.fetchall(); conn.close()
    return render_template("dashboard.html",
        event_name=session["event_name"],
        total_amount=round(sum(r["amount"] for r in rows), 2),
        participant_count=len(session["participants"]),
        expense_count=len(rows))

# ── Add Expense ───────────────────────────────────────────────────────────────
@app.route("/add_expense", methods=["GET","POST"])
def add_expense():
    if "event_id" not in session: return redirect(url_for("home"))
    if request.method == "POST":
        conn = get_connection(); cursor = conn.cursor()
        cursor.execute("INSERT INTO expenses (event_id,name,amount,payer,participants) VALUES (?,?,?,?,?)",
            (session["event_id"], request.form["expense_name"],
             float(request.form["amount"]), request.form["payer"],
             ",".join(request.form.getlist("participants"))))
        conn.commit(); conn.close()
        return redirect(url_for("show_expenses"))
    return render_template("add_expense.html",
        event_name=session["event_name"], participants=session["participants"])

# ── Show Expenses ─────────────────────────────────────────────────────────────
@app.route("/expenses")
def show_expenses():
    if "event_id" not in session: return redirect(url_for("home"))
    conn = get_connection(); cursor = conn.cursor()
    cursor.execute("SELECT * FROM expenses WHERE event_id=? ORDER BY id DESC", (session["event_id"],))
    rows = cursor.fetchall(); conn.close()
    expenses = [{"id":r["id"],"name":r["name"],"amount":r["amount"],
                 "payer":r["payer"],"participants":r["participants"].split(",")} for r in rows]
    return render_template("expenses.html", event_name=session["event_name"], expenses=expenses)

# ── Delete Expense ────────────────────────────────────────────────────────────
@app.route("/delete/<int:expense_id>")
def delete_expense(expense_id):
    if "event_id" not in session: return redirect(url_for("home"))
    conn = get_connection(); cursor = conn.cursor()
    cursor.execute("DELETE FROM expenses WHERE id=? AND event_id=?", (expense_id, session["event_id"]))
    conn.commit(); conn.close()
    return redirect(url_for("show_expenses"))

# ── Helpers ───────────────────────────────────────────────────────────────────
def compute_settlement(expenses, participants):
    paid = defaultdict(float); consumed = defaultdict(float); breakdown = defaultdict(list)
    for exp in expenses:
        share = exp["amount"] / len(exp["participants"])
        paid[exp["payer"]] += exp["amount"]
        for p in exp["participants"]:
            consumed[p] += share
            breakdown[p].append({"expense": exp["name"], "share": round(share, 2)})
    results = []
    for person in participants:
        balance = paid[person] - consumed[person]
        results.append({"person": person, "paid": round(paid[person],2),
                        "consumed": round(consumed[person],2),
                        "balance": round(balance,2), "details": breakdown[person]})
    creditors, debtors = [], []
    for r in results:
        if r["balance"] > 0: creditors.append({"name":r["person"],"amount":r["balance"]})
        elif r["balance"] < 0: debtors.append({"name":r["person"],"amount":abs(r["balance"])})
    transactions = []; i = j = 0
    while i < len(debtors) and j < len(creditors):
        amt = min(debtors[i]["amount"], creditors[j]["amount"])
        if amt > 0.01: transactions.append({"from":debtors[i]["name"],"to":creditors[j]["name"],"amount":round(amt,2)})
        debtors[i]["amount"] -= amt; creditors[j]["amount"] -= amt
        if debtors[i]["amount"] < 0.01: i += 1
        if creditors[j]["amount"] < 0.01: j += 1
    return results, transactions

# ── Settlement Page ───────────────────────────────────────────────────────────
@app.route("/settlement")
def settlement():
    if "event_id" not in session: return redirect(url_for("home"))
    conn = get_connection(); cursor = conn.cursor()
    cursor.execute("SELECT * FROM expenses WHERE event_id=?", (session["event_id"],))
    rows = cursor.fetchall(); conn.close()
    expenses = [{"name":r["name"],"amount":r["amount"],"payer":r["payer"],
                 "participants":r["participants"].split(",")} for r in rows]
    results, transactions = compute_settlement(expenses, session["participants"])
    return render_template("settlement.html", event_name=session["event_name"],
        results=results, transactions=transactions)

# ── PDF Generator ─────────────────────────────────────────────────────────────
def build_pdf(event_name, participants, expenses, results, transactions):
    buf = io.BytesIO()
    PAGE_W, PAGE_H = A4
    MARGIN = 18*mm

    doc = SimpleDocTemplate(buf, pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=MARGIN)

    # ── Styles ─────────────────────────────────────────────────────────────────
    def sty(name, **kw):
        defaults = dict(fontName="Helvetica", fontSize=9, textColor=INK, leading=14)
        defaults.update(kw)
        return ParagraphStyle(name, **defaults)

    S_TITLE   = sty("title", fontName="Helvetica-Bold", fontSize=20, textColor=INK, leading=24, spaceAfter=2)
    S_SUB     = sty("sub", fontSize=10, textColor=INK3, spaceAfter=16)
    S_H2      = sty("h2", fontName="Helvetica-Bold", fontSize=11, textColor=INK, spaceBefore=16, spaceAfter=8)
    S_LABEL   = sty("label", fontName="Helvetica-Bold", fontSize=7.5,
                    textColor=INK3, spaceBefore=0, spaceAfter=4,
                    wordWrap="CJK")
    S_NORMAL  = sty("normal", fontSize=9, textColor=INK2, leading=13)
    S_BOLD    = sty("bold", fontName="Helvetica-Bold", fontSize=9, textColor=INK)
    S_RIGHT   = sty("right", fontSize=9, textColor=INK, alignment=TA_RIGHT)
    S_BOLD_R  = sty("bold_r", fontName="Helvetica-Bold", fontSize=9, textColor=INK, alignment=TA_RIGHT)
    S_GREEN   = sty("green", fontName="Helvetica-Bold", fontSize=9, textColor=GREEN, alignment=TA_RIGHT)
    S_RED_    = sty("red", fontName="Helvetica-Bold", fontSize=9, textColor=RED, alignment=TA_RIGHT)
    S_SMALL   = sty("small", fontSize=7.5, textColor=INK3)
    S_FOOTER  = sty("footer", fontSize=7.5, textColor=INK3, alignment=TA_CENTER)

    COL = PAGE_W - 2*MARGIN
    story = []

    # ── Header band ────────────────────────────────────────────────────────────
    date_str = datetime.date.today().strftime("%d %B %Y")
    header_data = [[
        Paragraph(event_name, S_TITLE),
        Paragraph(f"Settlement Report<br/><font color='#8a8a94' size='8'>{date_str}</font>", sty("hr_right", fontSize=10, textColor=INK2, alignment=TA_RIGHT, leading=16))
    ]]
    header_tbl = Table(header_data, colWidths=[COL*0.62, COL*0.38])
    header_tbl.setStyle(TableStyle([
        ("VALIGN", (0,0), (-1,-1), "BOTTOM"),
        ("BOTTOMPADDING", (0,0), (-1,-1), 10),
        ("LINEBELOW", (0,0), (-1,-1), 0.5, BORDER),
    ]))
    story.append(header_tbl)
    story.append(Spacer(1, 12))

    # ── Summary stats ──────────────────────────────────────────────────────────
    total = sum(e["amount"] for e in expenses)
    avg = total / len(participants) if participants else 0
    stat_data = [[
        [Paragraph("TOTAL SPENT", S_LABEL), Paragraph(f"Rs. {total:,.2f}", sty("sv", fontName="Helvetica-Bold", fontSize=16, textColor=GREEN, leading=20))],
        [Paragraph("PARTICIPANTS", S_LABEL), Paragraph(str(len(participants)), sty("sv2", fontName="Helvetica-Bold", fontSize=16, textColor=ACCENT, leading=20))],
        [Paragraph("EXPENSES", S_LABEL), Paragraph(str(len(expenses)), sty("sv3", fontName="Helvetica-Bold", fontSize=16, textColor=INK, leading=20))],
        [Paragraph("AVG PER PERSON", S_LABEL), Paragraph(f"Rs. {avg:,.2f}", sty("sv4", fontName="Helvetica-Bold", fontSize=16, textColor=INK, leading=20))],
    ]]
    stat_tbl = Table(stat_data, colWidths=[COL/4]*4)
    stat_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), SURFACE2),
        ("ROUNDEDCORNERS", [4]),
        ("INNERGRID", (0,0), (-1,-1), 0.5, BORDER),
        ("BOX", (0,0), (-1,-1), 0.5, BORDER),
        ("TOPPADDING", (0,0), (-1,-1), 10),
        ("BOTTOMPADDING", (0,0), (-1,-1), 10),
        ("LEFTPADDING", (0,0), (-1,-1), 12),
        ("RIGHTPADDING", (0,0), (-1,-1), 8),
        ("VALIGN", (0,0), (-1,-1), "TOP"),
    ]))
    story.append(stat_tbl)
    story.append(Spacer(1, 18))

    # ── Who pays whom ──────────────────────────────────────────────────────────
    story.append(Paragraph("Final Settlement", S_H2))
    if not transactions:
        settled_tbl = Table([[Paragraph("Everyone is fully settled — no transfers needed.", sty("ok", fontSize=9, textColor=GREEN))]],
            colWidths=[COL])
        settled_tbl.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,-1), colors.HexColor("#ecfdf3")),
            ("BOX",(0,0),(-1,-1),0.5,colors.HexColor("#a7f3d0")),
            ("TOPPADDING",(0,0),(-1,-1),10), ("BOTTOMPADDING",(0,0),(-1,-1),10),
            ("LEFTPADDING",(0,0),(-1,-1),14),
        ]))
        story.append(settled_tbl)
    else:
        t_header = [[ Paragraph("FROM", S_LABEL), Paragraph("", S_LABEL),
                      Paragraph("TO", S_LABEL), Paragraph("AMOUNT", sty("lbl_r", fontSize=7.5, textColor=INK3, alignment=TA_RIGHT)) ]]
        t_rows = []
        for t in transactions:
            t_rows.append([
                Paragraph(t["from"], S_BOLD),
                Paragraph("→", sty("arr", fontSize=10, textColor=INK3, alignment=TA_CENTER)),
                Paragraph(t["to"], S_BOLD),
                Paragraph(f"Rs. {t['amount']:,.2f}", S_RED_),
            ])
        t_data = t_header + t_rows
        cws = [COL*0.32, COL*0.08, COL*0.32, COL*0.28]
        t_tbl = Table(t_data, colWidths=cws)
        ts = TableStyle([
            ("BACKGROUND",(0,0),(-1,0), SURFACE2),
            ("LINEBELOW",(0,0),(-1,0),0.5,BORDER),
            ("BOX",(0,0),(-1,-1),0.5,BORDER),
            ("INNERGRID",(0,1),(-1,-1),0.3,BORDER),
            ("TOPPADDING",(0,0),(-1,-1),8), ("BOTTOMPADDING",(0,0),(-1,-1),8),
            ("LEFTPADDING",(0,0),(-1,-1),12), ("RIGHTPADDING",(0,0),(-1,-1),12),
            ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
        ])
        for i,_ in enumerate(t_rows,1):
            if i%2==0: ts.add("BACKGROUND",(0,i),(-1,i),SURFACE2)
        t_tbl.setStyle(ts)
        story.append(t_tbl)

    story.append(Spacer(1, 18))

    # ── Expense log ────────────────────────────────────────────────────────────
    story.append(Paragraph("Expense Log", S_H2))
    e_header = [[Paragraph("DESCRIPTION", S_LABEL), Paragraph("PAID BY", S_LABEL),
                 Paragraph("SPLIT AMONG", S_LABEL), Paragraph("AMOUNT", sty("lbl_r2", fontSize=7.5, textColor=INK3, alignment=TA_RIGHT))]]
    e_rows = []
    for exp in expenses:
        parts_str = ", ".join(exp["participants"])
        share = exp["amount"] / len(exp["participants"])
        e_rows.append([
            Paragraph(exp["name"], S_BOLD),
            Paragraph(exp["payer"], S_NORMAL),
            Paragraph(f"{parts_str}<br/><font color='#8a8a94' size='7.5'>Rs.{share:.2f} each</font>",
                      sty("p_cell", fontSize=8.5, textColor=INK2, leading=13)),
            Paragraph(f"Rs. {exp['amount']:,.2f}", S_BOLD_R),
        ])
    e_data = e_header + e_rows
    e_cws = [COL*0.28, COL*0.16, COL*0.36, COL*0.20]
    e_tbl = Table(e_data, colWidths=e_cws)
    es = TableStyle([
        ("BACKGROUND",(0,0),(-1,0), SURFACE2),
        ("LINEBELOW",(0,0),(-1,0),0.5,BORDER),
        ("BOX",(0,0),(-1,-1),0.5,BORDER),
        ("INNERGRID",(0,1),(-1,-1),0.3,BORDER),
        ("TOPPADDING",(0,0),(-1,-1),8), ("BOTTOMPADDING",(0,0),(-1,-1),8),
        ("LEFTPADDING",(0,0),(-1,-1),12), ("RIGHTPADDING",(0,0),(-1,-1),12),
        ("VALIGN",(0,0),(-1,-1),"TOP"),
    ])
    for i,_ in enumerate(e_rows,1):
        if i%2==0: es.add("BACKGROUND",(0,i),(-1,i),SURFACE2)
    e_tbl.setStyle(es)
    story.append(e_tbl)
    story.append(Spacer(1, 18))

    # ── Per-person summary ─────────────────────────────────────────────────────
    story.append(Paragraph("Participant Summary", S_H2))
    p_header = [[Paragraph("PARTICIPANT", S_LABEL), Paragraph("PAID", sty("lh",fontSize=7.5,textColor=INK3,alignment=TA_RIGHT)),
                 Paragraph("CONSUMED", sty("lh2",fontSize=7.5,textColor=INK3,alignment=TA_RIGHT)),
                 Paragraph("BALANCE", sty("lh3",fontSize=7.5,textColor=INK3,alignment=TA_RIGHT))]]
    p_rows = []
    for r in results:
        bal = r["balance"]
        if bal > 0.01:   bal_p = Paragraph(f"+Rs. {bal:,.2f}", sty("bg",fontName="Helvetica-Bold",fontSize=9,textColor=GREEN,alignment=TA_RIGHT))
        elif bal < -0.01: bal_p = Paragraph(f"−Rs. {abs(bal):,.2f}", sty("br",fontName="Helvetica-Bold",fontSize=9,textColor=RED,alignment=TA_RIGHT))
        else:              bal_p = Paragraph("Settled", sty("bs",fontSize=9,textColor=INK3,alignment=TA_RIGHT))
        p_rows.append([
            Paragraph(r["person"], S_BOLD),
            Paragraph(f"Rs. {r['paid']:,.2f}", S_RIGHT),
            Paragraph(f"Rs. {r['consumed']:,.2f}", S_RIGHT),
            bal_p,
        ])
    p_data = p_header + p_rows
    p_cws = [COL*0.34, COL*0.22, COL*0.22, COL*0.22]
    p_tbl = Table(p_data, colWidths=p_cws)
    ps = TableStyle([
        ("BACKGROUND",(0,0),(-1,0), SURFACE2),
        ("LINEBELOW",(0,0),(-1,0),0.5,BORDER),
        ("BOX",(0,0),(-1,-1),0.5,BORDER),
        ("INNERGRID",(0,1),(-1,-1),0.3,BORDER),
        ("TOPPADDING",(0,0),(-1,-1),9), ("BOTTOMPADDING",(0,0),(-1,-1),9),
        ("LEFTPADDING",(0,0),(-1,-1),12), ("RIGHTPADDING",(0,0),(-1,-1),12),
        ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
    ])
    for i,_ in enumerate(p_rows,1):
        if i%2==0: ps.add("BACKGROUND",(0,i),(-1,i),SURFACE2)
    p_tbl.setStyle(ps)
    story.append(p_tbl)

    # ── Footer ─────────────────────────────────────────────────────────────────
    story.append(Spacer(1, 24))
    story.append(HRFlowable(width=COL, thickness=0.5, color=BORDER))
    story.append(Spacer(1, 8))
    story.append(Paragraph(f"Generated by SplitEasy  ·  {date_str}  ·  {len(participants)} participants  ·  {len(expenses)} expenses", S_FOOTER))

    doc.build(story)
    buf.seek(0)
    return buf

# ── Download PDF ──────────────────────────────────────────────────────────────
@app.route("/download_pdf")
def download_pdf():
    if "event_id" not in session: return redirect(url_for("home"))
    conn = get_connection(); cursor = conn.cursor()
    cursor.execute("SELECT * FROM expenses WHERE event_id=?", (session["event_id"],))
    rows = cursor.fetchall(); conn.close()
    expenses = [{"name":r["name"],"amount":r["amount"],"payer":r["payer"],
                 "participants":r["participants"].split(",")} for r in rows]
    results, transactions = compute_settlement(expenses, session["participants"])
    buf = build_pdf(session["event_name"], session["participants"], expenses, results, transactions)
    safe_name = session["event_name"].replace(" ","_")
    return send_file(buf, as_attachment=True,
        download_name=f"{safe_name}_settlement.pdf",
        mimetype="application/pdf")

# ── PWA Static Routes ─────────────────────────────────────────────────────────
# Service worker must be served from root scope, not /static/
@app.route("/sw.js")
def service_worker():
    return send_from_directory(
        os.path.join(app.root_path, "static"),
        "sw.js",
        mimetype="application/javascript"
    )

@app.route("/manifest.json")
def manifest():
    return send_from_directory(
        os.path.join(app.root_path, "static"),
        "manifest.json",
        mimetype="application/manifest+json"
    )

@app.route("/offline")
def offline():
    return render_template("offline.html")

# ── Run ───────────────────────────────────────────────────────────────────────
init_db()
if __name__ == "__main__":
    app.run(debug=True, port=5018)
