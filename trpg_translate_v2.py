#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TRPG 規則書 PDF 逐頁翻譯工具 v2

安裝：
    pip install google-genai pypdf pydantic python-dotenv
    pip install pikepdf        # 選用：處理加密 PDF

環境變數（或 .env）：
    GEMINI_API_KEYS="key1,key2,key3"
    GEMINI_MODEL_ID="..."      # 選用，可被 --model 覆蓋

常用範例：
    python trpg_translate_v2.py                          # 翻譯目前資料夾所有 *.pdf
    python trpg_translate_v2.py --pdf book.pdf --start 20 --end 60
    python trpg_translate_v2.py --pdf book.pdf --redo 12,30-32   # 強制重翻指定頁
    python trpg_translate_v2.py --merge-only             # 不呼叫 API，只用快取重組 Markdown

輸出結構（每本書一個資料夾）：
    <書名>/pages/0001.json   每頁一個檔案（斷點續傳的唯一依據；檔案存在 = 該頁完成）
    <書名>/glossary.json     術語庫
    <書名>/failed_pages.json 失敗頁清單（下次執行會自動重試）
    <書名>/<書名>_翻譯.md    由 pages/ 合併而成，可隨時重新產生
"""
from __future__ import annotations

import argparse
import datetime
import glob
import hashlib
import io
import json
import logging
import os
import re
import shutil
import signal
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from google import genai
from google.genai import errors, types
from pydantic import BaseModel, Field
from pypdf import PdfReader, PdfWriter

try:
    import pikepdf
except ImportError:
    pikepdf = None

log = logging.getLogger("trpg")
STOP = threading.Event()
RUN_GEN = [0]  # 每次「強制停止」加 1；舊世代的背景執行緒偵測到不一致就會自行退出，且不再寫入任何結果


def abort_all() -> None:
    """強制停止：作廢所有進行中的工作（背景正在等待回應的請求結果會被丟棄）。"""
    RUN_GEN[0] += 1
    STOP.set()

INLINE_LIMIT = 15 * 1024 * 1024  # 單頁 PDF 超過此大小才改走 Files API
BAD_KEY_COOLDOWN = 3600          # 401/403/無效金鑰：冷卻 1 小時
KEY_STATUS_FILE = "api_key_status.json"


# ============================================================
# 結構化輸出 Schema
# ============================================================
class TermEntry(BaseModel):
    english: str = Field(description="英文術語")
    chinese: str = Field(description="台灣譯名")


class PageTranslation(BaseModel):
    translation: str = Field(
        description="本頁完整譯文，純淨繁體中文 Markdown，不含任何分隔符號或標籤代碼"
    )
    last_english_anchor: str = Field(
        default="",
        description="本頁最後一段完整英文原文（不含頁尾、頁碼；過長時只取最後一句）；無法判斷則留空字串",
    )
    new_terms: List[TermEntry] = Field(
        default_factory=list,
        description="符合收錄準則的新術語列表；若無新術語請回傳空陣列",
    )


class FatalError(Exception):
    """不可恢復的錯誤（例如模型不存在），應中止整個程式。"""


class EmptyResponse(Exception):
    """模型回傳空內容（可能被安全機制攔截或輸出被截斷）。"""


# ============================================================
# 設定
# ============================================================
@dataclass
class Config:
    model: str = "gemini-3.8-flash"
    thinking: str = "low"
    start: int = 1
    end: int = 0
    start_key: int = 1
    max_fails: int = 5               # 單頁一般錯誤的重試上限
    max_rate_limit_retries: int = 20  # 單頁遇到 429 的處理次數上限
    max_consecutive_fails: int = 3   # 連續幾頁失敗就中止（通常代表系統性問題）
    min_interval: float = 6.0        # 兩次請求最小間隔（秒）
    timeout: int = 600               # 單次請求逾時（秒）。第一版為 600；設太短會把「慢但會成功」的請求砍成 504
    transport: str = "files"         # files = 先上傳 Files API（同第一版）；inline = PDF 直接內嵌在請求中
    with_prev_page: bool = False     # 額外附上上一頁 PDF 當上下文（成本約 2 倍）
    page_markers: bool = False       # 合併檔每頁前加 <!-- p.N --> 註解
    join_pages: bool = True          # 合併時嘗試接回被分頁切斷的段落
    glossary_warn: int = 500
    output_root: str = "."           # 輸出資料夾的上層目錄（GUI 使用；CLI 預設為目前資料夾）
    move_pdf: bool = True            # 全部完成後是否把原始 PDF 移進輸出資料夾
    # --- 自動切換模型 ---
    auto_switch: bool = False        # 伺服器塞車 / 額度用完時，自動改用備援模型
    fallback_models: List[str] = field(default_factory=list)  # 備援模型（依優先順序）
    congest_threshold: int = 2       # 同一模型連續幾次伺服器錯誤（503/504/逾時）就判定塞車
    recover_minutes: int = 15        # 判定塞車後，隔幾分鐘再回頭試優先順序較前的模型
    redo: Set[int] = field(default_factory=set)


# ============================================================
# 小工具
# ============================================================
def interruptible_sleep(sec: float) -> bool:
    """可被 Ctrl+C 中斷的 sleep。回傳 True 代表被中止。"""
    end = time.monotonic() + sec
    while not STOP.is_set():
        remaining = end - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.5, remaining))
    return True


def load_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        log.warning("讀取 %s 失敗（%s），改用預設值", path, e)
        return default


def save_json(path: Path, data) -> None:
    """原子寫入：先寫暫存檔再 os.replace。"""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def write_text_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def parse_page_spec(spec: str) -> Set[int]:
    """'5,8-10' -> {5,8,9,10}"""
    pages: Set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            pages.update(range(int(a), int(b) + 1))
        else:
            pages.add(int(part))
    return pages


def tail_text(text: str, limit: int) -> str:
    """取文字結尾約 limit 字元（壓縮空白，盡量從詞界開始）。"""
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[-limit:]
    sp = cut.find(" ")
    return cut[sp + 1:] if 0 <= sp < 60 else cut


# ============================================================
# 429 分類與每日額度重置時間
# ============================================================
def classify_rate_limit(e: Exception) -> Tuple[str, Optional[float]]:
    """回傳 ('daily' | 'rpm', 建議等待秒數或 None)。

    不再只比對 'quota'（RPM 與 RPD 的錯誤都含此字），
    而是檢查 quotaId（例如含 PerDay）與 retryDelay。
    """
    blob = str(e)
    details = getattr(e, "details", None)
    if details:
        try:
            blob += " " + json.dumps(details, ensure_ascii=False, default=str)
        except Exception:
            blob += " " + str(details)
    low = blob.lower()
    daily = "perday" in low or "per day" in low or "daily" in low
    retry = None
    m = re.search(r"retrydelay\W+(\d+(?:\.\d+)?)s", low) or re.search(
        r"retry in (\d+(?:\.\d+)?)s", low
    )
    if m:
        retry = float(m.group(1))
    return ("daily" if daily else "rpm"), retry


def next_daily_reset_ts() -> float:
    """Gemini 每日額度於太平洋時間午夜重置；無法取得時區資料則退回 24 小時。"""
    try:
        from zoneinfo import ZoneInfo

        now = datetime.datetime.now(ZoneInfo("America/Los_Angeles"))
        nxt = (now + datetime.timedelta(days=1)).replace(
            hour=0, minute=1, second=0, microsecond=0
        )
        return nxt.timestamp()
    except Exception:
        return time.time() + 86400


# ============================================================
# API Key 管理
# ============================================================
class ModelRouter:
    """決定每一次請求要用哪個模型。

    - 關閉自動切換：永遠只用主要模型。
    - 開啟：依「主要模型 → 備援模型…」的順序使用。某模型連續出現伺服器錯誤（503 / 504 / 逾時）
      就判定為塞車，暫時跳過；經過一段時間後，會回頭試優先順序較前的模型。
    """

    def __init__(self, primary: str, fallbacks=(), enabled: bool = False,
                 threshold: int = 2, recover_minutes: int = 15):
        chain = [primary]
        for m in fallbacks or ():
            m = (m or "").strip()
            if m and m not in chain:
                chain.append(m)
        self.primary = primary
        self.enabled = bool(enabled) and len(chain) > 1
        self.chain: List[str] = chain if self.enabled else [primary]
        self.threshold = max(1, int(threshold))
        self.recover = max(1, int(recover_minutes)) * 60
        self.congested_until: Dict[str, float] = {}
        self.streak: Dict[str, int] = {}

    @classmethod
    def from_config(cls, cfg: "Config") -> "ModelRouter":
        return cls(cfg.model, cfg.fallback_models, cfg.auto_switch, cfg.congest_threshold, cfg.recover_minutes)

    def candidates(self) -> List[str]:
        """依優先順序、排除「塞車冷卻中」的模型；全部都在冷卻就照原順序全部列出。"""
        now = time.time()
        ok = [m for m in self.chain if self.congested_until.get(m, 0.0) <= now]
        return ok or list(self.chain)

    def record_ok(self, model: str) -> None:
        self.streak[model] = 0

    def record_congestion(self, model: str) -> bool:
        """記一次伺服器端錯誤；同一模型連續達門檻就標為塞車並回傳 True。"""
        if not self.enabled:
            return False
        self.streak[model] = self.streak.get(model, 0) + 1
        if self.streak[model] >= self.threshold:
            self.streak[model] = 0
            self.congested_until[model] = time.time() + self.recover
            return True
        return False

    def has_other(self, model: str) -> bool:
        now = time.time()
        return any(m != model and self.congested_until.get(m, 0.0) <= now for m in self.chain)


class KeyManager:
    """管理多把 API Key。冷卻計時以「Key × 模型」分開記錄（Google 的額度本來就是每個模型各自計算），
    所以某模型的每日額度用完時，同一把 Key 仍可用於其他模型。"""

    def __init__(self, keys: List[str], start_index: int, timeout_sec: int,
                 status_path: str = KEY_STATUS_FILE):
        self.gen = RUN_GEN[0]
        seen, uniq = set(), []
        for k in keys:
            if k not in seen:
                seen.add(k)
                uniq.append(k)
        self.keys = uniq
        # 以 key 的 hash 當 ID，調整順序或增刪 key 時冷卻紀錄不會錯位
        self.ids = [hashlib.sha256(k.encode()).hexdigest()[:8] for k in self.keys]
        self.status_path = Path(status_path)
        raw = load_json(self.status_path, {})
        valid_ids = set(self.ids)
        self.until: Dict[str, float] = {}
        for k, v in (raw.items() if isinstance(raw, dict) else []):
            kid, sep, _model = str(k).partition("|")
            if not sep or kid not in valid_ids:
                continue  # 舊版（只記 Key、不分模型）的紀錄不沿用
            try:
                self.until[str(k)] = float(v)
            except (TypeError, ValueError):
                pass
        self.timeout_ms = timeout_sec * 1000
        self._clients: Dict[int, genai.Client] = {}
        self._last_req = 0.0
        self.index = start_index if 0 <= start_index < len(self.keys) else 0
        self.router: Optional[ModelRouter] = None

    # ---- 基本屬性 ----
    @property
    def label(self) -> str:
        return f"Key #{self.index + 1}"

    @property
    def client(self):
        if self.index not in self._clients:
            self._clients[self.index] = genai.Client(
                api_key=self.keys[self.index],
                http_options=types.HttpOptions(timeout=self.timeout_ms),
            )
        return self._clients[self.index]

    def _until(self, idx: int, model: Optional[str] = None) -> float:
        kid = self.ids[idx]
        u = self.until.get(f"{kid}|*", 0.0)  # 整把 Key 的冷卻（無效或無權限）
        if model:
            u = max(u, self.until.get(f"{kid}|{model}", 0.0))
        return u

    def _save(self) -> None:
        now = time.time()
        self.until = {k: v for k, v in self.until.items() if v > now}  # 順便清掉已過期的紀錄
        try:
            save_json(self.status_path, self.until)
        except Exception as e:
            log.warning("無法儲存 Key 狀態：%s", e)

    # ---- 冷卻與切換 ----
    def cooldown(self, until_ts: float, model: str) -> None:
        """目前這把 Key 在「指定模型」上冷卻到 until_ts（不影響其他模型）。"""
        self.until[f"{self.ids[self.index]}|{model}"] = until_ts
        self._save()

    def bad_key(self, reason: str) -> None:
        log.warning("🚫 %s 疑似無效或無權限（%s），所有模型冷卻 %d 分鐘", self.label, reason, BAD_KEY_COOLDOWN // 60)
        self.until[f"{self.ids[self.index]}|*"] = time.time() + BAD_KEY_COOLDOWN
        self._save()

    def has_ready(self, model: str) -> bool:
        now = time.time()
        return any(self._until(i, model) <= now for i in range(len(self.keys)))

    def use_ready(self, model: str) -> bool:
        """讓目前使用的 Key 指向「對這個模型可用」的 Key（優先沿用目前這把）。都不可用回傳 False。"""
        now = time.time()
        n = len(self.keys)
        if self._until(self.index, model) <= now:
            return True
        for i in range(1, n):
            cand = (self.index + i) % n
            if self._until(cand, model) <= now:
                self.index = cand
                return True
        return False

    def earliest_ready(self, models: List[str]) -> Tuple[float, int, str]:
        best = (float("inf"), 0, models[0] if models else "")
        for m in models:
            for i in range(len(self.keys)):
                u = self._until(i, m)
                if u < best[0]:
                    best = (u, i, m)
        return best

    def wait_any_ready(self, models: List[str]) -> bool:
        """所有 Key × 候選模型都在冷卻：顯示倒數並等待，直到任何一組恢復。被中止回傳 False。"""
        t, i, m = self.earliest_ready(models)
        resume = datetime.datetime.fromtimestamp(t).strftime("%m/%d %H:%M:%S")
        log.warning("⏳ 所有 Key 在「%s」都冷卻中，最快 %s 恢復（Key #%d，%s）",
                    " / ".join(models), resume, i + 1, m)
        while not STOP.is_set() and self.gen == RUN_GEN[0]:
            t, i, m = self.earliest_ready(models)  # 每秒重算（外部清除冷卻時能立刻解除等待）
            if time.time() >= t:
                print("\r" + " " * 50 + "\r", end="", flush=True)
                return True
            remaining = int(t - time.time()) + 1
            h, r = divmod(max(remaining, 0), 3600)
            mi, sec = divmod(r, 60)
            print(f"\r💤 倒數 [{h:02d}:{mi:02d}:{sec:02d}]（Ctrl+C 可中止）", end="", flush=True)
            interruptible_sleep(1)
        print("\r" + " " * 50 + "\r", end="", flush=True)
        return False

    def reset_all(self) -> None:
        """清除所有冷卻計時（手動重設；一般不需要，因為冷卻已依模型分開計時）。"""
        self.until = {}
        self._save()

    def wait_turn(self, min_interval: float) -> None:
        wait = self._last_req + min_interval - time.monotonic()
        if wait > 0:
            interruptible_sleep(wait)
        self._last_req = time.monotonic()


# ============================================================
# PDF 讀取
# ============================================================
def open_pdf(path: Path, work_dir: Path) -> Optional[PdfReader]:
    """開啟 PDF；若加密則先嘗試空白密碼，再退回 pikepdf。不會動到原檔。"""
    try:
        reader = PdfReader(str(path))
    except Exception as e:
        log.error("無法開啟 PDF [%s]：%s", path.name, e)
        return None
    if not reader.is_encrypted:
        return reader

    log.info("🔒 [%s] 已加密，嘗試以空白密碼解鎖…", path.name)
    try:
        if reader.decrypt("") != 0:
            return reader
    except Exception as e:
        log.debug("pypdf 解密失敗：%s", e)

    if pikepdf is None:
        log.error("此 PDF 已上鎖且未安裝 pikepdf，請執行：pip install pikepdf")
        return None
    unlocked = work_dir / "_unlocked.pdf"
    try:
        with pikepdf.open(str(path)) as pdf:
            pdf.save(str(unlocked))
        log.info("🔓 已解鎖為副本：%s（原檔未修改）", unlocked)
        return PdfReader(str(unlocked))
    except Exception as e:
        log.error("解鎖失敗（可能需要使用者密碼）：%s", e)
        return None


def parse_response(resp) -> PageTranslation:
    text = ""
    try:
        text = (getattr(resp, "text", None) or "").strip()
    except Exception:
        pass
    if not text:
        finish = block = None
        try:
            finish = resp.candidates[0].finish_reason
        except Exception:
            pass
        try:
            block = resp.prompt_feedback.block_reason
        except Exception:
            pass
        raise EmptyResponse(f"空回應 (finish_reason={finish}, block_reason={block})")
    return PageTranslation.model_validate_json(text)


# ============================================================
# 合併 Markdown
# ============================================================
_END_PUNCT = tuple("。！？!?」』）)”.:：;；…—》")
_STRUCT_START = re.compile(r"^(#|-|\*|>|\||`|<|\d+[.)]\s)")


def _looks_split(prev: str, nxt: str) -> bool:
    """判斷 prev 結尾與 nxt 開頭是否是被分頁切開的同一段落（保守判斷）。"""
    p_lines = prev.rstrip().splitlines()
    n_lines = nxt.lstrip().splitlines()
    if not p_lines or not n_lines:
        return False
    last, first = p_lines[-1].strip(), n_lines[0].strip()
    if len(last) < 12 or not first:
        return False
    if last.endswith(_END_PUNCT):
        return False
    if _STRUCT_START.match(last) or _STRUCT_START.match(first):
        return False
    if p_lines[-1].startswith("    "):
        return False
    return True


def merge_pages(pages_dir: Path, failed_path: Path, out_path: Path,
                join: bool = True, markers: bool = False) -> int:
    items: Dict[int, str] = {}
    for p in pages_dir.glob("*.json"):
        if p.stem.isdigit():
            data = load_json(p, {})
            items[int(p.stem)] = (data.get("translation") or "").strip()
    failed = load_json(failed_path, {}).get("pages", {})
    for k in failed:
        if str(k).isdigit() and int(k) not in items:
            items[int(k)] = f"<!-- ⚠️ 第 {int(k)} 頁翻譯失敗，請重新執行以重試 -->"

    buf, prev = "", ""
    for n in sorted(items):
        text = items[n]
        if not text:
            continue
        chunk = f"<!-- p.{n} -->\n\n{text}" if markers else text
        if not buf:
            buf = chunk
        elif join and not markers and _looks_split(prev, text):
            a, b = buf[-1], text[0]
            buf += (" " if (a.isascii() and a.isalnum() and b.isascii() and b.isalnum()) else "") + text
        else:
            buf += "\n\n" + chunk
        prev = text
    if buf:
        write_text_atomic(out_path, buf.rstrip() + "\n")
    return len(items)


# ============================================================
# 單本書的翻譯流程
# ============================================================
class BookJob:
    def alive(self) -> bool:
        """False 代表這個工作已被「強制停止」作廢，不應再寫入任何東西。"""
        return self.gen == RUN_GEN[0]

    def __init__(self, pdf_path: Path, cfg: Config, km: KeyManager):
        self.gen = RUN_GEN[0]
        self.pdf_path = pdf_path
        self.cfg = cfg
        self.km = km
        self.name = pdf_path.stem
        self.folder = Path(cfg.output_root) / self.name
        self.pages_dir = self.folder / "pages"
        self.glossary_path = self.folder / "glossary.json"
        self.failed_path = self.folder / "failed_pages.json"
        self.output_md = self.folder / f"{self.name}_翻譯.md"
        self.reader: Optional[PdfReader] = None
        self.total = 0
        self.glossary: Dict[str, str] = {}
        self._glossary_lower: Set[str] = set()
        self._text_cache: Dict[int, str] = {}
        self._warned_full_glossary = False
        self.tokens_in = 0
        self.tokens_out = 0
        self.last_upload_s = 0.0
        self.last_gen_s = 0.0
        if getattr(km, "router", None) is None:
            km.router = ModelRouter.from_config(cfg)  # 同一輪的多本書共用同一個 router（塞車狀態可延續）
        self.router: ModelRouter = km.router
        self.active_model: Optional[str] = None
        self.no_thinking: Set[str] = set()  # 不支援 thinking 設定的模型

    # ---------- 路徑 / 狀態 ----------
    def page_path(self, n: int) -> Path:
        return self.pages_dir / f"{n:04d}.json"

    def _prepare(self) -> None:
        self.pages_dir.mkdir(parents=True, exist_ok=True)
        # 舊版（單一 MD、無 pages/）輸出不覆蓋，先備份
        if self.output_md.exists() and not any(self.pages_dir.glob("*.json")):
            legacy = self.output_md.with_name(self.output_md.stem + ".legacy.md")
            if not legacy.exists():
                os.replace(self.output_md, legacy)
                log.warning("偵測到舊版輸出，已備份為 %s（新版從頭以每頁快取重建）", legacy.name)
        self.glossary = {str(k): str(v) for k, v in load_json(self.glossary_path, {}).items()}
        self._glossary_lower = {k.lower() for k in self.glossary}

    def _record_failed(self, n: int, err: Optional[str]) -> None:
        data = load_json(self.failed_path, {"pages": {}})
        pages = data.setdefault("pages", {})
        if err is None:
            pages.pop(str(n), None)
        else:
            pages[str(n)] = err
        save_json(self.failed_path, data)

    # ---------- PDF 內容 ----------
    def page_text(self, n: int) -> str:
        if not 1 <= n <= self.total:
            return ""
        if n not in self._text_cache:
            try:
                text = self.reader.pages[n - 1].extract_text() or ""
            except Exception:
                text = ""
            self._text_cache[n] = text
            while len(self._text_cache) > 6:
                self._text_cache.pop(next(iter(self._text_cache)))
        return self._text_cache[n]

    def page_bytes(self, n: int) -> bytes:
        writer = PdfWriter()
        writer.add_page(self.reader.pages[n - 1])
        buf = io.BytesIO()
        writer.write(buf)
        return buf.getvalue()

    def _upload_pdf(self, data: bytes):
        client = self.km.client
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(data)
            path = tmp.name
        try:
            f = client.files.upload(file=path, config=types.UploadFileConfig(mime_type="application/pdf"))
            while f.state is not None and f.state.name == "PROCESSING":
                if interruptible_sleep(1):
                    break
                f = client.files.get(name=f.name)
            if f.state is not None and f.state.name == "FAILED":
                raise RuntimeError("Files API 處理 PDF 失敗")
            return client, f
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    # ---------- 術語庫 ----------
    def relevant_glossary(self, n: int) -> Dict[str, str]:
        """只帶入本頁文字中實際出現的術語；掃描檔（抽不出文字）則帶入全部。"""
        text = self.page_text(n)
        if len(text.strip()) < 20:
            if len(self.glossary) > self.cfg.glossary_warn and not self._warned_full_glossary:
                log.warning("本頁無可抽取文字，無法過濾術語庫，將帶入全部 %d 條（token 成本較高）", len(self.glossary))
                self._warned_full_glossary = True
            return self.glossary
        low = re.sub(r"\s+", " ", text.lower())
        return {k: v for k, v in self.glossary.items() if k.lower() in low}

    def add_terms(self, terms: List[TermEntry]) -> int:
        added = 0
        for t in terms:
            eng, zh = t.english.strip(), t.chinese.strip()
            if not eng or not zh or len(eng.split()) > 4 or len(eng) > 40:
                continue
            if any(ord(c) > 0x2E7F for c in eng):  # 英文欄位不應含中日韓字元
                continue
            if eng.lower() in self._glossary_lower:  # 保留最早的譯名，維持前後一致
                continue
            self.glossary[eng] = zh
            self._glossary_lower.add(eng.lower())
            added += 1
        return added

    # ---------- Prompt ----------
    def _continuity(self, n: int) -> str:
        if n <= 1:
            return "（本頁是全書第一頁，請直接從頭翻譯。）"
        en = tail_text(self.page_text(n - 1), 300)
        zh = ""
        prev_file = self.page_path(n - 1)
        if prev_file.exists():
            d = load_json(prev_file, {})
            zh = tail_text(d.get("translation", ""), 300)
            if not en:
                en = d.get("last_english_anchor", "")
        if not en and not zh:
            return "（沒有可用的上一頁資訊，請直接從本頁開頭翻譯。）"
        return (
            f"上一頁（第 {n - 1} 頁）原文結尾（可能夾雜頁尾或頁碼，請忽略這些）：\n「{en or '（無法取得）'}」\n"
            f"上一頁已輸出的譯文結尾：\n「{zh or '（無法取得）'}」\n"
            "若本頁開頭是在承接上一頁尚未結束的句子或段落，請只翻譯尚未被上一頁譯文涵蓋的部分，語氣順接、不要重複；"
            "若本頁從新段落或新標題開始，請忽略以上資訊。"
        )

    def build_prompt(self, n: int) -> str:
        prev_note = ""
        if self.cfg.with_prev_page and n > 1:
            prev_note = f"\n注意：附上的第一份 PDF 是上一頁（第 {n - 1} 頁），僅供理解上下文，請勿翻譯；只翻譯第二份 PDF（第 {n} 頁）。\n"
        glossary = json.dumps(self.relevant_glossary(n), ensure_ascii=False)
        return f"""你是一位頂級的 TRPG 規則書譯者。請翻譯本頁 PDF 內容（第 {n} 頁）。{prev_note}

