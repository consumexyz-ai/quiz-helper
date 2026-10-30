import sys, os, json, re, time, threading, ctypes, subprocess, io
import tkinter as tk
from fractions import Fraction
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # ให้พิกัดตรงกับจอจริง
except Exception:
    pass

import mss
from PIL import Image, ImageOps
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
    nums = re.findall(r"\d+(?:\.\d+)?", t)
    rest = re.sub(r"[\s=＝?？.,:;!]", "", t)
    if len(nums) != 2 or len("".join(nums)) < 0.6 * len(rest):
        return None
    a, b = Decimal(nums[0]), Decimal(nums[1])
    ans = f"ไม่แน่ใจเครื่องหมาย\n+ → {_fmt(a + b)}\n− → {_fmt(a - b)}\n× → {_fmt(a * b)}"
    if b != 0:
        ans += f"\n÷ → {_fmt(a / b)}"
    return f"{nums[0]} ? {nums[1]}", ans, None


def solve_math(text):
    """โจทย์เลขล้วน เช่น '15 + 27 =?' หรือ '3/4 + 1/6 =?'
    คืน (นิพจน์สำหรับแสดง, คำตอบ, นิพจน์มาตรฐาน) หรือ None ถ้าไม่ใช่โจทย์เลขล้วน"""
    t = text.translate(_THDIG)
    if _THAI.search(t):
        return None
    t = re.sub(r"[×✕✖xX]", "*", t)
    t = re.sub(r"[−–—‒]", "-", t)
    t = t.translate(_FIX)
    t = re.sub(r"(?<=\d),(?=\d)", ".", t)
    found = _EXPR.findall(t)
    if not found:
        return _unsure_operator(t)
    best = max(found, key=len)
    rest = re.sub(r"[\s=＝?？.,:;!]", "", t)
    if len(re.sub(r"\s", "", best)) < 0.6 * len(rest):
        return None                      # มีข้อความอื่นปนเยอะ ไม่ใช่โจทย์เลขล้วน
    toks = [re.sub(r"\s+", "", x) for x in _TOK.findall(best)]
    toks = ["÷" if x in ("/", ":") else x for x in toks]
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


def sig(img, size=(48, 24)):
    return img.convert("L").resize(size).tobytes()


def sdiff(a, b):
    return sum(abs(p - q) for p, q in zip(a, b)) / len(a)


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

    def loop(self):
        prev = done = None
        stable = 0
        ex = ThreadPoolExecutor(2)
        with mss.mss() as sct:
            while self.running:
                time.sleep(0.05)
                if not self.cfg.get("q"):
                    continue
                try:
                    sg = sig(grab_raw(sct, self.cfg["q"]))
                    stable = stable + 1 if prev is not None and sdiff(sg, prev) < 4 else 0
                    prev = sg
                    if stable < 2:                      # รอภาพนิ่งประมาณ 0.2 วินาที
                        continue
                    if done is not None and sdiff(sg, done) < 4:
                        continue                        # ภาพเดิมที่อ่านไปแล้ว
                    done = sg
                    img = prep(grab_raw(sct, self.cfg["q"]))
                    hold = {}
                    f_fast = ex.submit(ocr_lines, img, "eng")            # เร็ว: เหมาะกับโจทย์เลข
                    f_full = ex.submit(ocr_lines, img, "tha+eng", hold)   # เต็ม: โจทย์ข้อความภาษาไทย
                    raw_fast = " ".join(f_fast.result())
                    if solve_math(raw_fast):
                        cancel(hold)                                      # เป็นโจทย์เลขแล้ว ไม่ต้องรออ่านไทย
                        raw = raw_fast
                    else:
                        raw = " ".join(f_full.result())

                    # 1) โจทย์เลขล้วน -> คำนวณเอง
                    m = solve_math(raw)
                    if m:
                        pretty, ans, canon = m
                        db = self.mathdb.get(canon)
                        if db and db != ans:
                            self.show(f"ไม่แน่ใจ: {ans} หรือ {db}", f"อ่านโจทย์ได้: {pretty} = ?  (คำนวณกับฐานข้อมูลไม่ตรงกัน)")
                        else:
                            self.show(ans, f"อ่านโจทย์ได้: {pretty} = ?" + ("  ✔ ตรงฐานข้อมูล" if db else ""))
                        continue

                    # 2) โจทย์ข้อความ -> หาในฐานข้อมูล
                    qtxt = norm(raw)
                    if len(qtxt) < 6:
                        continue
                    ms = process.extract(qtxt, self.nq, scorer=fuzz.ratio, limit=2)
                    if not ms or ms[0][1] < 60:
                        self.show("ไม่พบโจทย์นี้ในฐานข้อมูล", f"OCR: {qtxt[:60]}")
                        continue
                    picks = [ms[0]]
                    if len(ms) > 1 and ms[0][1] - ms[1][1] < 4 and ms[1][1] >= 60:
                        picks.append(ms[1])
                    answers = [self.qa[self.keys[x[2]]] for x in picks]
                    note = " / ".join(f"{self.keys[x[2]]} ({int(x[1])}%)" for x in picks)
                    self.show(answers[0] if len(answers) == 1 else "ไม่แน่ใจ:\n" + "\n".join(answers), note)
                except Exception as e:
                    self.show("OCR error", str(e)[:400])

    def show(self, a, i):
        self.root.after(0, lambda: (self.ans.config(text=a), self.info.config(text=i)))


if __name__ == "__main__":
    App()
