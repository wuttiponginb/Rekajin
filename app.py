import base64, json, os, re, sqlite3, uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

ROOT = Path(__file__).resolve().parent
DB = ROOT / "fairprice.db"
UPLOADS = ROOT / "uploads"
SEED = ROOT / "seed.json"
UPLOADS.mkdir(exist_ok=True)

app = Flask(__name__, static_folder="public", static_url_path="")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "Dodoman123")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "wuttipong.inb@gmail.com")
TOKENS = {}


def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def init():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS categories (id TEXT PRIMARY KEY, group_name TEXT, name TEXT, unit TEXT, size_hint TEXT, summary TEXT, scope_note TEXT);
    CREATE TABLE IF NOT EXISTS reference_prices (id TEXT PRIMARY KEY, category_id TEXT, zone TEXT, size_band TEXT, low REAL, mid REAL, high REAL, unit TEXT, includes TEXT, source TEXT, as_of TEXT);
    CREATE TABLE IF NOT EXISTS guides (id INTEGER PRIMARY KEY AUTOINCREMENT, category_id TEXT, sort INTEGER, question TEXT, why TEXT);
    CREATE TABLE IF NOT EXISTS submissions (id TEXT PRIMARY KEY, created_at TEXT, category_id TEXT, zone TEXT, province TEXT, district TEXT, size_value REAL, size_unit TEXT, price REAL, scope TEXT, material_grade TEXT, job_month TEXT, fairness TEXT, note TEXT, source TEXT, status TEXT, contact_email TEXT, evidence TEXT);
    CREATE TABLE IF NOT EXISTS unlocks (code TEXT PRIMARY KEY, created_at TEXT, submission_id TEXT);
    CREATE TABLE IF NOT EXISTS quotes (id TEXT PRIMARY KEY, created_at TEXT, category_id TEXT, zone TEXT, size_band TEXT, quoted_price REAL, benchmark_mid REAL, diff_pct REAL, verdict TEXT);
    CREATE TABLE IF NOT EXISTS providers (id TEXT PRIMARY KEY, name TEXT, groups TEXT, zone TEXT, province TEXT, fairness_score REAL, review_count INTEGER, price_note TEXT, contact TEXT, status TEXT);
    CREATE TABLE IF NOT EXISTS reviews (id TEXT PRIMARY KEY, created_at TEXT, provider_id TEXT, score REAL, note TEXT, status TEXT);
    CREATE TABLE IF NOT EXISTS contacts (id TEXT PRIMARY KEY, created_at TEXT, name TEXT, email TEXT, message TEXT);
    CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, created_at TEXT, kind TEXT, name TEXT, email TEXT, phone TEXT, detail TEXT, amount INTEGER, status TEXT);
    """)
    if con.execute("SELECT COUNT(*) c FROM categories").fetchone()["c"] == 0 and SEED.exists():
        seed = json.loads(SEED.read_text())
        for r in seed["cats"]:
            con.execute("INSERT OR IGNORE INTO categories VALUES (?,?,?,?,?,?,?)", r[:7])
        for r in seed["refs"]:
            con.execute("INSERT OR IGNORE INTO reference_prices VALUES (?,?,?,?,?,?,?,?,?,?,?)", r[:11])
        for r in seed["guides"]:
            con.execute("INSERT INTO guides (category_id, sort, question, why) VALUES (?,?,?,?)", r[:4])
        for r in seed["providers"]:
            con.execute("INSERT OR IGNORE INTO providers VALUES (?,?,?,?,?,?,?,?,?,?)", r[:10])
        for r in seed["submissions"]:
            created = r[1] if isinstance(r[1], str) else str(r[1])
            row = [r[0], created] + list(r[2:16])
            if len(row) < 18:
                row += ["", ""]
            con.execute("INSERT OR IGNORE INTO submissions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row[:18])
    con.commit()
    con.close()


def rows(sql, args=()):
    con = db()
    out = [dict(r) for r in con.execute(sql, args).fetchall()]
    con.close()
    return out


def code():
    return uuid.uuid4().hex[:8].upper()


def judge(price, low, mid, high):
    if price < low * 0.75:
        return {"label": "ต่ำกว่าช่วงที่พบบ่อย", "tone": "warn", "text": "ต่ำกว่าช่วงที่พบได้บ่อย อาจเป็นคนละขอบเขตงาน ควรสอบรายการที่รวมและไม่รวมก่อนตัดสินใจ"}
    if price <= mid:
        return {"label": "อยู่ในช่วงล่างถึงกลาง", "tone": "ok", "text": "อยู่ในช่วงที่พบได้บ่อยจากข้อมูลอ้างอิงชุดนี้"}
    if price <= high:
        return {"label": "สูงกว่าค่ากลางของชุดข้อมูล", "tone": "warn", "text": "สูงกว่าค่ากลางของชุดข้อมูลนี้ อาจมีเหตุผลจากวัสดุ ขอบเขตงาน หรือการรับประกัน ควรสอบรายละเอียดกับผู้เสนอราคา"}
    return {"label": "สูงกว่าช่วงบนของชุดข้อมูล", "tone": "warn", "text": "สูงกว่าช่วงบนของชุดข้อมูลนี้ ไม่ได้หมายความว่าราคาไม่เหมาะสม อาจเป็นงานยากกว่า วัสดุคนละเกรด หรือมีประกันงาน"}


def save_photo(data_url, sid):
    if not data_url:
        return ""
    m = re.match(r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.+)$", data_url)
    if not m:
        raise ValueError("ไฟล์รูปไม่ถูกต้อง")
    raw = base64.b64decode(m.group(2))
    if len(raw) > 1_500_000:
        raise ValueError("รูปใหญ่เกิน 1.5MB")
    ext = "png" if "png" in m.group(1) else "jpg"
    name = f"{sid}.{ext}"
    (UPLOADS / name).write_bytes(raw)
    return f"/uploads/{name}"


@app.get("/")
def home():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/uploads/<path:name>")
def upload(name):
    return send_from_directory(UPLOADS, name)


@app.post("/api/<name>")
def api(name):
    body = request.get_json(silent=True) or {}
    args = body.get("args") or []
    try:
        data = HANDLERS[name](*args)
        return jsonify({"ok": True, "data": data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


def get_bootstrap():
    cats = rows("SELECT * FROM categories ORDER BY group_name, name")
    refs = rows("SELECT * FROM reference_prices")
    guides = rows("SELECT category_id, sort, question, why FROM guides ORDER BY sort")
    providers = rows("SELECT * FROM providers WHERE status != 'hidden'")
    reports = rows("SELECT COUNT(*) c FROM submissions WHERE status='published'")[0]["c"]
    return {"categories": cats, "reference": refs, "guide": guides, "providers": providers, "stats": {"reports": reports, "user_reports": reports}, "generated_at": datetime.now().isoformat(timespec="minutes")}


def submit_price(payload=None):
    p = payload or {}
    price = float(p.get("price") or 0)
    if not p.get("category_id") or not p.get("zone"):
        raise ValueError("กรุณาเลือกหมวดและพื้นที่")
    if price <= 0 or price > 5000000:
        raise ValueError("ราคาไม่ถูกต้อง")
    evidence = str(p.get("evidence") or "").strip()
    photo = str(p.get("photo") or "")
    if len(evidence) < 8 and not photo:
        raise ValueError("ใส่ข้อความหลักฐานอย่างน้อย 8 ตัวอักษร หรือแนบรูปใบเสร็จ")
    sid = "U" + uuid.uuid4().hex[:8]
    photo_url = save_photo(photo, sid)
    text = evidence + (("\n" + photo_url) if photo_url else "")
    unlock = code()
    now = datetime.now().isoformat(timespec="seconds")
    con = db()
    con.execute("INSERT INTO submissions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
        sid, now, p.get("category_id"), p.get("zone"), str(p.get("province") or "")[:40], str(p.get("district") or "")[:40],
        float(p.get("size_value") or 0), str(p.get("size_unit") or "")[:20], round(price), str(p.get("scope") or "")[:300],
        str(p.get("material_grade") or "")[:40], str(p.get("job_month") or "")[:7], str(p.get("fairness") or "")[:20],
        str(p.get("note") or "")[:400], "user", "pending", str(p.get("contact_email") or "")[:80], text[:500]
    ))
    con.execute("INSERT INTO unlocks VALUES (?,?,?)", (unlock, now, sid))
    con.commit()
    con.close()
    return {"ok": True, "unlock_code": unlock, "id": sid, "status": "pending"}


def verify_unlock(c):
    c = str(c or "").strip().upper()
    if c == "DEMO2026":
        return {"ok": True}
    hit = rows("SELECT code FROM unlocks WHERE code=?", (c,))
    return {"ok": bool(hit)}


def get_peer_prices(c, category_id=""):
    if not verify_unlock(c)["ok"]:
        return {"ok": False, "rows": [], "message": "รหัสนี้ใช้ไม่ได้ รหัสทดลองคือ DEMO2026"}
    sql = "SELECT * FROM submissions WHERE status='published'"
    args = []
    if category_id:
        sql += " AND category_id=?"
        args.append(category_id)
    sql += " ORDER BY created_at DESC LIMIT 80"
    return {"ok": True, "rows": rows(sql, args), "message": ""}


def check_quote(payload=None):
    p = payload or {}
    price = float(p.get("quoted_price") or 0)
    refs = rows("SELECT * FROM reference_prices WHERE category_id=?", (p.get("category_id"),))
    zone = p.get("zone") or "กทม.และปริมณฑล"
    ref = next((r for r in refs if r["zone"] == zone and r["size_band"] == p.get("size_band")), None) or next((r for r in refs if r["zone"] == zone), None) or (refs[0] if refs else None)
    if not ref:
        raise ValueError("ยังไม่มีช่วงราคาอ้างอิงของหมวดนี้")
    diff = round((price - ref["mid"]) / ref["mid"] * 100) if ref["mid"] else 0
    verdict = judge(price, ref["low"], ref["mid"], ref["high"])
    questions = rows("SELECT question, why FROM guides WHERE category_id=? ORDER BY sort LIMIT 6", (p.get("category_id"),))
    peers = rows("SELECT price FROM submissions WHERE status='published' AND category_id=?", (p.get("category_id"),))
    return {"verdict": verdict, "size_band": ref["size_band"], "low": ref["low"], "mid": ref["mid"], "high": ref["high"], "unit": ref["unit"], "diff_pct": diff, "includes": ref["includes"], "source": ref["source"], "questions": questions, "peer_count": len(peers), "peer_median": sorted([x["price"] for x in peers])[len(peers)//2] if peers else None}


def submit_review(payload=None):
    p = payload or {}
    con = db()
    con.execute("INSERT INTO reviews VALUES (?,?,?,?,?,?)", ("R" + uuid.uuid4().hex[:8], datetime.now().isoformat(timespec="seconds"), p.get("provider_id"), float(p.get("score") or 0), str(p.get("note") or "")[:300], "published"))
    con.commit()
    con.close()
    return {"ok": True}


def submit_order(payload=None):
    p = payload or {}
    kind = str(p.get("kind") or "")
    prices = {"quote": 149, "listing": 990, "report": 2900}
    if kind not in prices:
        raise ValueError("ไม่พบแพ็กเกจนี้")
    if not p.get("name") or "@" not in str(p.get("email") or "") or len(str(p.get("detail") or "")) < 8:
        raise ValueError("กรอกชื่อ อีเมล และรายละเอียดให้ครบ")
    oid = "O" + uuid.uuid4().hex[:8]
    con = db()
    con.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?)", (
        oid, datetime.now().isoformat(timespec="seconds"), kind, str(p.get("name"))[:80], str(p.get("email"))[:80],
        str(p.get("phone") or "")[:30], str(p.get("detail"))[:800], prices[kind], "new"
    ))
    con.commit()
    con.close()
    return {"ok": True, "id": oid, "amount": prices[kind], "email": ADMIN_EMAIL}


def submit_dispute(payload=None):
    p = payload or {}
    if not p.get("name") or "@" not in str(p.get("email") or "") or len(str(p.get("message") or "")) < 12:
        raise ValueError("กรอกชื่อ อีเมล และเหตุผลที่ขอทบทวนให้ครบ")
    con = db()
    con.execute("INSERT INTO contacts VALUES (?,?,?,?,?)", (
        "D" + uuid.uuid4().hex[:8], datetime.now().isoformat(timespec="seconds"),
        "ทบทวน: " + str(p.get("name"))[:60], p.get("email"), str(p.get("message"))[:800]
    ))
    con.commit()
    con.close()
    return {"ok": True}


def submit_contact(payload=None):
    p = payload or {}
    if not p.get("name") or not p.get("message") or "@" not in str(p.get("email") or ""):
        raise ValueError("กรอกชื่อ อีเมล และข้อความให้ครบ")
    con = db()
    con.execute("INSERT INTO contacts VALUES (?,?,?,?,?)", ("C" + uuid.uuid4().hex[:8], datetime.now().isoformat(timespec="seconds"), p.get("name"), p.get("email"), p.get("message")))
    con.commit()
    con.close()
    return {"ok": True}


def admin_login(user, password):
    if user != ADMIN_USER or password != ADMIN_PASS:
        return {"ok": False, "message": "ชื่อหรือรหัสไม่ถูก"}
    token = uuid.uuid4().hex
    TOKENS[token] = True
    return {"ok": True, "token": token, "email": ADMIN_EMAIL}


def need(token):
    if token not in TOKENS:
        raise ValueError("หมดเวลาเข้าสู่ระบบ หรือยังไม่ได้ล็อกอิน")


def admin_pending(token):
    need(token)
    return rows("SELECT * FROM submissions WHERE status='pending' ORDER BY created_at DESC")


def admin_set_status(token, sid, status):
    need(token)
    if status not in ("published", "hidden", "pending"):
        raise ValueError("สถานะไม่ถูกต้อง")
    con = db()
    con.execute("UPDATE submissions SET status=? WHERE id=?", (status, sid))
    con.commit()
    con.close()
    return {"ok": True}


def admin_save_category(token, payload=None):
    need(token)
    p = payload or {}
    if not p.get("id"):
        raise ValueError("ต้องมีรหัสหมวด")
    con = db()
    con.execute("INSERT INTO categories VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET group_name=excluded.group_name, name=excluded.name, unit=excluded.unit, size_hint=excluded.size_hint, summary=excluded.summary, scope_note=excluded.scope_note", (p.get("id"), p.get("group_name"), p.get("name"), p.get("unit"), p.get("size_hint"), p.get("summary"), p.get("scope_note")))
    con.commit()
    con.close()
    return {"ok": True}


def admin_save_reference(token, payload=None):
    need(token)
    p = payload or {}
    rid = p.get("id") or f"{p.get('category_id')}-{uuid.uuid4().hex[:6]}"
    con = db()
    con.execute("INSERT INTO reference_prices VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET category_id=excluded.category_id, zone=excluded.zone, size_band=excluded.size_band, low=excluded.low, mid=excluded.mid, high=excluded.high, unit=excluded.unit, includes=excluded.includes, source=excluded.source, as_of=excluded.as_of", (rid, p.get("category_id"), p.get("zone"), p.get("size_band"), float(p.get("low") or 0), float(p.get("mid") or 0), float(p.get("high") or 0), p.get("unit") or "บาท", p.get("includes"), "แอดมิน", "2569"))
    con.commit()
    con.close()
    return {"ok": True}


def admin_delete(token, sheet, sid):
    need(token)
    table = {"Categories": "categories", "ReferencePrices": "reference_prices", "Submissions": "submissions", "Providers": "providers", "Reviews": "reviews"}.get(sheet)
    if not table:
        raise ValueError("ลบตารางนี้ไม่ได้")
    con = db()
    con.execute(f"DELETE FROM {table} WHERE id=?", (sid,))
    con.commit()
    con.close()
    return {"ok": True}


HANDLERS = {
    "getBootstrap": get_bootstrap,
    "submitPrice": submit_price,
    "verifyUnlock": verify_unlock,
    "getPeerPrices": get_peer_prices,
    "checkQuote": check_quote,
    "submitReview": submit_review,
    "submitContact": submit_contact,
    "submitOrder": submit_order,
    "submitDispute": submit_dispute,
    "adminLogin": admin_login,
    "adminPending": admin_pending,
    "adminSetStatus": admin_set_status,
    "adminSaveCategory": admin_save_category,
    "adminSaveReference": admin_save_reference,
    "adminDelete": admin_delete,
}

init()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
