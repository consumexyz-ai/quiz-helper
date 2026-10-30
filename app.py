import sys, os, json, re, time, threading, ctypes, subprocess, io
import tkinter as tk
from fractions import Fraction
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from collections import deque

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # ให้พิกัดตรงกับจอจริง
except Exception:
    pass

import mss
from PIL import Image, ImageOps, ImageChops
from math import gcd
from rapidfuzz import process, fuzz

BASE = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
QA_FILE = os.path.join(BASE, "qa.json")
CFG_FILE = os.path.join(BASE, "config.json")
TESS_DIR = os.path.join(BASE, "tesseract")
TESS_EXE = os.path.join(TESS_DIR, "tesseract.exe")


def norm(s):
    return re.sub(r"[\s\W_]+", "", s).lower()


# ------------------------------------------------------------------ เครื่องคิดเลข

_THAI = re.compile(r"[\u0e00-\u0e7f]")
_THDIG = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")
_FIX = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1", "|": "1", "S": "5", "B": "8"})
_NUM = r"\d+(?:\.\d+)?"
_ATOM = rf"(?:\d+\s*/\s*\d+(?![\d.])|{_NUM})"   # เศษส่วนจำนวนเต็ม a/b หรือตัวเลข/ทศนิยม
_EXPR = re.compile(rf"{_ATOM}(?:\s*[-+*÷/:]\s*{_ATOM})+")
_TOK = re.compile(rf"{_ATOM}|[-+*÷/:]")


def _fmt(d):
    return format(d.quantize(Decimal("0.000001")).normalize(), "f")


def _unsure_operator(t):
    if not re.search(r"[=＝?？]", t) or re.search(r"\d\s*/\s*\d", t):
        return None
    nums = re.findall(r"\d+(?:\.\d+)?", t)
    rest = re.sub(r"[\s=＝?？.,:;!]", "", t)
    if len(nums) != 2 or len("".join(nums)) < 0.6 * len(rest):
        return None
    a, b = Decimal(nums[0]), Decimal(nums[1])
    ans = f"ไม่แน่ใจเครื่องหมาย\n+ → {_fmt(a + b)}\n− → {_fmt(a - b)}\n× → {_fmt(a * b)}"
    if b != 0:
        ans += f"\n÷ → {_fmt(a / b)}"
    return f"{nums[0]} ? {nums[1]}", ans, None


