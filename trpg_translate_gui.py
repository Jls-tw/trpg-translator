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

import datetime
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

try:  # 系統匣功能（選用）：pip install pystray pillow；macOS 不支援
    import pystray
    from PIL import Image, ImageDraw
    TRAY_AVAILABLE = sys.platform != "darwin"
except Exception:
    pystray = None
    TRAY_AVAILABLE = False

SETTINGS_FILE = APP_DIR / "gui_settings.json"
DEFAULT_FALLBACKS = "gemini-3.6-flash, gemini-3.5-flash-lite, gemini-3.1-flash-lite"
DARK = {  # 深色模式配色
    "bg": "#1f1f23", "fg": "#e8e8ea", "field": "#2b2b31", "btn": "#38383f", "hover": "#45454d",
    "border": "#4a4a52", "sel": "#2f5d8a", "dis": "#7a7a82", "hint": "#a0a0a8", "accent": "#3b82f6",
    "warn": "#fbbf24", "err": "#f87171",
}
LIST_H0, LOG_H0 = 150, 190   # PDF 清單 / 執行記錄 的初始高度（像素）
LIST_MIN, LOG_MIN = 90, 80   # 兩者可縮到的最小高度
ENV_FILE = APP_DIR / ".env"
try:
    from dotenv import load_dotenv

    load_dotenv(ENV_FILE)  # 明確讀取 APP_DIR 內的 .env
except ImportError:
    pass
