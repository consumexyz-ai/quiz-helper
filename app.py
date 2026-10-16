import sys, os, json, re, time, threading, ctypes, subprocess, io
import tkinter as tk
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
TRANSPARENT = "#010101"


def norm(s):
    return re.sub(r"[\s\W_]+", "", s).lower()


# ------------------------------------------------------------------ OCR
def _tess(img, extra=()):
    if not os.path.exists(TESS_EXE):
        raise RuntimeError(f"ไม่พบ {TESS_EXE} (โฟลเดอร์ tesseract ต้องอยู่ข้างไฟล์ exe)")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    env = dict(os.environ, TESSDATA_PREFIX=os.path.join(TESS_DIR, "tessdata"), OMP_THREAD_LIMIT="1")
    p = subprocess.run([TESS_EXE, "stdin", "stdout", "-l", "tha+eng", "--psm", "6", *extra],
                       input=buf.getvalue(), capture_output=True, env=env, creationflags=0x08000000)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "ignore")[:300])
    return p.stdout.decode("utf-8", "ignore")


def ocr_lines(img):
    return [l.strip() for l in _tess(img).splitlines() if l.strip()]


def ocr_options(img, n=4):
    """OCR แบบมีพิกัด แล้วแบ่งภาพเป็น n แถบแนวตั้ง คืนข้อความของแต่ละแถบ"""
    bands = [[] for _ in range(n)]
    for row in _tess(img, ("tsv",)).splitlines()[1:]:
        c = row.split("\t")
        if len(c) < 12 or c[0] != "5":
            continue
        text = c[11].strip()
        if not text or float(c[10]) < 0:
            continue
        cy = int(c[7]) + int(c[9]) / 2
        b = min(n - 1, max(0, int(cy / img.height * n)))
        bands[b].append((int(c[6]), text))
    return ["".join(t for _, t in sorted(b)) for b in bands]


def grab_raw(sct, region):
    s = sct.grab(region)
    return Image.frombytes("RGB", s.size, s.bgra, "raw", "BGRX")


def prep(img):
    k = 3 if img.width < 450 else 2   # ภาพเล็กขยายมากขึ้นให้ OCR อ่านง่าย
    img = img.resize((img.width * k, img.height * k), Image.LANCZOS)
    return ImageOps.autocontrast(img.convert("L"))


def sig(img, size=(48, 24)):
    return img.convert("L").resize(size).tobytes()


def sdiff(a, b):
    return sum(abs(p - q) for p, q in zip(a, b)) / len(a)


# ------------------------------------------------------------------ UI helpers
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


class Highlight:
    """หน้าต่างโปร่งใสวางทับช่องตัวเลือก วาดกรอบรอบข้อที่ถูก คลิกทะลุได้ ไม่แย่งโฟกัสเกม"""

    def __init__(self, root):
        self.win = tk.Toplevel(root)
        w = self.win
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.attributes("-transparentcolor", TRANSPARENT)
        w.configure(bg=TRANSPARENT)
        self.cv = tk.Canvas(w, bg=TRANSPARENT, highlightthickness=0)
        self.cv.pack(fill="both", expand=True)
        self.region = None

    def place(self, region):
        self.region = region
        self.win.geometry(f"{region['width']}x{region['height']}+{region['left']}+{region['top']}")
        self.win.update_idletasks()
        try:
            u = ctypes.windll.user32
            hwnd = u.GetParent(self.win.winfo_id()) or self.win.winfo_id()
            ex = u.GetWindowLongW(hwnd, -20)
            # LAYERED | TRANSPARENT (คลิกทะลุ) | TOOLWINDOW | NOACTIVATE
            u.SetWindowLongW(hwnd, -20, ex | 0x80000 | 0x20 | 0x80 | 0x08000000)
        except Exception:
            pass

    def clear(self):
        self.cv.delete("all")

    def show(self, items, n=4):
        self.cv.delete("all")
        if not self.region:
            return
        w, h = self.region["width"], self.region["height"]
        for band, color in items:
            y1, y2 = band * h / n + 2, (band + 1) * h / n - 2
            self.cv.create_rectangle(3, y1, w - 3, y2, outline=color, width=4)