def solve_math(text, lenient=False):
    """โจทย์เลขล้วน เช่น '15 + 27 =?' หรือ '3/4 + 1/6 =?'
    คืน (นิพจน์สำหรับแสดง, คำตอบ, นิพจน์มาตรฐาน) หรือ None ถ้าไม่ใช่โจทย์เลขล้วน"""
    t = text.translate(_THDIG)
    if _THAI.search(t):
        if not lenient:
            return None
        t = _THAI.sub("§", t)             # ตัวไทยแทนด้วยตัวกั้น ไม่ให้นิพจน์ต่อข้ามคำไทย
    t = re.sub(r"[×✕✖xX]", "*", t)
    t = re.sub(r"[−–—‒]", "-", t)
    t = t.translate(_FIX)
    t = re.sub(r"(?<=\d),(?=\d)", ".", t)
    ms_ = list(_EXPR.finditer(t))
    if lenient:                           # ต้องมี = หรือ ? ตามหลังนิพจน์ทันที
        ms_ = [m for m in ms_ if re.match(r"\s*[=＝?？]", t[m.end():])]
    found = [m.group(0) for m in ms_]
    if not found:
        return None if lenient else _unsure_operator(t)
    best = max(found, key=len)
    rest = re.sub(r"[\s=＝?？.,:;!]", "", t)
    if not lenient and len(re.sub(r"\s", "", best)) < 0.6 * len(rest):
        return None                      # มีข้อความอื่นปนเยอะ ไม่ใช่โจทย์เลขล้วน
    toks = [re.sub(r"\s+", "", x) for x in _TOK.findall(best)]
    toks = ["÷" if x in ("/", ":") else x for x in toks]
    if len(toks) == 1:                    # มีตัวเลข/เศษส่วนก้อนเดียว ไม่มีเครื่องหมาย
        if "/" in toks[0] and re.search(r"[=＝?？]", t):
            a, b = toks[0].split("/")     # เช่น "72 / 8 =?" ที่ OCR อ่าน ÷ เป็น /
            toks = [a, "÷", b]
        else:
            return None                   # เช่นหัวข้อ "ข้อที่ 2/2" ไม่ใช่โจทย์เลข
    has_frac = any("/" in x for x in toks)

    def val(x):
        if "/" in x:
            a, b = x.split("/")
            return Fraction(int(a), int(b)) if int(b) else None
        return Fraction(x)

    nums = [val(x) for x in toks[0::2]]
    if any(n is None for n in nums):
        return None
    ops = toks[1::2]
    vals, vop = [nums[0]], []
    for op, n in zip(ops, nums[1:]):
        if op in "*÷":
            if op == "÷" and n == 0:
                return None
            vals[-1] = vals[-1] * n if op == "*" else vals[-1] / n
        else:
            vop.append(op)
            vals.append(n)
    r = vals[0]
    for op, n in zip(vop, vals[1:]):
        r = r + n if op == "+" else r - n

    if has_frac:
        ans = str(r.numerator) if r.denominator == 1 else f"{r.numerator}/{r.denominator}"
    else:
        ans = _fmt(Decimal(r.numerator) / Decimal(r.denominator))
    pretty = " ".join(toks).replace("*", "×")
    return pretty, ans, "".join(toks)


def find_math(lines, definite_only=False):
    """ตรวจทีละบรรทัด (หัวข้อ 'ข้อที่ 2/2' และป้ายหมวดอยู่คนละบรรทัด จะไม่มารบกวน)"""
    unsure = None
    for ln in lines:
        r = solve_math(ln)
        if r and r[2]:
            return r
        if r and unsure is None:
            unsure = r
    return None if definite_only else unsure