STATUS_TEXT = {"complete": "完成", "partial": "部分完成", "stopped": "已停止", "error": "錯誤"}
THINKING_CHOICES = ["none", "minimal", "low", "medium", "high"]
# 現行 Gemini 3 系列中，可用於「讀 PDF 並輸出文字譯文」的型號（Live / TTS / 圖片 / 轉錄 / Omni 等型號不適用）
MODEL_CHOICES = [
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3-flash-preview",
    "gemini-3.1-pro-preview",
]


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
        root.geometry("1020x860")
        root.minsize(920, 560)
        self._drag = None

        self.q: "queue.Queue" = queue.Queue()
        self.books: Dict[str, dict] = {}
        self.running = False          # 背景工作執行緒正在翻譯
        self.waiting = False          # 排程：等待開始時間中
        self.run_token = None         # 這一輪工作的識別物件（強制停止後，舊工作的事件會被忽略）
        self.km = None                # 目前工作使用的 KeyManager
        self.sched: Optional[dict] = None
        self.pending: Optional[dict] = None
        self.end_ts: Optional[float] = None
        self._end_signaled = False
        self.cur: Optional[dict] = None
        self.file_handler: Optional[logging.Handler] = None
        self.tray_icon = None
        self.grips: List[tk.Widget] = []
        self.keys: List[str] = parse_keys(os.environ.get("GEMINI_API_KEYS", ""))

        self._setup_style()
        self._init_vars()
        self._build_ui()
        self._setup_logging()
        self.load_settings()
        self._apply_theme()
        self._update_key_label()

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind("<Unmap>", self._on_unmap)
        self.poll()
        self.refresh_stats()
        self._sched_tick()
        root.after(250, self._init_layout)

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
        self._ui_family = ui_family
        self.log_font = (ui_family, 9)
        self.style = ttk.Style()
        self._native_theme = self.style.theme_use()
        self._default_bg = self.root.cget("bg")
        self._configure_custom_styles(dark=False)

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
        # 排程
        self.sched_start_on = tk.BooleanVar(value=False)
        self.sched_start_h = tk.StringVar(value="23")
        self.sched_start_m = tk.StringVar(value="00")
        self.sched_end_on = tk.BooleanVar(value=False)
        self.sched_end_h = tk.StringVar(value="06")
        self.sched_end_m = tk.StringVar(value="00")
        self.sched_daily = tk.BooleanVar(value=False)
        self.sched_text = tk.StringVar(value="")
        # 自動切換模型
        self.auto_var = tk.BooleanVar(value=False)
        self.fallback_var = tk.StringVar(value=DEFAULT_FALLBACKS)
        self.congest_var = tk.StringVar(value=str(engine.Config.congest_threshold))
        self.recover_var = tk.StringVar(value=str(engine.Config.recover_minutes))
        # 視窗選項
        self.dark_var = tk.BooleanVar(value=False)
        self.tray_var = tk.BooleanVar(value=False)

    # ---------- 介面 ----------
    def _build_ui(self) -> None:
        root = self.root
        root.columnconfigure(0, weight=1)
        # 版面（由上到下）：0 PDF 清單｜1 清單拖曳條｜2 設定｜3 操作列｜4 進度｜5 執行記錄｜6 記錄拖曳條｜7 狀態列
        # 視窗放大時，PDF 清單與執行記錄平均分配多出來的高度；
        # 各自下方的拖曳條可「單獨」調整該框高度（視窗高度會跟著增減，不會去擠壓另一個框）。
        root.rowconfigure(0, weight=1)
        root.rowconfigure(5, weight=1)

        # --- ① PDF 清單 ---
        box = ttk.LabelFrame(root, text=" ① 要翻譯的 PDF ", padding=8)
        box.grid(row=0, column=0, sticky="nsew", padx=10, pady=(10, 0))
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        box.grid_propagate(False)       # 高度由拖曳條控制，不被內容撐大
        box.configure(height=LIST_H0)
        self.list_frame = box
        self._make_grip(1, "list")

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
        ttk.Label(box, text="未選取任何項目時，會處理清單中的全部 PDF；雙擊可開啟譯文。"
                            "拖曳各框下方的橫條，可分別調整清單與執行記錄的高度。",
                  style="Hint.TLabel").grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # --- ②③④⑤ 設定 ---
        mid = ttk.Frame(root)
        mid.grid(row=2, column=0, sticky="ew", padx=10, pady=4)
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

        model_values = [engine.Config.model] + [m for m in MODEL_CHOICES if m != engine.Config.model]
        add_row(0, 0, "模型", ttk.Combobox(sf, textvariable=self.model_var, width=24, values=model_values))
        add_row(0, 2, "思考強度", ttk.Combobox(sf, textvariable=self.thinking_var, width=10,
                                              values=THINKING_CHOICES, state="readonly"))
        add_row(1, 0, "單次逾時（秒）", ttk.Spinbox(sf, from_=10, to=900, increment=10, textvariable=self.timeout_var, width=8))
        add_row(1, 2, "請求間隔（秒）", ttk.Spinbox(sf, from_=0, to=120, increment=1, textvariable=self.interval_var, width=8))
        add_row(2, 0, "單頁重試上限", ttk.Spinbox(sf, from_=1, to=20, textvariable=self.maxfails_var, width=8))
        add_row(2, 2, "連續失敗中止", ttk.Spinbox(sf, from_=1, to=50, textvariable=self.consec_var, width=8))
        add_row(3, 0, "起始 Key 編號", ttk.Spinbox(sf, from_=1, to=99, textvariable=self.startkey_var, width=8))
        add_row(3, 2, "PDF 傳送方式", ttk.Combobox(sf, textvariable=self.transport_var, width=10,
                                                  values=["files", "inline"], state="readonly"))
        ttk.Label(sf, text="免費層可用：3.8 / 3.6 Flash、3.5 / 3.1 Flash-Lite；3.1 Pro 需付費。也可直接輸入其他型號名稱。",
                  style="Hint.TLabel", wraplength=560, justify="left").grid(
            row=4, column=0, columnspan=4, sticky="w", pady=(2, 4))
        auto_cb = ttk.Checkbutton(sf, text="伺服器塞車或額度用完時，自動切換到備援模型", variable=self.auto_var)
        auto_cb.grid(row=5, column=0, columnspan=4, sticky="w", pady=(2, 1))
        self.settings_widgets.append(auto_cb)
        ttk.Label(sf, text="備援模型（依序）").grid(row=6, column=0, sticky="e", padx=(0, 6), pady=3)
        fb = ttk.Entry(sf, textvariable=self.fallback_var)
        fb.grid(row=6, column=1, columnspan=3, sticky="ew", padx=(0, 12), pady=3)
        self.settings_widgets.append(fb)
        add_row(7, 0, "塞車判定（連續錯誤次數）", ttk.Spinbox(sf, from_=1, to=20, textvariable=self.congest_var, width=8))
        add_row(7, 2, "回頭試主模型（分鐘）", ttk.Spinbox(sf, from_=1, to=1440, textvariable=self.recover_var, width=8))
        for i, (var, text) in enumerate((
            (self.prev_var, "附上上一頁 PDF 當上下文（品質較好，成本約 2 倍）"),
            (self.join_var, "合併時接回被分頁切斷的段落"),
            (self.markers_var, "合併檔加入頁碼標記 <!-- p.N -->（會停用段落接合）"),
            (self.move_var, "全部完成後，把原始 PDF 移進輸出資料夾"),
        )):
            cb = ttk.Checkbutton(sf, text=text, variable=var)
            cb.grid(row=8 + i, column=0, columnspan=4, sticky="w", pady=1)
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
        ttk.Button(of, text="開啟資料夾", width=10, command=self.on_open_out).grid(row=0, column=2, padx=(6, 0))
        ttk.Label(of, text="譯文與進度會存在此資料夾下的「書名」子資料夾。",
                  style="Hint.TLabel").grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))

        sc = ttk.LabelFrame(right, text=" ⑤ 排程（選用） ", padding=8)
        sc.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        for r, (on_var, text, hv, mv) in enumerate((
            (self.sched_start_on, "開始時間", self.sched_start_h, self.sched_start_m),
            (self.sched_end_on, "結束時間", self.sched_end_h, self.sched_end_m),
        )):
            cb = ttk.Checkbutton(sc, text=text, variable=on_var)
            cb.grid(row=r, column=0, sticky="w", pady=2)
            hs = ttk.Spinbox(sc, from_=0, to=23, wrap=True, width=3, format="%02.0f", textvariable=hv)
            ms = ttk.Spinbox(sc, from_=0, to=59, wrap=True, width=3, format="%02.0f", textvariable=mv)
            hs.grid(row=r, column=1, padx=(10, 0))
            ttk.Label(sc, text=":").grid(row=r, column=2, padx=2)
            ms.grid(row=r, column=3)
            self.settings_widgets += [cb, hs, ms]
        dc = ttk.Checkbutton(sc, text="每天重複（需同時勾選開始與結束）", variable=self.sched_daily)
        dc.grid(row=2, column=0, columnspan=4, sticky="w", pady=2)
        self.settings_widgets.append(dc)
        ttk.Label(sc, textvariable=self.sched_text, style="Hint.TLabel", wraplength=330,
                  justify="left").grid(row=3, column=0, columnspan=4, sticky="w", pady=(4, 0))

        # --- 操作列 ---
        bar = ttk.Frame(root)
        bar.grid(row=3, column=0, sticky="ew", padx=10, pady=6)
        self.start_btn = ttk.Button(bar, text="▶ 開始翻譯", style="Start.TButton", command=self.on_start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(bar, text="■ 停止", command=self.on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        self.force_btn = ttk.Button(bar, text="強制停止", style="Danger.TButton",
                                    command=self.on_force_stop, state="disabled")
        self.force_btn.pack(side="left")
        self.merge_btn = ttk.Button(bar, text="重組 Markdown", command=self.on_merge)
        self.merge_btn.pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="管理 Key…", command=self.on_manage_keys).pack(side="right")
        ttk.Label(bar, textvariable=self.key_var).pack(side="right", padx=10)

        # --- 進度 ---
        pf = ttk.Frame(root)
        pf.grid(row=4, column=0, sticky="ew", padx=10, pady=(0, 6))
        pf.columnconfigure(0, weight=1)
        ttk.Progressbar(pf, variable=self.progress_var, maximum=100).grid(row=0, column=0, sticky="ew")
        ttk.Label(pf, textvariable=self.progress_text).grid(row=1, column=0, sticky="w", pady=(4, 0))

        # --- 記錄（下半部）---
        lf = ttk.LabelFrame(root, text=" 執行記錄 ", padding=4)
        lf.grid(row=5, column=0, sticky="nsew", padx=10, pady=(0, 0))
        lf.columnconfigure(0, weight=1)
        lf.rowconfigure(0, weight=1)
        lf.grid_propagate(False)
        lf.configure(height=LOG_H0)
        self.log_frame = lf
        self.log_text = ScrolledText(lf, height=8, state="disabled", wrap="word", font=self.log_font)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        self.log_text.tag_configure("warn", foreground="#b45309")
        self.log_text.tag_configure("error", foreground="#b91c1c")

        self._make_grip(6, "log")

        foot = ttk.Frame(root)
        foot.grid(row=7, column=0, sticky="ew")
        foot.columnconfigure(0, weight=1)
        ttk.Label(foot, textvariable=self.status_var, relief="sunken", anchor="w", padding=(6, 2)).grid(
            row=0, column=0, sticky="ew")
        self.tray_cb = ttk.Checkbutton(foot, text="最小化時縮到系統匣", variable=self.tray_var,
                                       command=self._on_tray_toggle)
        self.tray_cb.grid(row=0, column=1, padx=(10, 6))
        if sys.platform == "darwin":  # macOS 不支援縮到系統匣，請用 Dock 縮小視窗
            self.tray_cb.grid_remove()
        self.theme_btn = ttk.Button(foot, text="深色模式", command=self.on_toggle_theme)
        self.theme_btn.grid(row=0, column=2, padx=(0, 6), pady=2)

    def _init_layout(self) -> None:
        """依實際內容決定視窗初始高度與最小高度。"""
        try:
            self.root.update_idletasks()
            req_h = self.root.winfo_reqheight()
            fixed = req_h - LIST_H0 - LOG_H0  # 設定、操作列、進度、狀態列等固定區塊的總高度
            self.root.minsize(920, fixed + LIST_MIN + LOG_MIN)
            self.root.geometry(f"1020x{min(req_h, self.root.winfo_screenheight() - 90)}")
        except tk.TclError:
            pass

    # ---------- 可拖曳調整高度的橫條 ----------
    def _make_grip(self, row: int, which: str) -> None:
        g = tk.Frame(self.root, height=9, bd=2, relief="ridge", cursor="sb_v_double_arrow")
        g.grid(row=row, column=0, sticky="ew", padx=10, pady=(2, 2))
        g.bind("<ButtonPress-1>", lambda e, k=which: self._grip_press(k, e))
        g.bind("<B1-Motion>", self._grip_drag)
        g.bind("<ButtonRelease-1>", self._grip_release)
        self.grips.append(g)

    def _grip_frame(self, which: str):
        return self.list_frame if which == "list" else self.log_frame

    def _grip_press(self, which: str, event) -> None:
        # 先把兩個框「凍結」成目前實際高度，之後只改被拖曳的那一個，並同步增減視窗高度
        for f in (self.list_frame, self.log_frame):
            f.configure(height=max(int(f.winfo_height()), 1))
        self._drag = {"which": which, "y0": event.y_root,
                      "h0": int(self._grip_frame(which).winfo_height()),
                      "win_h0": int(self.root.winfo_height())}

    def _grip_drag(self, event) -> None:
        d = self._drag
        if not d:
            return
        min_h = LIST_MIN if d["which"] == "list" else LOG_MIN
        want = d["h0"] + (event.y_root - d["y0"])
        delta = max(want, min_h) - d["h0"]
        win_lo = int(self.root.minsize()[1])
        win_hi = max(win_lo, int(self.root.winfo_screenheight()) - 90)  # 不超出螢幕
        win_h = min(max(d["win_h0"] + delta, win_lo), win_hi)
        delta = win_h - d["win_h0"]
        self._grip_frame(d["which"]).configure(height=d["h0"] + delta)
        self.root.geometry(f"{int(self.root.winfo_width())}x{win_h}")

    def _grip_release(self, event) -> None:
        self._drag = None

    def _setup_logging(self) -> None:
        engine.log.setLevel(logging.DEBUG)
        engine.log.addHandler(QueueLogHandler(self.q))

    # ---------- 設定存取 ----------
    def load_settings(self) -> None:
        data = engine.load_json(SETTINGS_FILE, {})
        pairs = [("model", self.model_var), ("thinking", self.thinking_var), ("timeout", self.timeout_var),
                 ("interval", self.interval_var), ("max_fails", self.maxfails_var), ("consec", self.consec_var),
                 ("start_key", self.startkey_var), ("transport", self.transport_var), ("out", self.out_var),
                 ("sched_start_h", self.sched_start_h), ("sched_start_m", self.sched_start_m),
                 ("sched_end_h", self.sched_end_h), ("sched_end_m", self.sched_end_m),
                 ("fallback", self.fallback_var), ("congest", self.congest_var), ("recover", self.recover_var)]
        for k, var in pairs:
            if k not in data:
                continue
            if k == "timeout" and str(data[k]).isdigit() and int(data[k]) < 300:
                continue  # 舊版預設 90 秒會把「慢但會成功」的請求砍成 504，載入時忽略
            var.set(str(data[k]))
        for k, var in (("prev", self.prev_var), ("join", self.join_var),
                       ("markers", self.markers_var), ("move", self.move_var),
                       ("sched_start_on", self.sched_start_on), ("sched_end_on", self.sched_end_on),
                       ("sched_daily", self.sched_daily), ("auto", self.auto_var),
                       ("dark", self.dark_var), ("tray", self.tray_var)):
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
            "sched_start_on": self.sched_start_on.get(), "sched_start_h": self.sched_start_h.get(),
            "sched_start_m": self.sched_start_m.get(), "sched_end_on": self.sched_end_on.get(),
            "sched_end_h": self.sched_end_h.get(), "sched_end_m": self.sched_end_m.get(),
            "sched_daily": self.sched_daily.get(),
            "auto": self.auto_var.get(), "fallback": self.fallback_var.get(),
            "congest": self.congest_var.get(), "recover": self.recover_var.get(),
            "dark": self.dark_var.get(), "tray": self.tray_var.get(),
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
        if self.running or self.waiting:
            messagebox.showinfo("進行中", "翻譯進行中（或排程等待中），請先停止再修改 Key。")
            return
        win = tk.Toplevel(self.root)
        win.title("管理 API Key")
        win.transient(self.root)
        win.grab_set()
        ttk.Label(win, text="每行一把 Key（也可用逗號分隔）。多把 Key 會在額度用完時自動輪替。",
                  padding=10).pack(anchor="w")
        txt = tk.Text(win, width=64, height=12, font=self.log_font)
        self._style_dialog(win, txt)
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
        model = self.model_var.get().strip() or engine.Config.model
        raw = engine.load_json(self._status_path(), {})
        now = time.time()

        def until_of(kid: str, m: str) -> float:
            vals = []
            for key in (f"{kid}|*", f"{kid}|{m}"):
                try:
                    vals.append(float(raw.get(key, 0) or 0))
                except (TypeError, ValueError):
                    vals.append(0.0)
            return max(vals)

        kids = [hashlib.sha256(k.encode()).hexdigest()[:8] for k in self.keys]
        untils = [until_of(kid, model) for kid in kids]
        ready = sum(1 for u in untils if u <= now)
        text = f"API Key：{model} 可用 {ready} / 共 {n} 把"
        if ready == 0:
            soon = min(untils)
            text += f"（最快 {time.strftime('%H:%M', time.localtime(soon))} 恢復）"
            if self.auto_var.get():  # 開了自動切換：看看備援模型是否還有可用的 Key
                for m in [x for x in re.split(r"[,;\s]+", self.fallback_var.get()) if x]:
                    if any(until_of(kid, m) <= now for kid in kids):
                        text += f"，可改用 {m}"
                        break
        self.key_var.set(text)

    # ---------- 深色模式 ----------
    def _configure_custom_styles(self, dark: bool) -> None:
        style = self.style
        style.configure("Treeview", rowheight=26)
        style.configure("Start.TButton", font=(self._ui_family, 11, "bold"), padding=(14, 6))
        style.configure("Hint.TLabel", foreground=DARK["hint"] if dark else "#666666")
        style.configure("Danger.TButton", foreground=DARK["err"] if dark else "#b91c1c")

    def _apply_theme(self) -> None:
        dark = bool(self.dark_var.get())
        style, root = self.style, self.root
        if dark:
            c = DARK
            style.theme_use("clam")
            style.configure(".", background=c["bg"], foreground=c["fg"], fieldbackground=c["field"],
                            bordercolor=c["border"], lightcolor=c["bg"], darkcolor=c["bg"],
                            troughcolor=c["field"], insertcolor=c["fg"],
                            selectbackground=c["sel"], selectforeground="#ffffff")
            style.map(".", foreground=[("disabled", c["dis"])])
            style.configure("TButton", background=c["btn"], foreground=c["fg"], bordercolor=c["border"],
                            focuscolor=c["btn"])
            style.map("TButton", background=[("active", c["hover"]), ("disabled", c["bg"])],
                      foreground=[("disabled", c["dis"])])
            style.configure("TEntry", fieldbackground=c["field"], foreground=c["fg"], insertcolor=c["fg"])
            style.map("TEntry", fieldbackground=[("disabled", c["bg"])], foreground=[("disabled", c["dis"])])
            for w in ("TCombobox", "TSpinbox"):
                style.configure(w, fieldbackground=c["field"], background=c["btn"], foreground=c["fg"],
                                arrowcolor=c["fg"], insertcolor=c["fg"],
                                selectbackground=c["sel"], selectforeground="#ffffff")
                style.map(w, fieldbackground=[("readonly", c["field"]), ("disabled", c["bg"])],
                          foreground=[("disabled", c["dis"])], arrowcolor=[("disabled", c["dis"])])
            style.configure("TCheckbutton", background=c["bg"], foreground=c["fg"],
                            indicatorbackground=c["field"], indicatorforeground=c["fg"],
                            upperbordercolor=c["border"], lowerbordercolor=c["border"])
            style.map("TCheckbutton", background=[("active", c["bg"])],
                      indicatorbackground=[("selected", c["accent"]), ("disabled", c["bg"])])
            style.configure("TLabelframe", background=c["bg"], bordercolor=c["border"])
            style.configure("TLabelframe.Label", background=c["bg"], foreground=c["fg"])
            style.configure("Treeview", background=c["field"], fieldbackground=c["field"],
                            foreground=c["fg"], bordercolor=c["border"])
            style.map("Treeview", background=[("selected", c["sel"])], foreground=[("selected", "#ffffff")])
            style.configure("Treeview.Heading", background=c["btn"], foreground=c["fg"], bordercolor=c["border"])
            style.map("Treeview.Heading", background=[("active", c["hover"])])
            style.configure("TProgressbar", background=c["accent"], troughcolor=c["field"], bordercolor=c["border"])
            style.configure("Vertical.TScrollbar", background=c["btn"], troughcolor=c["field"],
                            arrowcolor=c["fg"], bordercolor=c["border"])
            style.map("Vertical.TScrollbar", background=[("active", c["hover"])])
            root.option_add("*TCombobox*Listbox.background", c["field"])
            root.option_add("*TCombobox*Listbox.foreground", c["fg"])
            root.option_add("*TCombobox*Listbox.selectBackground", c["sel"])
            root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
            bg, fg, field, warn, err, grip = c["bg"], c["fg"], c["field"], c["warn"], c["err"], c["border"]
        else:
            style.theme_use(self._native_theme)
            root.option_add("*TCombobox*Listbox.background", "#ffffff")
            root.option_add("*TCombobox*Listbox.foreground", "#000000")
            root.option_add("*TCombobox*Listbox.selectBackground", "#0078d7")
            root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
            bg, fg, field, warn, err, grip = self._default_bg, "#000000", "#ffffff", "#b45309", "#b91c1c", self._default_bg
        self._configure_custom_styles(dark)
        root.configure(bg=bg)
        self.log_text.configure(bg=field, fg=fg, insertbackground=fg)
        self.log_text.tag_configure("warn", foreground=warn)
        self.log_text.tag_configure("error", foreground=err)
        for gw in self.grips:
            gw.configure(bg=grip)
        self.theme_btn.configure(text="淺色模式" if dark else "深色模式")
        self._set_titlebar_dark(dark)

    def _set_titlebar_dark(self, dark: bool) -> None:
        """Windows 10/11：讓視窗標題列也跟著變深色（失敗就略過）。"""
        if sys.platform != "win32":
            return
        try:
            import ctypes

            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            val = ctypes.c_int(1 if dark else 0)
            for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE（新舊版 Windows 的代碼不同）
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val))
        except Exception:
            pass

    def on_toggle_theme(self) -> None:
        self.dark_var.set(not self.dark_var.get())
        self._apply_theme()
        self.save_settings()

    def _style_dialog(self, win, txt) -> None:
        if self.dark_var.get():
            win.configure(bg=DARK["bg"])
            txt.configure(bg=DARK["field"], fg=DARK["fg"], insertbackground=DARK["fg"])

    # ---------- 縮到系統匣 ----------
    def _on_tray_toggle(self) -> None:
        if self.tray_var.get() and not TRAY_AVAILABLE:
            msg = ("macOS 不支援縮到系統匣，請直接用 Dock 縮小視窗。" if sys.platform == "darwin"
                   else "需要先安裝 pystray 與 pillow：\n    pip install pystray pillow\n安裝後重新開啟程式。")
            messagebox.showinfo("無法使用系統匣", msg)
            self.tray_var.set(False)
            return
        self.save_settings()

    def _on_unmap(self, event) -> None:
        if event.widget is self.root and self.tray_var.get() and TRAY_AVAILABLE:
            self.root.after(150, self._maybe_hide_to_tray)

    def _maybe_hide_to_tray(self) -> None:
        try:
            if self.root.state() == "iconic":  # 使用者按了「縮小」才收進系統匣
                self._hide_to_tray()
        except tk.TclError:
            pass

    @staticmethod
    def _tray_image():
        img = Image.new("RGB", (64, 64), "#2563eb")
        d = ImageDraw.Draw(img)
        d.rectangle((14, 14, 50, 25), fill="white")  # 「T」字
        d.rectangle((26, 25, 38, 52), fill="white")
        return img

    def _hide_to_tray(self) -> None:
        self.root.withdraw()
        if self.tray_icon is None:
            menu = pystray.Menu(
                pystray.MenuItem("顯示視窗", lambda icon, item: self.q.put(("tray", "show")), default=True),
                pystray.MenuItem("結束程式", lambda icon, item: self.q.put(("tray", "quit"))),
            )
            self.tray_icon = pystray.Icon("trpg_translator", self._tray_image(), "TRPG 規則書翻譯工具", menu)
            self.tray_icon.run_detached()
            try:
                self.tray_icon.notify("仍在背景執行。點兩下系統匣圖示可還原視窗。", "TRPG 翻譯工具")
            except Exception:
                pass

    def _on_tray_event(self, action: str) -> None:
        if action == "show":
            self._restore_from_tray()
        elif action == "quit":
            self._restore_from_tray()
            self.on_close()

    def _restore_from_tray(self) -> None:
        self._stop_tray_icon()
        try:
            self.root.deiconify()
            self.root.state("normal")
            self.root.lift()
            self.root.focus_force()
        except tk.TclError:
            pass

    def _stop_tray_icon(self) -> None:
        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
            self.tray_icon = None

    def _update_tray_title(self) -> None:
        if self.tray_icon is None:
            return
        t = self.progress_text.get() if self.running else (self.sched_text.get() or "閒置")
        try:
            self.tray_icon.title = ("TRPG 翻譯：" + t)[:120]
        except Exception:
            pass

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
                auto_switch=self.auto_var.get(),
                fallback_models=[m for m in re.split(r"[,;\s]+", self.fallback_var.get()) if m],
                congest_threshold=self._num(self.congest_var, "塞車判定次數", 1, 20),
                recover_minutes=self._num(self.recover_var, "回頭試主模型（分鐘）", 1, 1440),
            )
            if cfg.auto_switch and not cfg.fallback_models:
                raise ValueError("已啟用自動切換模型，但「備援模型」是空的。請至少填一個，例如：gemini-3.5-flash-lite")
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
    def _set_busy_ui(self, busy: bool) -> None:
        """busy = 執行中或排程等待中：鎖住設定、啟用停止按鈕。"""
        self.start_btn.configure(state="disabled" if busy else "normal")
        self.stop_btn.configure(state="normal" if busy else "disabled")
        self.force_btn.configure(state="normal" if busy else "disabled")
        self.merge_btn.configure(state="disabled" if busy else "normal")
        for w in self.list_buttons + [self.out_browse, self.out_entry] + self.settings_widgets + self.range_widgets:
            try:
                w.state(["disabled"] if busy else ["!disabled"])
            except Exception:
                pass

    def on_start(self) -> None:
        if self.running or self.waiting:
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
        try:
            sched = self._read_schedule()
        except ValueError as e:
            messagebox.showerror("排程設定有誤", str(e))
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

        self.pending = {"targets": targets, "jobs": [(iid, self.books[iid]["path"]) for iid in targets],
                        "cfg": cfg, "keys": list(self.keys), "out": out}
        if sched["start"]:
            start_ts = self._next_occurrence(sched["start"][0], sched["start"][1], time.time())
            self.sched = dict(sched, start_ts=start_ts)
            self.waiting = True
            self._set_busy_ui(True)
            self._update_sched_text(time.time())
            engine.log.info("已設定排程，將於 %s 開始。電腦需保持開機且不要睡眠。", self._fmt_ts(start_ts))
        else:
            self.sched = dict(sched, start_ts=None) if sched["end"] else None
            self._launch()

    def _launch(self) -> None:
        """實際開始翻譯（立即開始，或排程時間到）。"""
        p = self.pending
        if not p:
            return
        sched = self.sched
        self.end_ts = None
        if sched and sched.get("end"):
            self.end_ts = self._next_occurrence(sched["end"][0], sched["end"][1], time.time())
        self._end_signaled = False

        self._detach_file_handler()
        self.file_handler = logging.FileHandler(Path(p["out"]) / "translator.log", encoding="utf-8")
        self.file_handler.setLevel(logging.DEBUG)
        self.file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        engine.log.addHandler(self.file_handler)

        engine.STOP.clear()
        self.run_token = object()
        self.running = True
        self.waiting = False
        for iid in p["targets"]:
            if iid in self.books:
                self.books[iid]["status"] = "排隊中"
        self._set_busy_ui(True)
        self.status_var.set("翻譯中…")
        if self.end_ts:
            engine.log.info("本輪將於 %s 自動停止。", self._fmt_ts(self.end_ts))
        threading.Thread(target=self._worker, args=(self.run_token, p["jobs"], p["cfg"], p["keys"]),
                         daemon=True).start()

    def on_stop(self) -> None:
        """安全停止：等目前這次請求結束後才停（也會取消排程的每日重複）。"""
        if self.waiting and not self.running:
            self._cancel_waiting("已取消排程")
            return
        if not self.running:
            return
        self.sched = None
        engine.STOP.set()
        self.stop_btn.configure(state="disabled")
        self.status_var.set("正在停止…將在目前這次請求結束後安全停止（最久約等於「單次逾時」秒數）。"
                            "不想等可按「強制停止」。")

    def on_force_stop(self) -> None:
        """強制停止：立刻放棄目前進行中的請求，不等回應。"""
        if not (self.running or self.waiting):
            return
        if self.waiting and not self.running:
            self._cancel_waiting("已取消排程")
            return
        if not messagebox.askyesno(
                "強制停止",
                "強制停止會立刻放棄目前正在等待回應的請求（它的結果會被丟棄，仍可能消耗一次額度）。\n"
                "已完成的頁面都已存檔，不受影響，下次可續傳。\n\n確定要強制停止嗎？"):
            return
        engine.abort_all()
        self.run_token = None
        self.running = False
        self.waiting = False
        self.sched = None
        self.pending = None
        self.end_ts = None
        self._end_signaled = False
        self.cur = None
        self.km = None
        self._detach_file_handler()
        for b in self.books.values():
            if b["status"] in ("排隊中", "翻譯中"):
                b["status"] = "已強制停止"
        self._set_busy_ui(False)
        self.refresh_stats(once=True)
        engine.log.warning("已強制停止。背景中尚未回應的請求會被丟棄。")
        self.status_var.set("已強制停止")

    def _cancel_waiting(self, msg: str) -> None:
        self.waiting = False
        self.sched = None
        self.pending = None
        self._set_busy_ui(False)
        self._update_sched_text(time.time())
        engine.log.info(msg)
        self.status_var.set(msg)

    def _detach_file_handler(self) -> None:
        if self.file_handler is not None:
            engine.log.removeHandler(self.file_handler)
            self.file_handler.close()
            self.file_handler = None

    # ---------- 排程 ----------
    @staticmethod
    def _next_occurrence(h: int, m: int, after_ts: float) -> float:
        """after_ts 之後第一個「時:分」的時間點（已過就算明天）。"""
        dt = datetime.datetime.fromtimestamp(after_ts).replace(hour=h, minute=m, second=0, microsecond=0)
        if dt.timestamp() <= after_ts:
            dt += datetime.timedelta(days=1)
        return dt.timestamp()

    @staticmethod
    def _fmt_ts(ts: float) -> str:
        return datetime.datetime.fromtimestamp(ts).strftime("%m/%d %H:%M")

    def _read_schedule(self) -> dict:
        """讀取排程設定；格式錯誤時拋出 ValueError。"""
        def hm(on, hv, mv, label):
            if not on.get():
                return None
            return (self._num(hv, f"{label}（時）", 0, 23), self._num(mv, f"{label}（分）", 0, 59))
        start = hm(self.sched_start_on, self.sched_start_h, self.sched_start_m, "開始時間")
        end = hm(self.sched_end_on, self.sched_end_h, self.sched_end_m, "結束時間")
        daily = bool(self.sched_daily.get())
        if daily and not (start and end):
            raise ValueError("「每天重複」需要同時勾選「開始時間」與「結束時間」。")
        return {"start": start, "end": end, "daily": daily}

    def _sched_tick(self) -> None:
        """每秒檢查：到開始時間就啟動；到結束時間就安全停止。"""
        try:
            now = time.time()
            if self.waiting and self.sched and self.sched.get("start_ts") and now >= self.sched["start_ts"]:
                engine.log.info("已到排程開始時間，開始翻譯。")
                self._launch()
            elif self.running and self.end_ts and not self._end_signaled and now >= self.end_ts:
                self._end_signaled = True
                engine.log.info("已到排程結束時間，將在目前這次請求結束後停止。")
                engine.STOP.set()
                self.stop_btn.configure(state="disabled")
            self._update_sched_text(now)
            self._update_tray_title()
        except Exception:
            pass
        self.root.after(1000, self._sched_tick)

    def _update_sched_text(self, now: float) -> None:
        if self.waiting and self.sched and self.sched.get("start_ts"):
            st = self.sched["start_ts"]
            t = f"排程等待中：{self._fmt_ts(st)} 開始（還有 {fmt_dur(st - now)}）"
            if self.sched.get("end"):
                t += f"，{self._fmt_ts(self._next_occurrence(self.sched['end'][0], self.sched['end'][1], st))} 停止"
            self.sched_text.set(t)
            self.status_var.set(t)
        elif self.running and self.end_ts:
            self.sched_text.set(f"執行中：將於 {self._fmt_ts(self.end_ts)} 自動停止（還有 {fmt_dur(self.end_ts - now)}）")
        elif self.running or self.waiting:
            self.sched_text.set("")
        else:
            try:
                sc = self._read_schedule()
            except ValueError as e:
                self.sched_text.set(str(e))
                return
            if not sc["start"] and not sc["end"]:
                self.sched_text.set("未設定排程：按「開始翻譯」會立即開始，做完為止。")
                return
            base, parts = now, []
            if sc["start"]:
                base = self._next_occurrence(sc["start"][0], sc["start"][1], now)
                parts.append(f"{self._fmt_ts(base)} 開始")
            else:
                parts.append("立即開始")
            if sc["end"]:
                parts.append(f"{self._fmt_ts(self._next_occurrence(sc['end'][0], sc['end'][1], base))} 停止")
            self.sched_text.set("預計：" + "，".join(parts) + ("（每天重複）" if sc["daily"] else ""))

    # ---------- 開啟輸出資料夾 ----------
    def on_open_out(self) -> None:
        v = self.out_var.get().strip()
        if not v:
            messagebox.showinfo("尚未設定", "請先新增 PDF，或在「輸出資料夾」欄位選擇資料夾。")
            return
        p = Path(v).expanduser()
        if not p.is_dir():
            messagebox.showinfo("資料夾不存在", f"{p}\n\n開始翻譯後才會建立。")
            return
        open_path(p)

    def _status_path(self) -> Path:
        return Path(self.out_var.get().strip() or ".") / engine.KEY_STATUS_FILE


    def _worker(self, token, jobs, cfg, keys) -> None:
        log = engine.log
        put = lambda *a: self.q.put(a)
        try:
            km = engine.KeyManager(keys, cfg.start_key - 1, cfg.timeout,
                                   str(Path(cfg.output_root) / engine.KEY_STATUS_FILE))
            if token is self.run_token:
                self.km = km
            log.info("模型 %s 啟動，共 %d 把 Key（從 %s 開始），%d 個 PDF", cfg.model, len(km.keys), km.label, len(jobs))
            for iid, path in jobs:
                if engine.STOP.is_set() or token is not self.run_token:
                    break
                put("book_start", token, iid)
                job = engine.BookJob(path, cfg, km)
                try:
                    status = job.run()
                except engine.FatalError as e:
                    log.error("致命錯誤，中止：%s", e)
                    put("book_done", token, iid, "error")
                    put("fatal", token, str(e))
                    break
                if job.tokens_in or job.tokens_out:
                    log.info("[%s] 本次 token 用量：in=%d out=%d", job.name, job.tokens_in, job.tokens_out)
                put("book_done", token, iid, status)
        except Exception as e:
            log.error("未預期的錯誤：%s: %s", type(e).__name__, e)
            log.debug("traceback", exc_info=True)
            put("fatal", token, f"{type(e).__name__}: {e}")
        finally:
            put("finished", token)

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
                    continue
                if kind == "tray":
                    self._on_tray_event(item[1])
                    continue
                if item[1] is not self.run_token:
                    continue  # 已被「強制停止」作廢的舊工作事件，直接忽略
                if kind == "book_start":
                    self._on_book_start(item[2])
                elif kind == "book_done":
                    if item[2] in self.books:
                        self.books[item[2]]["status"] = STATUS_TEXT.get(item[3], item[3])
                    self.refresh_stats(once=True)
                elif kind == "fatal":
                    if self.sched is None:  # 排程執行時不跳視窗，避免半夜卡在對話框
                        messagebox.showerror("翻譯中止", item[2])
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
        by_schedule = self._end_signaled
        sched = self.sched
        now = time.time()
        self.running = False
        self.run_token = None
        self.cur = None
        self.km = None
        self.end_ts = None
        self._end_signaled = False
        self._detach_file_handler()
        for b in self.books.values():
            if b["status"] in ("排隊中", "翻譯中"):
                b["status"] = "已停止"

        # 排程「每天重複」：是到了結束時間才停的，就等下一個開始時間再繼續
        if by_schedule and sched and sched.get("daily") and sched.get("start_ts") and self.pending:
            nxt = sched["start_ts"] + 86400
            while nxt <= now:
                nxt += 86400
            sched["start_ts"] = nxt
            self.waiting = True
            self.refresh_stats(once=True)
            engine.log.info("本輪排程結束，下次將於 %s 開始。", self._fmt_ts(nxt))
            return

        self.waiting = False
        self.sched = None
        self.pending = None
        self._set_busy_ui(False)
        self.refresh_stats(once=True)
        if by_schedule:
            self.status_var.set("已到排程結束時間，已停止")
        else:
            self.status_var.set("已停止" if stopped else "全部處理完畢")
        if not stopped and sched is None:
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
        if self.running or self.waiting:
            msg = ("目前仍在翻譯。確定要結束嗎？\n（已完成的頁面都已存檔，下次可續傳）" if self.running
                   else "目前有排程等待中，結束程式會取消排程。確定要結束嗎？")
            if not messagebox.askyesno("確定要結束？", msg):
                return
            engine.abort_all()
        self._stop_tray_icon()
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
