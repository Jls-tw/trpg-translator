#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TRPG 規則書翻譯工具：圖形介面 (Tkinter，Python 內建，不需額外安裝 GUI 套件)

使用方式：
    1. 把本檔與 trpg_translate_v2.py 放在同一個資料夾
    2. 執行：python trpg_translate_gui.py
    3. 「新增 PDF」→「管理 Key」貼上 API Key → 「開始翻譯」

翻譯引擎沿用 trpg_translate_v2.py，因此命令列版與圖形版共用同一份進度
（每頁一個快取檔），兩邊可以交替使用、隨時中斷續傳。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Dict, List, Optional

# 打包成 exe 後，設定檔與 .env 放在 exe 旁邊（而不是暫存解壓目錄）
if getattr(sys, "frozen", False):
    if sys.platform == "darwin":
        # .app 套件內不可寫入（寫入會破壞簽章），設定與 .env 改放使用者資料夾
        APP_DIR = Path.home() / "Library" / "Application Support" / "TRPG_Translator"
        APP_DIR.mkdir(parents=True, exist_ok=True)
    else:
        APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

try:
    import trpg_translate_v2 as engine
except Exception as _e:  # 缺檔或缺套件時給出看得懂的訊息
    _r = tk.Tk()
    _r.withdraw()
    messagebox.showerror(
        "無法載入翻譯引擎",
        f"{type(_e).__name__}: {_e}\n\n"
        "請確認 trpg_translate_v2.py 與本檔在同一個資料夾，並已安裝：\n"
        "pip install google-genai pypdf pydantic python-dotenv",
    )
    sys.exit(1)

SETTINGS_FILE = APP_DIR / "gui_settings.json"
ENV_FILE = APP_DIR / ".env"
try:
    from dotenv import load_dotenv

    load_dotenv(ENV_FILE)  # 明確讀取 APP_DIR 內的 .env
except ImportError:
    pass
STATUS_TEXT = {"complete": "完成", "partial": "部分完成", "stopped": "已停止", "error": "錯誤"}
THINKING_CHOICES = ["none", "minimal", "low", "medium", "high"]


# ============================================================
# 小工具
# ============================================================
def parse_keys(text: str) -> List[str]:
    seen, keys = set(), []
    for k in re.split(r"[,\s;]+", (text or "").strip()):
        if k and k not in seen:
            seen.add(k)
            keys.append(k)
    return keys


def save_keys_to_env(keys: List[str]) -> None:
    """把 GEMINI_API_KEYS 寫進 .env（保留其他設定行）。"""
    line = f'GEMINI_API_KEYS="{",".join(keys)}"'
    lines: List[str] = []
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    for i, ln in enumerate(lines):
        if ln.strip().startswith("GEMINI_API_KEYS"):
            lines[i] = line
            break
    else:
        lines.append(line)
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def open_path(path: Path) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as e:
        messagebox.showerror("無法開啟", f"{path}\n\n{e}")


def fmt_dur(sec: float) -> str:
    sec = int(max(sec, 0))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h} 小時 {m} 分" if h else (f"{m} 分 {s} 秒" if m else f"{s} 秒")


def pdf_page_count(path: Path) -> Optional[int]:
    try:
        r = engine.PdfReader(str(path))
        if r.is_encrypted:
            try:
                r.decrypt("")
            except Exception:
                return None
        return len(r.pages)
    except Exception:
        return None


def count_pages(pages_dir: Path) -> int:
    try:
        return sum(1 for p in pages_dir.glob("*.json") if p.stem.isdigit())
    except OSError:
        return 0


def count_failed(failed_path: Path) -> int:
    try:
        return len(engine.load_json(failed_path, {}).get("pages", {}))
    except Exception:
        return 0


class QueueLogHandler(logging.Handler):
    """把引擎的 log 轉送到 GUI 的佇列（執行緒安全）。"""

    def __init__(self, q: "queue.Queue"):
        super().__init__(logging.INFO)
        self.q = q
        self.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.q.put(("log", record.levelno, self.format(record)))
        except Exception:
            pass