# ------------------------------------------------------------------ app
class App:
    def __init__(self):
        self.root = r = tk.Tk()
        r.title("ROX Quiz Helper")
        r.attributes("-topmost", True)
        r.geometry("380x190+40+40")
        r.configure(bg="#1b1b1b")
        self.ans = tk.Label(r, text="", font=("Tahoma", 15, "bold"), bg="#1b1b1b",
                            fg="#7CFC00", wraplength=360, justify="left")
        self.ans.pack(fill="both", expand=True, padx=8, pady=(8, 0))
        self.info = tk.Label(r, text="", font=("Tahoma", 9), bg="#1b1b1b", fg="#aaa",
                             wraplength=360, justify="left")
        self.info.pack(fill="x", padx=8)
        tk.Button(r, text="ตั้งพื้นที่", command=self.setup).pack(pady=6)

        self.qa = json.load(open(QA_FILE, encoding="utf-8"))
        self.keys = list(self.qa)
        self.nq = [norm(k) for k in self.keys]
        self.cfg = json.load(open(CFG_FILE)) if os.path.exists(CFG_FILE) else {}
        self.hl = Highlight(r)
        if self.cfg.get("o"):
            self.hl.place(self.cfg["o"])
        self.ans.config(text="รอโจทย์..." if self.cfg.get("q") else "กด 'ตั้งพื้นที่' ก่อนครับ")
        self.info.config(text=f"ฐานข้อมูล {len(self.qa)} ข้อ")
        self.running = True
        threading.Thread(target=self.loop, daemon=True).start()
        r.protocol("WM_DELETE_WINDOW", self.close)
        r.mainloop()

    def close(self):
        self.running = False
        self.root.destroy()

    def ui(self, fn):
        self.root.after(0, fn)

    def setup(self):
        self.root.withdraw()
        self.hl.clear()
        time.sleep(0.3)
        q = pick_region(self.root, "1/2 ลากคลุม 'ช่องโจทย์'")
        o = pick_region(self.root, "2/2 ลากคลุมตัวเลือก 4 ข้อ (ขอบบนปุ่มแรก ถึง ขอบล่างปุ่มสุดท้าย)") if q else None
        self.root.deiconify()
        if q and o:
            self.cfg = {"q": q, "o": o}
            json.dump(self.cfg, open(CFG_FILE, "w"))
            self.hl.place(o)
            self.ans.config(text="รอโจทย์...")

    def loop(self):
        ex = ThreadPoolExecutor(2)
        prev = done = None
        stable = retries = 0
        with mss.mss() as sct:
            while self.running:
                time.sleep(0.1)
                q, o = self.cfg.get("q"), self.cfg.get("o")
                if not q:
                    continue
                try:
                    sg = sig(grab_raw(sct, q))
                    if prev is None or sdiff(sg, prev) >= 4:
                        stable = 0
                        if done is None or sdiff(sg, done) >= 4:
                            self.ui(self.hl.clear)     # โจทย์เปลี่ยน: ลบกรอบเก่า
                    else:
                        stable += 1
                    prev = sg
                    if stable < 2:                      # รอภาพนิ่งประมาณ 0.2 วินาที
                        continue
                    if done is not None and sdiff(sg, done) < 4:
                        continue                        # ภาพเดิมที่อ่านไปแล้ว
                    done = sg
                    self.ui(self.hl.clear)
                    time.sleep(0.05)
                    qi = grab_raw(sct, q)
                    fq = ex.submit(ocr_lines, prep(qi))
                    fo = ex.submit(ocr_options, prep(grab_raw(sct, o))) if o else None
                    qtxt = norm("".join(fq.result()))
                    opts = fo.result() if fo else []
                    if len(qtxt) < 6:
                        continue
                    ms = process.extract(qtxt, self.nq, scorer=fuzz.ratio, limit=2)
                    if not ms or ms[0][1] < 60:
                        self.show("ไม่พบโจทย์นี้ในฐานข้อมูล", f"OCR: {qtxt[:60]}")
                        continue
                    picks = [ms[0]]
                    if len(ms) > 1 and ms[0][1] - ms[1][1] < 4 and ms[1][1] >= 60:
                        picks.append(ms[1])
                    answers = [self.qa[self.keys[m[2]]] for m in picks]

                    items = []
                    if any(opts):
                        nopts = [norm(x) for x in opts]
                        for k, m in enumerate(picks):
                            na = norm(answers[k])
                            scorer = fuzz.partial_ratio if len(na) >= 5 else fuzz.ratio
                            sc = [scorer(na, x) if x else 0 for x in nopts]
                            order = sorted(range(len(sc)), key=lambda i: -sc[i])
                            if sc[order[0]] >= 60:
                                items.append((order[0], "#00ff00" if k == 0 else "#ffd400"))
                                if k == 0 and sc[order[0]] - sc[order[1]] < 8:
                                    items.append((order[1], "#ffd400"))
                    if o and not items and retries < 3:   # ตัวเลือกอาจยังไม่ขึ้น ลองอ่านซ้ำ
                        retries += 1
                        done = None
                        time.sleep(0.4)
                    else:
                        retries = 0

                    text = answers[0] if len(answers) == 1 else "ไม่แน่ใจ:\n" + "\n".join(answers)
                    note = " / ".join(f"{self.keys[m[2]]} ({int(m[1])}%)" for m in picks)
                    if o:
                        note += "\nOCR ตัวเลือก: " + " | ".join(x[:14] for x in opts)
                    self.show(text, note)
                    self.ui(lambda it=items: self.hl.show(it))
                except Exception as e:
                    self.show("OCR error", str(e)[:400])

    def show(self, a, i):
        self.ui(lambda: (self.ans.config(text=a), self.info.config(text=i)))


if __name__ == "__main__":
    App()
