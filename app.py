import sys, os, json, re, time, threading, ctypes
import tkinter as tk

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # ให้พิกัดตรงกับจอจริง
except Exception:
    pass

import mss
from PIL import Image
from rapidfuzz import process, fuzz

BASE = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
QA_FILE = os.path.join(BASE, "qa.json")
CFG_FILE = os.path.join(BASE, "config.json")


def norm(s):
    return re.sub(r"[\s\W_]+", "", s).lower()


_LANG = [None]


def available_langs():
    try:
        from winrt.windows.media.ocr import OcrEngine
        return [l.language_tag for l in OcrEngine.available_recognizer_languages]
    except Exception as e:
        return [f"ไม่สามารถอ่านรายการภาษา: {e!r}"]


def ocr_lines(img):
    import winocr
    order = [_LANG[0]] if _LANG[0] else []
    order += [l for l in ("th-TH", "th", "en-US") if l not in order]
    last = None
    for l in order:
        try:
            res = winocr.recognize_pil_sync(img, l)
            _LANG[0] = l
            return [x["text"] for x in res.get("lines", [])]
        except Exception as e:
            last = e
    raise RuntimeError(f"{type(last).__name__}: {last} | ภาษาที่ Windows มี: {available_langs()}")


def grab(sct, region):
    s = sct.grab(region)
    img = Image.frombytes("RGB", s.size, s.bgra, "raw", "BGRX")
    return img.resize((img.width * 2, img.height * 2), Image.LANCZOS)


def pick_region(root, title):
    sel, st = {}, {}
    top = tk.Toplevel(root)
    top.attributes("-fullscreen", True, "-alpha", 0.3, "-topmost", True)
    top.configure(bg="black", cursor="cross")
    cv = tk.Canvas(top, bg="black", highlightthickness=0)
    cv.pack(fill="both", expand=True)
    cv.create_text(top.winfo_screenwidth() // 2, 60, fill="white", font=("Tahoma", 24),
                   text=title + "  (ลากเมาส์คลุมพื้นที่ / Esc ยกเลิก)")

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
        self.ans.config(text="รอโจทย์..." if self.cfg else "กด 'ตั้งพื้นที่' ก่อนครับ")
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
        q = pick_region(self.root, "1/2 ลากคลุม 'ช่องโจทย์'")
        o = pick_region(self.root, "2/2 ลากคลุม 'ตัวเลือกคำตอบทั้ง 4 ข้อ'") if q else None
        self.root.deiconify()
        if q and o:
            self.cfg = {"q": q, "o": o}
            json.dump(self.cfg, open(CFG_FILE, "w"))
            self.ans.config(text="รอโจทย์...")

    def loop(self):
        last = ""
        with mss.mss() as sct:
            while self.running:
                time.sleep(0.8)
                if not self.cfg:
                    continue
                try:
                    qtxt = norm("".join(ocr_lines(grab(sct, self.cfg["q"]))))
                    if len(qtxt) < 6 or qtxt == last:
                        continue
                    last = qtxt
                    ms = process.extract(qtxt, self.nq, scorer=fuzz.ratio, limit=2)
                    if not ms or ms[0][1] < 70:
                        self.show("ไม่พบโจทย์นี้ในฐานข้อมูล", f"OCR: {qtxt[:60]}")
                        continue
                    # ถ้าสองข้อคะแนนใกล้กันมาก (เช่น MVP คนละแผนที่) แสดงทั้งสอง
                    picks = [ms[0]]
                    if len(ms) > 1 and ms[0][1] - ms[1][1] < 4 and ms[1][1] >= 70:
                        picks.append(ms[1])
                    opts = ocr_lines(grab(sct, self.cfg["o"]))
                    out = []
                    for m in picks:
                        ans = self.qa[self.keys[m[2]]]
                        pos = ""
                        if opts:
                            na = norm(ans)
                            sc = [(fuzz.ratio if len(na) <= 8 else fuzz.partial_ratio)(na, norm(o))
                                  for o in opts]
                            b = max(range(len(sc)), key=sc.__getitem__)
                            if sc[b] >= 60:
                                pos = f"  → ข้อ {b + 1}"
                        out.append(ans + pos)
                    note = " / ".join(f"{self.keys[m[2]]} ({int(m[1])}%)" for m in picks)
                    self.show("\n".join(out) if len(out) == 1 else "ไม่แน่ใจ:\n" + "\n".join(out), note)
                except Exception as e:
                    self.show("OCR error", str(e)[:400])

    def show(self, a, i):
        self.root.after(0, lambda: (self.ans.config(text=a), self.info.config(text=i)))


if __name__ == "__main__":
    App()