# ============================================================
# 主視窗
# ============================================================
class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("TRPG 規則書翻譯工具")
        root.geometry("1020x800")
        root.minsize(920, 700)

        self.q: "queue.Queue" = queue.Queue()
        self.books: Dict[str, dict] = {}
        self.running = False
        self.cur: Optional[dict] = None
        self.file_handler: Optional[logging.Handler] = None
        self.keys: List[str] = parse_keys(os.environ.get("GEMINI_API_KEYS", ""))

        self._setup_style()
        self._init_vars()
        self._build_ui()
        self._setup_logging()
        self.load_settings()
        self._update_key_label()

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.poll()
        self.refresh_stats()

    # ---------- 樣式 ----------
    def _setup_style(self) -> None:
        family = "Microsoft JhengHei UI" if sys.platform == "win32" else None
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont"):
            try:
                f = tkfont.nametofont(name)
                if family:
                    f.configure(family=family)
                f.configure(size=10)
            except tk.TclError:
                pass
        ui_family = family or ("PingFang TC" if sys.platform == "darwin" else "TkDefaultFont")
        self.log_font = (ui_family, 9)
        style = ttk.Style()
        style.configure("Treeview", rowheight=26)
        style.configure("Start.TButton", font=(ui_family, 11, "bold"), padding=(14, 6))
        style.configure("Hint.TLabel", foreground="#666666")

    # ---------- 變數 ----------
    def _init_vars(self) -> None:
        self.model_var = tk.StringVar(value=os.environ.get("GEMINI_MODEL_ID", engine.Config.model))
        self.thinking_var = tk.StringVar(value=engine.Config.thinking)
        self.timeout_var = tk.StringVar(value=str(engine.Config.timeout))
        self.interval_var = tk.StringVar(value=str(int(engine.Config.min_interval)))
        self.maxfails_var = tk.StringVar(value=str(engine.Config.max_fails))
        self.consec_var = tk.StringVar(value=str(engine.Config.max_consecutive_fails))
        self.startkey_var = tk.StringVar(value="1")
        self.transport_var = tk.StringVar(value=engine.Config.transport)
        self.prev_var = tk.BooleanVar(value=False)
        self.join_var = tk.BooleanVar(value=True)
        self.markers_var = tk.BooleanVar(value=False)
        self.move_var = tk.BooleanVar(value=False)
        self.start_var = tk.StringVar(value="1")
        self.end_var = tk.StringVar(value="0")
        self.redo_var = tk.StringVar(value="")
        self.out_var = tk.StringVar(value="")
        self.key_var = tk.StringVar(value="")
        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress_text = tk.StringVar(value="尚未開始")
        self.status_var = tk.StringVar(value="就緒")

    # ---------- 介面 ----------
    def _build_ui(self) -> None:
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=2)
        root.rowconfigure(4, weight=3)

        # --- ① PDF 清單 ---
        box = ttk.LabelFrame(root, text=" ① 要翻譯的 PDF ", padding=8)
        box.grid(row=0, column=0, sticky="nsew", padx=10, pady=(10, 4))
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)

        cols = ("name", "total", "done", "failed", "status")
        self.tree = ttk.Treeview(box, columns=cols, show="headings", selectmode="extended", height=5)
        for c, text, w, anchor in (
            ("name", "檔名", 380, "w"), ("total", "總頁數", 80, "center"),
            ("done", "已完成", 100, "center"), ("failed", "失敗頁", 80, "center"),
            ("status", "狀態", 110, "center"),
        ):
            self.tree.heading(c, text=text)
            self.tree.column(c, width=w, anchor=anchor, stretch=(c == "name"))
        sb = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<Double-1>", lambda e: self.on_open_result())
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._update_progress())

        btns = ttk.Frame(box)
        btns.grid(row=0, column=2, sticky="ns", padx=(10, 0))
        self.list_buttons = [
            ttk.Button(btns, text="新增 PDF…", command=self.on_add_files),
            ttk.Button(btns, text="新增資料夾…", command=self.on_add_folder),
            ttk.Button(btns, text="移除選取", command=self.on_remove),
        ]
        for b in self.list_buttons:
            b.pack(fill="x", pady=2)
        ttk.Button(btns, text="開啟譯文 / 資料夾", command=self.on_open_result).pack(fill="x", pady=(14, 2))
        ttk.Label(box, text="未選取任何項目時，會處理清單中的全部 PDF；雙擊可開啟譯文。",
                  style="Hint.TLabel").grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # --- ②③④ 設定 ---
        mid = ttk.Frame(root)
        mid.grid(row=1, column=0, sticky="ew", padx=10, pady=4)
        mid.columnconfigure(0, weight=3)
        mid.columnconfigure(1, weight=2)

        sf = ttk.LabelFrame(mid, text=" ② 翻譯設定 ", padding=8)
        sf.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        sf.columnconfigure(1, weight=1)
        sf.columnconfigure(3, weight=1)
        self.settings_widgets: List[tk.Widget] = []

        def add_row(r, c, label, widget):
            ttk.Label(sf, text=label).grid(row=r, column=c, sticky="e", padx=(0, 6), pady=3)
            widget.grid(row=r, column=c + 1, sticky="ew", padx=(0, 12), pady=3)
            self.settings_widgets.append(widget)

        add_row(0, 0, "模型", ttk.Combobox(sf, textvariable=self.model_var, width=24, values=[
            engine.Config.model, "gemini-2.5-flash", "gemini-2.5-flash-lite"]))
        add_row(0, 2, "思考強度", ttk.Combobox(sf, textvariable=self.thinking_var, width=10,
                                              values=THINKING_CHOICES, state="readonly"))
        add_row(1, 0, "單次逾時（秒）", ttk.Spinbox(sf, from_=10, to=900, increment=10, textvariable=self.timeout_var, width=8))
        add_row(1, 2, "請求間隔（秒）", ttk.Spinbox(sf, from_=0, to=120, increment=1, textvariable=self.interval_var, width=8))
        add_row(2, 0, "單頁重試上限", ttk.Spinbox(sf, from_=1, to=20, textvariable=self.maxfails_var, width=8))
        add_row(2, 2, "連續失敗中止", ttk.Spinbox(sf, from_=1, to=50, textvariable=self.consec_var, width=8))
        add_row(3, 0, "起始 Key 編號", ttk.Spinbox(sf, from_=1, to=99, textvariable=self.startkey_var, width=8))
        add_row(3, 2, "PDF 傳送方式", ttk.Combobox(sf, textvariable=self.transport_var, width=10,
                                                  values=["files", "inline"], state="readonly"))
        for i, (var, text) in enumerate((
            (self.prev_var, "附上上一頁 PDF 當上下文（品質較好，成本約 2 倍）"),
            (self.join_var, "合併時接回被分頁切斷的段落"),
            (self.markers_var, "合併檔加入頁碼標記 <!-- p.N -->（會停用段落接合）"),
            (self.move_var, "全部完成後，把原始 PDF 移進輸出資料夾"),
        )):
            cb = ttk.Checkbutton(sf, text=text, variable=var)
            cb.grid(row=4 + i, column=0, columnspan=4, sticky="w", pady=1)
            self.settings_widgets.append(cb)

        right = ttk.Frame(mid)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)

        rf = ttk.LabelFrame(right, text=" ③ 頁面範圍（僅限單一 PDF）", padding=8)
        rf.grid(row=0, column=0, sticky="ew")
        rf.columnconfigure(1, weight=1)
        self.range_widgets: List[tk.Widget] = []
        for r, (label, var) in enumerate((("起始頁", self.start_var),
                                          ("結束頁（0 = 到最後）", self.end_var),
                                          ("強制重翻（例：5,8-10）", self.redo_var))):
            ttk.Label(rf, text=label).grid(row=r, column=0, sticky="w", pady=3)
            e = ttk.Entry(rf, textvariable=var, width=14)
            e.grid(row=r, column=1, sticky="ew", padx=(8, 0), pady=3)
            self.range_widgets.append(e)

        of = ttk.LabelFrame(right, text=" ④ 輸出資料夾 ", padding=8)
        of.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        of.columnconfigure(0, weight=1)
        self.out_entry = ttk.Entry(of, textvariable=self.out_var)
        self.out_entry.grid(row=0, column=0, sticky="ew")
        self.out_browse = ttk.Button(of, text="瀏覽…", width=7, command=self.on_browse_out)
        self.out_browse.grid(row=0, column=1, padx=(6, 0))
        ttk.Label(of, text="譯文與進度會存在此資料夾下的「書名」子資料夾。",
                  style="Hint.TLabel").grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # --- 操作列 ---
        bar = ttk.Frame(root)
        bar.grid(row=2, column=0, sticky="ew", padx=10, pady=6)
        self.start_btn = ttk.Button(bar, text="▶ 開始翻譯", style="Start.TButton", command=self.on_start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(bar, text="■ 停止", command=self.on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        self.merge_btn = ttk.Button(bar, text="重組 Markdown", command=self.on_merge)
        self.merge_btn.pack(side="left")
        ttk.Button(bar, text="管理 Key…", command=self.on_manage_keys).pack(side="right")
        ttk.Label(bar, textvariable=self.key_var).pack(side="right", padx=10)

        # --- 進度 ---
        pf = ttk.Frame(root)
        pf.grid(row=3, column=0, sticky="ew", padx=10)
        pf.columnconfigure(0, weight=1)
        ttk.Progressbar(pf, variable=self.progress_var, maximum=100).grid(row=0, column=0, sticky="ew")
        ttk.Label(pf, textvariable=self.progress_text).grid(row=1, column=0, sticky="w", pady=(4, 0))

        # --- 記錄 ---
        lf = ttk.LabelFrame(root, text=" 執行記錄 ", padding=4)
        lf.grid(row=4, column=0, sticky="nsew", padx=10, pady=(8, 4))
        lf.columnconfigure(0, weight=1)
        lf.rowconfigure(0, weight=1)
        self.log_text = ScrolledText(lf, height=10, state="disabled", wrap="word", font=self.log_font)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        self.log_text.tag_configure("warn", foreground="#b45309")
        self.log_text.tag_configure("error", foreground="#b91c1c")

        ttk.Label(root, textvariable=self.status_var, relief="sunken", anchor="w", padding=(6, 2)).grid(
            row=5, column=0, sticky="ew")

    def _setup_logging(self) -> None:
        engine.log.setLevel(logging.DEBUG)
        engine.log.addHandler(QueueLogHandler(self.q))

    # ---------- 設定存取 ----------
    def load_settings(self) -> None:
        data = engine.load_json(SETTINGS_FILE, {})
        pairs = [("model", self.model_var), ("thinking", self.thinking_var), ("timeout", self.timeout_var),
                 ("interval", self.interval_var), ("max_fails", self.maxfails_var), ("consec", self.consec_var),
                 ("start_key", self.startkey_var), ("transport", self.transport_var), ("out", self.out_var)]
        for k, var in pairs:
            if k not in data:
                continue
            if k == "timeout" and str(data[k]).isdigit() and int(data[k]) < 300:
                continue  # 舊版預設 90 秒會把「慢但會成功」的請求砍成 504，載入時忽略
            var.set(str(data[k]))
        for k, var in (("prev", self.prev_var), ("join", self.join_var),
                       ("markers", self.markers_var), ("move", self.move_var)):
            if k in data:
                var.set(bool(data[k]))
        for p in data.get("pdfs", []):
            if Path(p).is_file():
                self.add_pdf(Path(p), quiet=True)

    def save_settings(self) -> None:
        data = {
            "model": self.model_var.get(), "thinking": self.thinking_var.get(),
            "timeout": self.timeout_var.get(), "interval": self.interval_var.get(),
            "max_fails": self.maxfails_var.get(), "consec": self.consec_var.get(),
            "start_key": self.startkey_var.get(), "transport": self.transport_var.get(), "out": self.out_var.get(),
            "prev": self.prev_var.get(), "join": self.join_var.get(),
            "markers": self.markers_var.get(), "move": self.move_var.get(),
            "pdfs": [str(b["path"]) for b in self.books.values()],
        }
        try:
            engine.save_json(SETTINGS_FILE, data)
        except Exception:
            pass

    # ---------- PDF 清單 ----------
    def out_root(self, pdf: Path) -> Path:
        v = self.out_var.get().strip()
        return Path(v) if v else pdf.parent

    def add_pdf(self, path: Path, quiet: bool = False) -> None:
        path = path.resolve()
        if any(b["path"] == path for b in self.books.values()):
            return
        total = pdf_page_count(path)
        if total is None and not quiet:
            messagebox.showwarning("讀取失敗", f"無法讀取頁數（可能已加密或損毀）：\n{path.name}\n仍會加入清單，翻譯時再嘗試解鎖。")
        if not self.out_var.get().strip():
            self.out_var.set(str(path.parent))
        iid = self.tree.insert("", "end", values=(path.name, total or "?", "-", "-", "待處理"))
        self.books[iid] = {"path": path, "name": path.stem, "total": total, "done": 0, "failed": 0, "status": "待處理"}

    def on_add_files(self) -> None:
        files = filedialog.askopenfilenames(title="選擇 PDF", filetypes=[("PDF 檔案", "*.pdf"), ("所有檔案", "*.*")])
        for f in files:
            self.add_pdf(Path(f))
        self.refresh_stats(once=True)

    def on_add_folder(self) -> None:
        d = filedialog.askdirectory(title="選擇含有 PDF 的資料夾")
        if not d:
            return
        pdfs = sorted(Path(d).glob("*.pdf"))
        if not pdfs:
            messagebox.showinfo("沒有找到 PDF", "這個資料夾裡沒有 .pdf 檔案。")
            return
        for p in pdfs:
            self.add_pdf(p)
        self.refresh_stats(once=True)

    def on_remove(self) -> None:
        for iid in self.tree.selection():
            self.tree.delete(iid)
            self.books.pop(iid, None)

    def on_browse_out(self) -> None:
        d = filedialog.askdirectory(title="選擇輸出資料夾")
        if d:
            self.out_var.set(d)

    def selected_or_all(self) -> List[str]:
        sel = [i for i in self.tree.selection() if i in self.books]
        return sel or list(self.books.keys())

    def on_open_result(self) -> None:
        sel = [i for i in self.tree.selection() if i in self.books]
        if not sel:
            if self.out_var.get().strip() and Path(self.out_var.get()).is_dir():
                open_path(Path(self.out_var.get()))
            return
        b = self.books[sel[0]]
        folder = self.out_root(b["path"]) / b["name"]
        md = folder / f"{b['name']}_翻譯.md"
        open_path(md if md.exists() else folder if folder.exists() else folder.parent)

    # ---------- Key 管理 ----------
    def on_manage_keys(self) -> None:
        if self.running:
            messagebox.showinfo("進行中", "翻譯進行中，請先停止再修改 Key。")
            return
        win = tk.Toplevel(self.root)
        win.title("管理 API Key")
        win.transient(self.root)
        win.grab_set()
        ttk.Label(win, text="每行一把 Key（也可用逗號分隔）。多把 Key 會在額度用完時自動輪替。",
                  padding=10).pack(anchor="w")
        txt = tk.Text(win, width=64, height=12, font=self.log_font)
        txt.pack(padx=10, fill="both", expand=True)
        txt.insert("1.0", "\n".join(self.keys))
        save_env = tk.BooleanVar(value=True)
        ttk.Checkbutton(win, text=f"同時儲存到 {ENV_FILE.name}（下次啟動自動載入；金鑰只存在本機）",
                        variable=save_env).pack(anchor="w", padx=10, pady=6)
        row = ttk.Frame(win)
        row.pack(fill="x", padx=10, pady=(0, 10))

        def ok():
            self.keys = parse_keys(txt.get("1.0", "end"))
            os.environ["GEMINI_API_KEYS"] = ",".join(self.keys)
            if save_env.get():
                try:
                    save_keys_to_env(self.keys)
                except Exception as e:
                    messagebox.showerror("儲存失敗", str(e), parent=win)
                    return
            self._update_key_label()
            win.destroy()

        ttk.Button(row, text="確定", command=ok).pack(side="right")
        ttk.Button(row, text="取消", command=win.destroy).pack(side="right", padx=6)

    def _update_key_label(self) -> None:
        n = len(self.keys)
        if n == 0:
            self.key_var.set("API Key：尚未設定")
            return
        out = self.out_var.get().strip() or "."
        raw = engine.load_json(Path(out) / engine.KEY_STATUS_FILE, {})
        now, ready, soon = time.time(), 0, None
        for k in self.keys:
            kid = hashlib.sha256(k.encode()).hexdigest()[:8]
            try:
                until = float(raw.get(kid, 0))
            except (TypeError, ValueError):
                until = 0.0
            if until <= now:
                ready += 1
            else:
                soon = until if soon is None else min(soon, until)
        text = f"API Key：可用 {ready} / 共 {n} 把"
        if ready == 0 and soon:
            text += f"（最快 {time.strftime('%H:%M', time.localtime(soon))} 恢復）"
        self.key_var.set(text)

    # ---------- 設定驗證 ----------
    @staticmethod
    def _num(var: tk.StringVar, label: str, lo: float, hi: float, cast=int):
        try:
            val = cast(float(var.get().strip()))
        except ValueError:
            raise ValueError(f"「{label}」需要填入數字")
        if not lo <= val <= hi:
            raise ValueError(f"「{label}」需介於 {lo} 到 {hi}")
        return val

    def build_config(self, n_targets: int) -> Optional["engine.Config"]:
        try:
            cfg = engine.Config(
                model=self.model_var.get().strip() or engine.Config.model,
                thinking=self.thinking_var.get().strip() or "none",
                start=self._num(self.start_var, "起始頁", 1, 100000),
                end=self._num(self.end_var, "結束頁", 0, 100000),
                start_key=self._num(self.startkey_var, "起始 Key 編號", 1, 999),
                max_fails=self._num(self.maxfails_var, "單頁重試上限", 1, 50),
                max_consecutive_fails=self._num(self.consec_var, "連續失敗中止", 1, 500),
                min_interval=self._num(self.interval_var, "請求間隔", 0, 600, float),
                timeout=self._num(self.timeout_var, "單次逾時", 10, 3600),
                transport=self.transport_var.get() or "files",
                with_prev_page=self.prev_var.get(),
                page_markers=self.markers_var.get(),
                join_pages=self.join_var.get(),
                move_pdf=self.move_var.get(),
            )
            redo_text = self.redo_var.get().strip()
            if redo_text:
                try:
                    cfg.redo = engine.parse_page_spec(redo_text)
                except ValueError:
                    raise ValueError("「強制重翻」格式不正確，範例：5,8-10")
        except ValueError as e:
            messagebox.showerror("設定有誤", str(e))
            return None
        if (cfg.start > 1 or cfg.end > 0 or cfg.redo) and n_targets != 1:
            messagebox.showerror("頁面範圍只能用於單一 PDF",
                                 f"目前會處理 {n_targets} 個 PDF。請在清單中只選取一個，或把頁面範圍改回預設值。")
            return None
        return cfg

    # ---------- 開始 / 停止 ----------
    def _set_running_ui(self, running: bool) -> None:
        self.running = running
        self.start_btn.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")
        self.merge_btn.configure(state="disabled" if running else "normal")
        for w in self.list_buttons + [self.out_browse, self.out_entry] + self.settings_widgets + self.range_widgets:
            try:
                w.state(["disabled"] if running else ["!disabled"])
            except Exception:
                pass

    def on_start(self) -> None:
        if self.running:
            return
        if not self.keys:
            messagebox.showwarning("尚未設定 API Key", "請先按「管理 Key…」貼上至少一把 Gemini API Key。")
            return
        targets = self.selected_or_all()
        if not targets:
            messagebox.showinfo("沒有 PDF", "請先按「新增 PDF…」選擇要翻譯的檔案。")
            return
        cfg = self.build_config(len(targets))
        if cfg is None:
            return
        out = Path(self.out_var.get().strip() or self.books[targets[0]]["path"].parent)
        try:
            out.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror("無法建立輸出資料夾", str(e))
            return
        self.out_var.set(str(out))
        cfg.output_root = str(out)
        self.save_settings()

        self.file_handler = logging.FileHandler(out / "translator.log", encoding="utf-8")
        self.file_handler.setLevel(logging.DEBUG)
        self.file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        engine.log.addHandler(self.file_handler)

        engine.STOP.clear()
        for iid in targets:
            self.books[iid]["status"] = "排隊中"
        self._set_running_ui(True)
        self.status_var.set("翻譯中…")
        jobs = [(iid, self.books[iid]["path"]) for iid in targets]
        threading.Thread(target=self._worker, args=(jobs, cfg, list(self.keys)), daemon=True).start()

    def on_stop(self) -> None:
        engine.STOP.set()
        self.stop_btn.configure(state="disabled")
        self.status_var.set("正在停止…將在目前這次請求結束後安全停止（最久約等於「單次逾時」秒數）")

    def _worker(self, jobs, cfg, keys) -> None:
        log = engine.log
        try:
            km = engine.KeyManager(keys, cfg.start_key - 1, cfg.timeout,
                                   str(Path(cfg.output_root) / engine.KEY_STATUS_FILE))
            log.info("模型 %s 啟動，共 %d 把 Key（從 %s 開始），%d 個 PDF", cfg.model, len(km.keys), km.label, len(jobs))
            for iid, path in jobs:
                if engine.STOP.is_set():
                    break
                self.q.put(("book_start", iid))
                job = engine.BookJob(path, cfg, km)
                try:
                    status = job.run()
                except engine.FatalError as e:
                    log.error("致命錯誤，中止：%s", e)
                    self.q.put(("book_done", iid, "error"))
                    self.q.put(("fatal", str(e)))
                    break
                if job.tokens_in or job.tokens_out:
                    log.info("[%s] 本次 token 用量：in=%d out=%d", job.name, job.tokens_in, job.tokens_out)
                self.q.put(("book_done", iid, status))
        except Exception as e:
            log.error("未預期的錯誤：%s: %s", type(e).__name__, e)
            log.debug("traceback", exc_info=True)
            self.q.put(("fatal", f"{type(e).__name__}: {e}"))
        finally:
            self.q.put(("finished",))

    # ---------- 重組 Markdown ----------
    def on_merge(self) -> None:
        targets = self.selected_or_all()
        if not targets:
            messagebox.showinfo("沒有 PDF", "請先新增 PDF。")
            return
        n_done = 0
        for iid in targets:
            b = self.books[iid]
            folder = self.out_root(b["path"]) / b["name"]
            if not (folder / "pages").is_dir():
                engine.log.warning("[%s] 還沒有任何翻譯快取，略過", b["name"])
                continue
            n = engine.merge_pages(folder / "pages", folder / "failed_pages.json",
                                   folder / f"{b['name']}_翻譯.md",
                                   join=self.join_var.get(), markers=self.markers_var.get())
            engine.log.info("[%s] 已合併 %d 頁 → %s", b["name"], n, folder / f"{b['name']}_翻譯.md")
            n_done += 1
        self.status_var.set(f"已重組 {n_done} 本的 Markdown")

    # ---------- 事件迴圈 ----------
    def poll(self) -> None:
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "log":
                    self.append_log(item[1], item[2])
                elif kind == "book_start":
                    self._on_book_start(item[1])
                elif kind == "book_done":
                    if item[1] in self.books:
                        self.books[item[1]]["status"] = STATUS_TEXT.get(item[2], item[2])
                    self.refresh_stats(once=True)
                elif kind == "fatal":
                    messagebox.showerror("翻譯中止", item[1])
                elif kind == "finished":
                    self._on_finished()
        except queue.Empty:
            pass
        self.root.after(120, self.poll)

    def _on_book_start(self, iid: str) -> None:
        b = self.books.get(iid)
        if not b:
            return
        b["status"] = "翻譯中"
        folder = self.out_root(b["path"]) / b["name"]
        self.cur = {"iid": iid, "t0": time.time(), "done0": count_pages(folder / "pages")}
        self.refresh_stats(once=True)

    def _on_finished(self) -> None:
        stopped = engine.STOP.is_set()
        self.cur = None
        if self.file_handler is not None:
            engine.log.removeHandler(self.file_handler)
            self.file_handler.close()
            self.file_handler = None
        for b in self.books.values():
            if b["status"] in ("排隊中", "翻譯中"):
                b["status"] = "已停止"
        self._set_running_ui(False)
        self.refresh_stats(once=True)
        self.status_var.set("已停止" if stopped else "全部處理完畢")
        if not stopped:
            lines = [f"{b['name']}：{b['status']}（{b['done']}/{b['total'] or '?'} 頁，失敗 {b['failed']}）"
                     for b in self.books.values()]
            messagebox.showinfo("翻譯結束", "\n".join(lines) +
                                "\n\n有失敗頁時，再按一次「開始翻譯」即會自動重試。")

    def append_log(self, level: int, msg: str) -> None:
        msg = "".join(c for c in msg if ord(c) <= 0xFFFF)  # Tk 在部分系統無法顯示 emoji 等字元
        tag = "error" if level >= logging.ERROR else "warn" if level >= logging.WARNING else ""
        at_bottom = self.log_text.yview()[1] >= 0.999
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n", tag)
        total = int(self.log_text.index("end-1c").split(".")[0])
        if total > 3000:
            self.log_text.delete("1.0", f"{total - 3000}.0")
        if at_bottom:
            self.log_text.see("end")
        self.log_text.configure(state="disabled")
        self.status_var.set(msg[:160])

    # ---------- 進度與統計 ----------
    def refresh_stats(self, once: bool = False) -> None:
        try:
            for iid, b in self.books.items():
                folder = self.out_root(b["path"]) / b["name"]
                b["done"] = count_pages(folder / "pages")
                b["failed"] = count_failed(folder / "failed_pages.json")
                total = b["total"]
                if b["status"] == "待處理" and b["done"]:
                    b["status"] = "有進度" if not (total and b["done"] >= total) else "完成"
                vals = (b["path"].name, total or "?", f"{b['done']}/{total or '?'}",
                        b["failed"] or "-", b["status"])
                if tuple(str(v) for v in self.tree.item(iid, "values")) != tuple(str(v) for v in vals):
                    self.tree.item(iid, values=vals)
            self._update_progress()
            self._update_key_label()
        except Exception:
            pass
        if not once:
            self.root.after(1500, self.refresh_stats)

    def _update_progress(self) -> None:
        b = None
        if self.running and self.cur:
            b = self.books.get(self.cur["iid"])
        else:
            sel = [i for i in self.tree.selection() if i in self.books]
            if sel:
                b = self.books[sel[0]]
            elif len(self.books) == 1:
                b = next(iter(self.books.values()))
        if not b:
            self.progress_var.set(0)
            self.progress_text.set("尚未開始" if not self.books else "請選取一個 PDF 查看進度")
            return
        total, done = b["total"] or 0, b["done"]
        self.progress_var.set(done / total * 100 if total else 0)
        text = f"《{b['name']}》  完成 {done} / {total or '?'} 頁  ·  失敗 {b['failed']} 頁"
        if self.running and self.cur and self.books.get(self.cur["iid"]) is b:
            gained = done - self.cur["done0"]
            if gained > 0 and total:
                per = (time.time() - self.cur["t0"]) / gained
                text += f"  ·  平均 {per:.0f} 秒/頁  ·  預估剩餘 {fmt_dur(per * (total - done))}"
        self.progress_text.set(text)

    # ---------- 關閉 ----------
    def on_close(self) -> None:
        if self.running:
            if not messagebox.askyesno("翻譯進行中", "目前仍在翻譯。確定要結束嗎？\n（已完成的頁面都已存檔，下次可續傳）"):
                return
            engine.STOP.set()
        self.save_settings()
        self.root.destroy()


def main() -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # 高 DPI 螢幕文字更清晰
        except Exception:
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