def solve_words(lines):
    """ห.ร.ม. / ค.ร.น. ของสองจำนวน"""
    for i, ln in enumerate(lines):
        n = norm(ln)
        kind = None
        if "หรม" in n or fuzz.partial_ratio("ตัวหารร่วมมาก", n) >= 80:
            kind = "ห.ร.ม."
        elif "ครน" in n or fuzz.partial_ratio("ตัวคูณร่วมน้อย", n) >= 80:
            kind = "ค.ร.น."
        if not kind:
            continue
        txt = ln
        if len(re.findall(r"\d+", txt)) < 2 and i + 1 < len(lines):
            txt += " " + lines[i + 1]
        nums = [int(x) for x in re.findall(r"\d+", txt)]
        if len(nums) >= 2 and nums[0] and nums[1]:
            a, b = nums[0], nums[1]
            g = gcd(a, b)
            return f"{kind} ของ {a} และ {b}", str(g if kind == "ห.ร.ม." else a * b // g)
    return None


def best_db(lines, nq):
    """ลองจับคู่ทุกช่วง 1-3 บรรทัดติดกัน เลือกช่วงที่ตรงที่สุด (ตัดหัวข้อ/ป้ายหมวดที่ปนมาออกเอง)"""
    L = [norm(x) for x in lines]
    best = None
    for i in range(len(L)):
        for j in range(i, min(i + 3, len(L))):
            c = "".join(L[i:j + 1])
            if len(c) < 6:
                continue
            ms = process.extract(c, nq, scorer=fuzz.ratio, limit=2)
            if ms and (best is None or ms[0][1] > best[0][1]):
                best = ms
    return best


def decide(lines_fast, get_full, cancel_full, qa, keys, nq, mathdb):
    """คืน (สำเร็จไหม, ข้อความคำตอบ, หมายเหตุ, บรรทัดทั้งหมดที่อ่านได้)"""
    m = find_math(lines_fast, definite_only=True)
    if m:
        cancel_full()                                  # เป็นโจทย์เลขแล้ว ไม่ต้องรออ่านไทย
        lines = lines_fast
    else:
        lines = get_full()
        m = find_math(lines) or find_math(lines_fast)

    if m:                                              # 1) โจทย์เลขล้วน -> คำนวณเอง
        pretty, ans, canon = m
        db = mathdb.get(canon) if canon else None
        if db and db != ans:
            return True, f"ไม่แน่ใจ: {ans} หรือ {db}", f"อ่านโจทย์ได้: {pretty} = ?  (คำนวณกับฐานข้อมูลไม่ตรงกัน)", lines
        return True, ans, f"อ่านโจทย์ได้: {pretty} = ?" + ("  ✔ ตรงฐานข้อมูล" if db else ""), lines

    w = solve_words(lines)                             # 2) ห.ร.ม. / ค.ร.น.
    if w:
        return True, w[1], f"อ่านโจทย์ได้: {w[0]} (คำนวณเอง)", lines

    ms = best_db(lines, nq)                            # 3) โจทย์ข้อความ -> ฐานข้อมูล
    if ms and ms[0][1] >= 60:
        picks = [ms[0]]
        if len(ms) > 1 and ms[0][1] - ms[1][1] < 4 and ms[1][1] >= 60:
            picks.append(ms[1])
        answers = [qa[keys[x[2]]] for x in picks]
        note = " / ".join(f"{keys[x[2]]} ({int(x[1])}%)" for x in picks)
        return True, (answers[0] if len(answers) == 1 else "ไม่แน่ใจ:\n" + "\n".join(answers)), note, lines

    # 4) หาโจทย์ไม่เจอ -> ลองอ่านเป็นโจทย์เลขแบบผ่อนปรน (ตัดตัวไทยทิ้ง แล้วคิดเลขที่มี = หรือ ? ตามหลัง)
    for src in (lines, lines_fast):
        for cand in list(src) + [" ".join(src)]:
            r = solve_math(cand, lenient=True)
            if r and r[2]:
                return True, r[1], f"ไม่พบในฐานข้อมูล → คำนวณเอง: {r[0]} = ?", lines

    if not ms:
        return False, "อ่านโจทย์ไม่ได้", "กำลังลองอ่านซ้ำ... (ถ้าไม่หาย ลองกด 'ตั้งพื้นที่' ใหม่)", lines
    return False, "ไม่พบโจทย์นี้ในฐานข้อมูล", "OCR: " + " / ".join(lines)[:70], lines


# ------------------------------------------------------------------ OCR
def ocr_lines(img, lang="tha+eng", holder=None):
    if not os.path.exists(TESS_EXE):
        raise RuntimeError(f"ไม่พบ {TESS_EXE} (โฟลเดอร์ tesseract ต้องอยู่ข้างไฟล์ exe)")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    env = dict(os.environ, TESSDATA_PREFIX=os.path.join(TESS_DIR, "tessdata"), OMP_THREAD_LIMIT="1")
    p = subprocess.Popen([TESS_EXE, "stdin", "stdout", "-l", lang, "--oem", "1", "--psm", "6",
                          "-c", "tessedit_do_invert=0"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         env=env, creationflags=0x08004000)   # ไม่เปิดหน้าต่าง + ลำดับความสำคัญต่ำ (ไม่ทำเกมกระตุก)
    if holder is not None:
        holder["p"] = p
    out, err = p.communicate(buf.getvalue())
    if holder is not None and holder.get("cancel"):
        return []
    if p.returncode != 0:
        raise RuntimeError(err.decode("utf-8", "ignore")[:300])
    return [l.strip() for l in out.decode("utf-8", "ignore").splitlines() if l.strip()]


def cancel(holder):
    holder["cancel"] = True
    try:
        holder["p"].kill()
    except Exception:
        pass


def grab_raw(sct, region):
    s = sct.grab(region)
    return Image.frombytes("RGB", s.size, s.bgra, "raw", "BGRX")


def prep(img):
    k = 3 if img.width < 300 else 2   # ภาพเล็กขยายมากขึ้นให้ OCR อ่านง่าย
    img = img.resize((img.width * k, img.height * k), Image.LANCZOS)
    return ImageOps.autocontrast(img.convert("L"))


CHANGE = 15                       # พิกเซลที่เปลี่ยนชัดเจนอย่างน้อยเท่านี้ ถือว่าภาพเปลี่ยน
_LUT = [0] * 41 + [255] * 215


def sig(img, maxw=320):
    g = img.convert("L")
    if g.width > maxw:
        g = g.resize((maxw, max(1, g.height * maxw // g.width)))
    return g


def sdiff(a, b):
    if a.size != b.size:
        return 10 ** 9
    return ImageChops.difference(a, b).point(_LUT).histogram()[255]


def pick_region(root, title):
    sel, st = {}, {}
    top = tk.Toplevel(root)
    top.attributes("-fullscreen", True, "-alpha", 0.3, "-topmost", True)
    top.configure(bg="black", cursor="cross")
    cv = tk.Canvas(top, bg="black", highlightthickness=0)
    cv.pack(fill="both", expand=True)
    cv.create_text(top.winfo_screenwidth() // 2, 60, fill="white", font=("Tahoma", 22),
                   text=title + "  (ลากเมาส์ / Esc ยกเลิก)")

    def down(e):
        st["x"], st["y"] = e.x, e.y
        st["r"] = cv.create_rectangle(e.x, e.y, e.x, e.y, outline="red", width=3)

    def move(e):
        cv.coords(st["r"], st["x"], st["y"], e.x, e.y)

    def up(e):
        x1, x2 = sorted((st["x"], e.x))
        y1, y2 = sorted((st["y"], e.y))
        if x2 - x1 > 10 and y2 - y1 > 10:
            sel.update(left=x1, top=y1, width=x2 - x1, height=y2 - y1)
        top.destroy()

    cv.bind("<ButtonPress-1>", down)
    cv.bind("<B1-Motion>", move)
    cv.bind("<ButtonRelease-1>", up)
    top.bind("<Escape>", lambda e: top.destroy())
    root.wait_window(top)
    return sel or None


# ------------------------------------------------------------------ app
class App:
    def __init__(self):
        self.root = r = tk.Tk()
        r.title("ROX Quiz Helper")
        r.attributes("-topmost", True)
        r.geometry("380x190+40+40")
        r.configure(bg="#1b1b1b")
        self.ans = tk.Label(r, text="", font=("Tahoma", 16, "bold"), bg="#1b1b1b",
                            fg="#7CFC00", wraplength=360, justify="left")
        self.ans.pack(fill="both", expand=True, padx=8, pady=(8, 0))
        self.info = tk.Label(r, text="", font=("Tahoma", 9), bg="#1b1b1b", fg="#aaa",
                             wraplength=360, justify="left")
        self.info.pack(fill="x", padx=8)
        tk.Button(r, text="ตั้งพื้นที่", command=self.setup).pack(pady=6)

        self.qa = json.load(open(QA_FILE, encoding="utf-8"))
        self.keys = list(self.qa)
        self.nq = [norm(k) for k in self.keys]
        # โจทย์เลขในฐานข้อมูล ไว้ตรวจเทียบกับผลที่คำนวณได้
        self.mathdb = {}
        for k, v in self.qa.items():
            m = solve_math(k)
            if m and m[2]:
                self.mathdb[m[2]] = v.strip()
        self.cfg = json.load(open(CFG_FILE)) if os.path.exists(CFG_FILE) else {}
        self.ans.config(text="รอโจทย์..." if self.cfg.get("q") else "กด 'ตั้งพื้นที่' ก่อนครับ")
        self.info.config(text=f"ฐานข้อมูล {len(self.qa)} ข้อ")
        self.running = True
        threading.Thread(target=self.loop, daemon=True).start()
        r.protocol("WM_DELETE_WINDOW", self.close)
        r.mainloop()

    def close(self):
        self.running = False
        self.root.destroy()

    def setup(self):
        self.root.withdraw()
        time.sleep(0.3)
        q = pick_region(self.root, "ลากคลุม 'ช่องโจทย์' ให้พอดีกับข้อความ")
        self.root.deiconify()
        if q:
            self.cfg = {"q": q}
            json.dump(self.cfg, open(CFG_FILE, "w"))
            self.ans.config(text="รอโจทย์...")

    def log(self, lines, why):
        try:
            p = os.path.join(BASE, "debug_log.txt")
            if os.path.exists(p) and os.path.getsize(p) > 200_000:
                return
            with open(p, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%H:%M:%S')} [{why}] {lines}\n")
        except Exception:
            pass

    def loop(self):
        prev = done = None
        stable = retries = 0
        cleared = False
        changed_since = retry_at = None
        thr = CHANGE                       # ความไวในการตรวจว่าภาพเปลี่ยน (ปรับเองถ้าในกรอบมีเอฟเฟกต์ขยับตลอด)
        recent = deque(maxlen=12)
        ex = ThreadPoolExecutor(2)
        with mss.mss() as sct:
            while self.running:
                time.sleep(0.05)
                if not self.cfg.get("q"):
                    continue
                try:
                    now = time.time()
                    sg = sig(grab_raw(sct, self.cfg["q"]))
                    d = sdiff(sg, prev) if prev is not None else 0
                    recent.append(d)
                    if len(recent) == recent.maxlen and max(recent) < CHANGE:
                        thr = CHANGE                  # กรอบนิ่งแล้ว กลับไปใช้ความไวปกติ
                    stable = stable + 1 if prev is not None and d < thr else 0
                    prev = sg
                    differs = done is None or sdiff(sg, done) >= thr
                    if differs:
                        if changed_since is None:
                            changed_since = now
                        if not cleared:
                            self.show("กำลังอ่านโจทย์...", "")     # ลบคำตอบข้อเก่าทันที กันเข้าใจผิด
                            cleared = True
                    else:
                        changed_since = None
                    force = differs and changed_since is not None and now - changed_since > 0.8
                    if differs:
                        if stable < 2 and not force:      # รอภาพนิ่ง (หรือบังคับอ่านถ้าภาพขยับไม่หยุด)
                            continue
                        if force and stable < 2:          # ภาพขยับไม่หยุด: ลดความไวตามระดับรบกวนที่วัดได้
                            thr = max(CHANGE, int(sorted(recent)[len(recent) // 2] * 3) + 5)
                        retries = 0
                    else:                                  # ภาพเดิม: อ่านซ้ำเฉพาะเมื่อครั้งก่อนไม่สำเร็จ
                        if retry_at is None or now < retry_at or retries >= 6:
                            continue
                        retries += 1
                    done, cleared, changed_since, retry_at = sg, False, None, None

                    img = prep(grab_raw(sct, self.cfg["q"]))
                    hold = {}
                    f_fast = ex.submit(ocr_lines, img, "eng")              # เร็ว: เหมาะกับโจทย์เลข
                    f_full = ex.submit(ocr_lines, img, "tha+eng", hold)     # เต็ม: โจทย์ภาษาไทย
                    ok, text, note, lines = decide(f_fast.result(), f_full.result,
                                                   lambda: cancel(hold),
                                                   self.qa, self.keys, self.nq, self.mathdb)
                    self.show(text, note)
                    if not ok:
                        retry_at = time.time() + 1.0
                        self.log(lines, text)
                except Exception as e:
                    self.show("OCR error", str(e)[:400])
                    retry_at = time.time() + 1.0

    def show(self, a, i):
        self.root.after(0, lambda: (self.ans.config(text=a), self.info.config(text=i)))


if __name__ == "__main__":
    App()