【跨頁接續資訊】(最優先規則)
{self._continuity(n)}

## 翻譯規範
1. 使用繁體中文與台灣 TRPG 慣用語（例：Saving Throw -> 豁免、Proficiency -> 熟練），不使用中國大陸用語。
2. 稍後會提供一份術語庫，譯文中請直接使用對應的中文譯名；不要輸出 <glossary>、term= 等任何標籤代碼，譯文必須是純淨、可直接閱讀的中文。
3. 以段落為單位翻譯，避免破碎句子。
4. 忽略裝飾性圖片、頁首、頁尾、頁碼。
5. new_terms 欄位只收錄具有長期參考價值的專有名詞：NPC/角色姓名、地名、重要場景物件、核心遊戲機制、角色屬性、關鍵物品、狀態效果。不收錄片語、諺語、句子，或已出現在術語庫中的詞彙；沒有符合條件的新術語就留空。

## Markdown 排版規範（套用在 translation 欄位的內容上）
1. 標題：遵守 H1→H2→H3 遞進，不跳級；`#` 與文字間留一個半形空格；內文從 H2/H3 開始。
2. 間距：不同區塊之間只留一個空白行，不連續出現兩個以上空白行。
3. 列表：無序列表統一用 `-`；有序列表用遞增數字；巢狀列表縮排 4 個半形空格。
4. 強調：重點用 **粗體**；專有名詞/檔名/指令用 `反引號`；中英文字之間留一個半形空格。
5. 程式碼區塊：三個反引號並標註語言（```python、```bash、```text）。
6. 不使用 HTML 標籤（如 `<br>`），一律用原生 Markdown。
7. 表格：每列前後都要有 `|`；`|` 與內容間留空格；表頭下方要有含對齊設定的分隔線；儲存格內不換行、不放列表。

## 術語庫
<glossary>
{glossary}
</glossary>

請根據以上規則與術語庫，翻譯第 {n} 頁，並以結構化 JSON 格式回傳（translation / last_english_anchor / new_terms）。
"""

    # ---------- 呼叫模型 ----------
    def call_model(self, n: int, model: str):
        self.last_upload_s = 0.0
        self.last_gen_s = 0.0
        parts, cleanup = [], []
        try:
            pages = [n - 1, n] if (self.cfg.with_prev_page and n > 1) else [n]
            for p in pages:
                data = self.page_bytes(p)
                if self.cfg.transport == "inline" and len(data) <= INLINE_LIMIT:
                    parts.append(types.Part.from_bytes(data=data, mime_type="application/pdf"))
                else:
                    t_up = time.time()
                    client, f = self._upload_pdf(data)
                    self.last_upload_s += time.time() - t_up
                    parts.append(f)
                    cleanup.append((client, f.name))

            kwargs = dict(response_mime_type="application/json", response_schema=PageTranslation)
            if (self.cfg.thinking and self.cfg.thinking.lower() not in ("none", "default")
                    and model not in self.no_thinking):
                kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=self.cfg.thinking)

            t_gen = time.time()
            resp = self.km.client.models.generate_content(
                model=model,
                contents=[*parts, self.build_prompt(n)],
                config=types.GenerateContentConfig(**kwargs),
            )
            self.last_gen_s = time.time() - t_gen
            return parse_response(resp), getattr(resp, "usage_metadata", None)
        finally:
            for client, name in cleanup:
                try:
                    client.files.delete(name=name)
                except Exception:
                    pass

    def commit_page(self, n: int, result: PageTranslation, usage, model: str) -> None:
        # 先存術語庫再存頁面：中途當機時，重做該頁是安全的（術語不會重複覆蓋）
        added = self.add_terms(result.new_terms)
        if added:
            save_json(self.glossary_path, self.glossary)
            if len(self.glossary) > self.cfg.glossary_warn:
                log.info("💡 術語庫已有 %d 條；已改為只帶入本頁相關術語，成本影響有限", len(self.glossary))
        save_json(self.page_path(n), {
            "page": n,
            "translation": result.translation.strip(),
            "last_english_anchor": result.last_english_anchor.strip(),
            "new_terms": [t.model_dump() for t in result.new_terms],
            "model": model,
            "key_id": self.km.ids[self.km.index],
            "saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
        })
        self._record_failed(n, None)
        if usage is not None:
            self.tokens_in += getattr(usage, "prompt_token_count", 0) or 0
            self.tokens_out += getattr(usage, "candidates_token_count", 0) or 0

    # ---------- 單頁：重試 / 輪替 / 失敗記錄 ----------
    def handle_rate_limit(self, e: Exception, model: str) -> None:
        kind, retry = classify_rate_limit(e)
        km = self.km
        if kind == "daily":
            until = next_daily_reset_ts()
            when = datetime.datetime.fromtimestamp(until).strftime("%m/%d %H:%M")
            log.warning("❄️ %s 在「%s」觸發每日額度上限，冷卻至 %s（其他模型不受影響）", km.label, model, when)
        else:
            wait = min(max(retry or 60.0, 5.0), 300.0) + 1
            until = time.time() + wait
            log.warning("⚠️ %s 在「%s」觸發短期頻率限制，冷卻 %d 秒", km.label, model, int(wait))
        km.cooldown(until, model)
        # 先換成「對這個模型仍可用」的其他 Key；都沒有的話，下一輪 choose_model 會改用備援模型或等待
        old = km.index
        if km.use_ready(model) and km.index != old:
            log.info("🔄 已切換至 %s", km.label)

    def choose_model(self) -> Optional[str]:
        """挑出這次請求要用的模型（依優先順序，且要有可用的 Key）。全部都在冷卻時等待。"""
        r, km = self.router, self.km
        while not STOP.is_set() and self.alive():
            for pool in (r.candidates(), r.chain):  # 先排除「塞車中」的模型；都不行再不管塞車
                for m in pool:
                    if km.use_ready(m):
                        if m != self.active_model:
                            if self.active_model:
                                log.info("🔀 切換模型：%s → %s", self.active_model, m)
                            self.active_model = m
                        return m
            if not km.wait_any_ready(r.chain):
                return None
        return None

    def _on_congestion(self, model: str) -> bool:
        """記一次伺服器端錯誤；若因此判定塞車且有其他模型可用，回傳 True（呼叫端應立刻換模型重試）。"""
        r = self.router
        if r.record_congestion(model) and r.has_other(model):
            log.warning("🚦 模型 %s 連續伺服器錯誤，判定塞車，%d 分鐘內改用備援模型", model, r.recover // 60)
            return True
        return False

    def mark_failed(self, n: int, err: str) -> str:
        log.error("❌ 第 %d 頁失敗，已記錄並跳過：%s", n, err)
        self._record_failed(n, err)
        return "failed"

    def translate_page(self, n: int) -> str:
        """回傳 'ok' | 'failed' | 'stopped'"""
        cfg, km, r = self.cfg, self.km, self.router
        fails = rate_hits = switches = 0
        last_err = ""
        while True:
            if STOP.is_set() or not self.alive():
                return "stopped"
            model = self.choose_model()
            if model is None or not self.alive():
                return "stopped"
            km.wait_turn(cfg.min_interval)
            if STOP.is_set() or not self.alive():
                return "stopped"
            t0 = time.time()
            try:
                result, usage = self.call_model(n, model)
            except FatalError:
                raise
            except errors.ClientError as e:
                code = getattr(e, "code", None)
                last_err = f"{code}: {str(e)[:200]}"
                if code == 429:
                    rate_hits += 1
                    if rate_hits > cfg.max_rate_limit_retries:
                        return self.mark_failed(n, f"429 處理超過 {cfg.max_rate_limit_retries} 次")
                    self.handle_rate_limit(e, model)
                    continue
                if code in (401, 403) or (code == 400 and "api key" in str(e).lower()):
                    km.bad_key(last_err)
                    continue
                if code == 400 and "thinking" in str(e).lower() and model not in self.no_thinking:
                    self.no_thinking.add(model)
                    log.warning("⚠️ 模型 %s 不支援目前的思考強度設定，改為不指定思考強度後重試", model)
                    continue
                if code == 404:
                    if r.enabled and r.has_other(model):
                        log.warning("⚠️ 找不到模型 %s（404），1 小時內改用其他模型", model)
                        r.congested_until[model] = time.time() + 3600
                        continue
                    raise FatalError(f"找不到模型或資源 (404)，請檢查模型名稱（目前：{model}）：{e}")
                fails += 1
                log.warning("⚠️ 第 %d 頁用戶端錯誤 (%s)，第 %d/%d 次", n, last_err, fails, cfg.max_fails)
            except errors.ServerError as e:
                fails += 1
                last_err = f"{getattr(e, 'code', '5xx')}: {str(e)[:200]}"
                log.warning("⚠️ 伺服器錯誤 (%s)，模型 %s，耗時 %.0fs，第 %d/%d 次",
                            last_err, model, time.time() - t0, fails, cfg.max_fails)
                if self._on_congestion(model):
                    switches += 1
                    if switches >= max(len(r.chain), 2) * 2:
                        return self.mark_failed(n, f"各模型都持續塞車：{last_err}")
                    fails = 0  # 換了模型，重新計算這一頁的重試次數
                    continue
            except (EmptyResponse, ValueError) as e:  # ValueError 涵蓋 pydantic ValidationError
                fails += 1
                last_err = f"{type(e).__name__}: {str(e)[:200]}"
                log.warning("⚠️ 第 %d 頁輸出無效 (%s)，第 %d/%d 次", n, last_err, fails, cfg.max_fails)
            except Exception as e:  # 網路中斷、逾時等
                fails += 1
                last_err = f"{type(e).__name__}: {str(e)[:200]}"
                log.warning("⚠️ 第 %d 頁發生錯誤 (%s)，第 %d/%d 次", n, last_err, fails, cfg.max_fails)
                log.debug("traceback", exc_info=True)
                if "timeout" in type(e).__name__.lower() and self._on_congestion(model):  # 逾時也視為塞車徵兆
                    fails = 0
                    continue
            else:
                if not self.alive():  # 強制停止後才回來的結果：直接丟棄
                    return "stopped"
                r.record_ok(model)
                self.commit_page(n, result, usage, model)
                log.info("✅ 第 %d 頁完成（%s｜總 %.1fs｜上傳 %.1fs｜生成 %.1fs｜in=%s out=%s think=%s）", n, model,
                         time.time() - t0, self.last_upload_s, self.last_gen_s,
                         getattr(usage, "prompt_token_count", "?"), getattr(usage, "candidates_token_count", "?"),
                         getattr(usage, "thoughts_token_count", "?"))
                return "ok"

            if fails >= cfg.max_fails:
                return self.mark_failed(n, last_err)
            if interruptible_sleep(min(10 * 2 ** (fails - 1), 120)):
                return "stopped"

    # ---------- 整本流程 ----------
    def merge(self) -> int:
        return merge_pages(self.pages_dir, self.failed_path, self.output_md,
                           join=self.cfg.join_pages, markers=self.cfg.page_markers)

    def run(self) -> str:
        """回傳 'complete' | 'partial' | 'stopped' | 'error'"""
        cfg = self.cfg
        self.folder.mkdir(parents=True, exist_ok=True)
        self._prepare()
        self.reader = open_pdf(self.pdf_path, self.folder)
        if self.reader is None:
            return "error"
        self.total = len(self.reader.pages)
        if self.total == 0:
            log.error("[%s] PDF 沒有任何頁面", self.name)
            return "error"

        for n in sorted(cfg.redo):
            if self.page_path(n).exists():
                self.page_path(n).unlink()
                log.info("已清除第 %d 頁快取，將重新翻譯", n)

        start = max(1, cfg.start)
        end = cfg.end if 0 < cfg.end <= self.total else self.total
        log.info("📖 [%s] 共 %d 頁，本次範圍 %d~%d", self.name, self.total, start, end)
        if self.router.enabled:
            log.info("🔀 自動切換模型已啟用：%s", " → ".join(self.router.chain))

        consecutive = 0
        aborted = False
        try:
            for n in range(start, end + 1):
                if STOP.is_set() or not self.alive():
                    break
                if self.page_path(n).exists():
                    continue
                done = sum(1 for i in range(1, self.total + 1) if self.page_path(i).exists())
                log.info("⏳ [%s] 第 %d/%d 頁（已完成 %d）", self.name, n, self.total, done)
                status = self.translate_page(n)
                if status == "ok":
                    consecutive = 0
                elif status == "failed":
                    consecutive += 1
                    if consecutive >= cfg.max_consecutive_fails:
                        log.error("連續 %d 頁失敗，疑似系統性問題，中止本書。", consecutive)
                        aborted = True
                        break
                else:
                    break
        finally:
            if self.alive():
                count = self.merge()
                log.info("📝 已合併 %d 頁 → %s", count, self.output_md)

        all_done = all(self.page_path(i).exists() for i in range(1, self.total + 1))
        if STOP.is_set() or not self.alive():
            return "stopped"
        if all_done:
            log.info("🎉 [%s] 全文翻譯完成！", self.name)
            try:
                if cfg.move_pdf and self.pdf_path.resolve().parent != self.folder.resolve():
                    shutil.move(str(self.pdf_path), str(self.folder / self.pdf_path.name))
                    log.info("原始 PDF 已移入 %s/", self.folder)
                (self.folder / "_unlocked.pdf").unlink(missing_ok=True)
            except Exception as e:
                log.warning("整理檔案時發生問題：%s", e)
            return "complete"
        if aborted:
            return "error"
        log.info("[%s] 尚有頁面未完成（指定範圍或有失敗頁），PDF 原檔保留在原位置。", self.name)
        return "partial"


# ============================================================
# 進入點
# ============================================================
def load_api_keys() -> List[str]:
    raw = os.environ.get("GEMINI_API_KEYS", "")
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        log.error("找不到 API 金鑰。請設定環境變數 GEMINI_API_KEYS（多組以半形逗號分隔），例如：")
        log.error('    export GEMINI_API_KEYS="key1,key2,key3"')
        sys.exit(1)
    return keys


def setup_logging() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    log.setLevel(logging.DEBUG)
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    logfile = logging.FileHandler("translator.log", encoding="utf-8")
    logfile.setLevel(logging.DEBUG)
    logfile.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(console)
    log.addHandler(logfile)


def _sigint(sig, frame):
    if not STOP.is_set():
        print()
        log.warning("🛑 收到中止訊號，完成當前動作後安全存檔（再按一次 Ctrl+C 強制結束）")
        STOP.set()
    else:
        print("\n強制結束。")
        sys.exit(130)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="TRPG 規則書 PDF 逐頁翻譯工具 v2")
    p.add_argument("--pdf", nargs="*", help="指定 PDF 檔（可用萬用字元）；預設為目前資料夾所有 *.pdf")
    p.add_argument("--start", type=int, default=1, help="起始頁（需搭配單一 PDF）")
    p.add_argument("--end", type=int, default=0, help="結束頁，0 = 到最後一頁（需搭配單一 PDF）")
    p.add_argument("--redo", default="", help="強制重翻的頁碼，例如 '5,8-10'（需搭配單一 PDF）")
    p.add_argument("--start-key", type=int, default=1, help="起始 API Key 編號（從 1 起算）")
    p.add_argument("--model", default=os.environ.get("GEMINI_MODEL_ID", Config.model))
    p.add_argument("--thinking", default=Config.thinking, help="thinking level；填 none 則不設定")
    p.add_argument("--min-interval", type=float, default=Config.min_interval, help="兩次請求最小間隔秒數")
    p.add_argument("--timeout", type=int, default=Config.timeout, help="單次請求逾時秒數（建議 ≥300；太短會把慢但成功的請求砍成 504）")
    p.add_argument("--transport", choices=["files", "inline"], default=Config.transport,
                   help="PDF 傳送方式：files = 先上傳 Files API（同第一版，預設）；inline = 內嵌於請求")
    p.add_argument("--auto-switch", action="store_true", help="伺服器塞車或額度用完時，自動改用備援模型")
    p.add_argument("--fallback-models", default="", help="備援模型（依優先順序，逗號分隔）")
    p.add_argument("--congest-threshold", type=int, default=Config.congest_threshold,
                   help="同一模型連續幾次伺服器錯誤就判定塞車")
    p.add_argument("--recover-minutes", type=int, default=Config.recover_minutes,
                   help="判定塞車後，隔幾分鐘再回頭試優先順序較前的模型")
    p.add_argument("--max-fails", type=int, default=Config.max_fails)
    p.add_argument("--max-consecutive-fails", type=int, default=Config.max_consecutive_fails)
    p.add_argument("--with-prev-page", action="store_true", help="額外附上上一頁 PDF 當上下文（約 2 倍成本）")
    p.add_argument("--page-markers", action="store_true", help="合併檔每頁前加 <!-- p.N --> 註解（會停用跨頁段落接合）")
    p.add_argument("--no-join", action="store_true", help="合併時不嘗試接回被分頁切斷的段落")
    p.add_argument("--merge-only", action="store_true", help="不呼叫 API，只用 pages/ 快取重新產生 Markdown")
    return p.parse_args(argv)


def resolve_pdfs(patterns: Optional[List[str]]) -> List[Path]:
    if not patterns:
        patterns = ["*.pdf"]
    found: List[str] = []
    for pat in patterns:
        matches = glob.glob(pat)
        found.extend(matches if matches else ([pat] if os.path.isfile(pat) else []))
    seen, result = set(), []
    for f in found:
        rp = os.path.abspath(f)
        if rp not in seen:
            seen.add(rp)
            result.append(Path(f))
    return result


def main(argv=None) -> int:
    args = parse_args(argv)
    setup_logging()
    signal.signal(signal.SIGINT, _sigint)

    cfg = Config(
        model=args.model, thinking=args.thinking, start=args.start, end=args.end,
        start_key=args.start_key, max_fails=args.max_fails,
        max_consecutive_fails=args.max_consecutive_fails, min_interval=args.min_interval,
        timeout=args.timeout, transport=args.transport, with_prev_page=args.with_prev_page,
        auto_switch=args.auto_switch, congest_threshold=args.congest_threshold,
        recover_minutes=args.recover_minutes,
        fallback_models=[m.strip() for m in args.fallback_models.split(",") if m.strip()],
        page_markers=args.page_markers, join_pages=not args.no_join,
        redo=parse_page_spec(args.redo) if args.redo else set(),
    )

    # ---- 只重組 Markdown ----
    if args.merge_only:
        wanted = {Path(p).stem for p in (args.pdf or [])}
        for d in sorted(Path(".").iterdir()):
            if d.is_dir() and (d / "pages").is_dir() and (not wanted or d.name in wanted):
                n = merge_pages(d / "pages", d / "failed_pages.json", d / f"{d.name}_翻譯.md",
                                join=cfg.join_pages, markers=cfg.page_markers)
                log.info("📝 [%s] 已合併 %d 頁", d.name, n)
        return 0

    pdfs = resolve_pdfs(args.pdf)
    if not pdfs:
        log.info("📭 找不到任何 PDF 檔案。")
        return 0
    if (cfg.start > 1 or cfg.end > 0 or cfg.redo) and len(pdfs) != 1:
        log.error("--start / --end / --redo 只能搭配單一 PDF，請用 --pdf 指定檔案（目前有 %d 個）。", len(pdfs))
        return 2

    km = KeyManager(load_api_keys(), cfg.start_key - 1, cfg.timeout)
    log.info("✨ 模型 %s 啟動，共 %d 把 Key（從 %s 開始），%d 個 PDF", cfg.model, len(km.keys), km.label, len(pdfs))

    summary: List[Tuple[str, str]] = []
    exit_code = 0
    for pdf in pdfs:
        if STOP.is_set():
            break
        job = BookJob(pdf, cfg, km)
        try:
            status = job.run()
        except FatalError as e:
            log.error("💥 致命錯誤，中止：%s", e)
            exit_code = 1
            break
        summary.append((job.name, status))
        if job.tokens_in or job.tokens_out:
            log.info("📊 [%s] 本次 token 用量：in=%d out=%d", job.name, job.tokens_in, job.tokens_out)
        failed = load_json(job.failed_path, {}).get("pages", {})
        if failed:
            log.warning("⚠️ [%s] 失敗頁：%s（重新執行即會自動重試）", job.name, ", ".join(sorted(failed, key=int)))

    log.info("🏁 結束。結果：%s", "；".join(f"{n}={s}" for n, s in summary) or "（無）")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
