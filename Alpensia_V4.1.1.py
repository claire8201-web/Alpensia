import json
import os
import sys
import ctypes
import base64
import threading
import time
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, date, timezone
from typing import List, Optional, Tuple
from email.utils import parsedate_to_datetime
import urllib.request
import tkinter as tk
from tkinter import ttk, messagebox
from ctypes import wintypes

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
    # PyInstaller onedir: datas are typically under "<exe_dir>/_internal".
    RESOURCE_DIR = os.path.join(APP_DIR, "_internal")
    if not os.path.isdir(RESOURCE_DIR):
        RESOURCE_DIR = APP_DIR
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
    RESOURCE_DIR = APP_DIR

CONFIG_PATH = os.path.join(APP_DIR, "config.json")
CREDENTIALS_PATH = os.path.join(APP_DIR, "credentials.enc.json")
APP_BG = "#f0f0f0"
WHITE = "#ffffff"
LOGO_FILENAME = "alpensia_logo.png"
ICON_FILENAME = "alpensia_logo.ico"
APP_ID = "armatech.alpensia.v411"
APP_VERSION = "4.1.2"
MAX_SAVED_ACCOUNTS = 20
DPAPI_ENTROPY = b"Alpensia_V4.1.1_Credentials"
DEBUG_CAPTURE_ENABLED = False
TIME_ROW_DEBUG_ENABLED = False
PERF_LOG_ENABLED = True


def _set_windows_app_id():
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception as e:
        print(f"[ICON][WARN] SetCurrentProcessExplicitAppUserModelID failed: {e}")


def _resource_candidates(filename: str):
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(os.path.join(meipass, filename))
    candidates.append(os.path.join(RESOURCE_DIR, filename))
    candidates.append(os.path.join(APP_DIR, filename))
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), filename))
    return candidates


def _first_existing_resource(filename: str) -> Optional[str]:
    for p in _resource_candidates(filename):
        if os.path.exists(p):
            return p
    return None


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _blob_from_bytes(data: bytes):
    if not data:
        return _DataBlob(0, None), None
    buf = ctypes.create_string_buffer(data, len(data))
    blob = _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    return blob, buf


def _dpapi_encrypt(plain_text: str) -> str:
    if os.name != "nt":
        raise RuntimeError("DPAPI is available only on Windows.")
    data_blob, data_buf = _blob_from_bytes(plain_text.encode("utf-8"))
    entropy_blob, entropy_buf = _blob_from_bytes(DPAPI_ENTROPY)
    out_blob = _DataBlob()

    try:
        ok = ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(data_blob),
            "AlpensiaCredentials",
            ctypes.byref(entropy_blob),
            None,
            None,
            0,
            ctypes.byref(out_blob),
        )
        if not ok:
            raise ctypes.WinError()
        enc_bytes = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return base64.b64encode(enc_bytes).decode("ascii")
    finally:
        if out_blob.pbData:
            ctypes.windll.kernel32.LocalFree(out_blob.pbData)
        _ = data_buf, entropy_buf


def _dpapi_decrypt(cipher_text_b64: str) -> str:
    if os.name != "nt":
        raise RuntimeError("DPAPI is available only on Windows.")
    raw = base64.b64decode(cipher_text_b64.encode("ascii"))
    data_blob, data_buf = _blob_from_bytes(raw)
    entropy_blob, entropy_buf = _blob_from_bytes(DPAPI_ENTROPY)
    out_blob = _DataBlob()
    desc = wintypes.LPWSTR()

    try:
        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(data_blob),
            ctypes.byref(desc),
            ctypes.byref(entropy_blob),
            None,
            None,
            0,
            ctypes.byref(out_blob),
        )
        if not ok:
            raise ctypes.WinError()
        dec_bytes = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return dec_bytes.decode("utf-8")
    finally:
        if out_blob.pbData:
            ctypes.windll.kernel32.LocalFree(out_blob.pbData)
        _ = data_buf, entropy_buf


def yyyymm_now() -> str:
    return datetime.now().strftime("%Y%m")


def safe_int(x: str, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


def parse_hhmm(s: str) -> Optional[int]:
    """'HH:MM' -> minutes from 00:00"""
    try:
        hh, mm = s.split(":")
        return int(hh) * 60 + int(mm)
    except Exception:
        return None


def fmt_hhmm(total_minutes: int) -> str:
    total_minutes = max(0, min(23 * 60 + 59, total_minutes))
    hh = total_minutes // 60
    mm = total_minutes % 60
    return f"{hh:02d}:{mm:02d}"


def build_time_candidates(target_hhmm: str, step_min: int = 10, max_diff: int = 120) -> List[str]:
    """
    캐슬렉스처럼: 목표시간 → +10 → -10 → +20 → -20 ... 최대 ±max_diff
    (이번 버전에서는 주로 fallback 용도로만 사용)
    """
    base = parse_hhmm(target_hhmm)
    if base is None:
        return [target_hhmm]

    out = [fmt_hhmm(base)]
    for d in range(step_min, max_diff + 1, step_min):
        out.append(fmt_hhmm(base + d))
        out.append(fmt_hhmm(base - d))

    seen = set()
    uniq = []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


class UiLogger:
    def __init__(self, text_widget: tk.Text):
        self.text = text_widget
        self.lock = threading.Lock()

    def log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        with self.lock:
            self.text.after(0, self._append, line)

    def _append(self, line: str):
        self.text.insert("end", line)
        self.text.see("end")


class DatePicker(tk.Toplevel):
    """
    간단한 달력 팝업.
    - initial_ymd: 'YYYY-MM-DD'
    - on_pick: 선택된 'YYYY-MM-DD' 콜백
    """
    def __init__(self, master, initial_ymd: str, on_pick):
        super().__init__(master)
        self.title("날짜 선택")
        self.resizable(False, False)
        self.configure(bg=APP_BG)
        self.on_pick = on_pick

        self.transient(master)
        self.grab_set()

        try:
            y, m, d = [int(x) for x in initial_ymd.split("-")]
            self.cur = date(y, m, d)
        except Exception:
            self.cur = date.today()

        self._build()

        self.update_idletasks()
        x = master.winfo_rootx() + 350
        y = master.winfo_rooty() + 250
        self.geometry(f"+{x}+{y}")

    def _build(self):
        top = tk.Frame(self, bg=APP_BG)
        top.pack(fill="x", padx=10, pady=8)

        btn_prev = ttk.Button(top, text="◀", width=4, command=self._prev_month)
        btn_prev.pack(side="left")

        self.lbl = tk.Label(top, text="", bg=APP_BG, font=("맑은 고딕", 11, "bold"))
        self.lbl.pack(side="left", expand=True)

        btn_next = ttk.Button(top, text="▶", width=4, command=self._next_month)
        btn_next.pack(side="right")

        self.grid_frame = tk.Frame(self, bg=APP_BG)
        self.grid_frame.pack(padx=10, pady=(0, 10))

        self._render()

    def _render(self):
        for w in self.grid_frame.winfo_children():
            w.destroy()

        y = self.cur.year
        m = self.cur.month
        self.lbl.config(text=f"{y:04d}-{m:02d}")

        days = ["일", "월", "화", "수", "목", "금", "토"]
        for c, name in enumerate(days):
            tk.Label(self.grid_frame, text=name, bg=APP_BG, width=4, font=("맑은 고딕", 9, "bold")).grid(
                row=0, column=c, padx=2, pady=2
            )

        first = date(y, m, 1)
        start_col = (first.weekday() + 1) % 7
        if m == 12:
            last = date(y + 1, 1, 1) - timedelta(days=1)
        else:
            last = date(y, m + 1, 1) - timedelta(days=1)

        r = 1
        c = start_col
        today = date.today()

        for d in range(1, last.day + 1):
            cur_day = date(y, m, d)

            def make_cmd(day=cur_day):
                ymd = day.strftime("%Y-%m-%d")
                self.on_pick(ymd)
                self.destroy()

            btn = ttk.Button(self.grid_frame, text=f"{d:02d}", width=4, command=make_cmd)
            btn.grid(row=r, column=c, padx=2, pady=2)

            if cur_day == today:
                try:
                    btn.configure(style="Today.TButton")
                except Exception:
                    pass

            c += 1
            if c >= 7:
                c = 0
                r += 1

    def _prev_month(self):
        y = self.cur.year
        m = self.cur.month
        if m == 1:
            self.cur = date(y - 1, 12, 1)
        else:
            self.cur = date(y, m - 1, 1)
        self._render()

    def _next_month(self):
        y = self.cur.year
        m = self.cur.month
        if m == 12:
            self.cur = date(y + 1, 1, 1)
        else:
            self.cur = date(y, m + 1, 1)
        self._render()


@dataclass
class PriorityItem:
    enabled: bool
    ymd: str
    hhmm: str


class AlpensiaBot:
    BASE = "https://www.alpensia.com"

    def __init__(self, logger: UiLogger, stop_event: threading.Event, debug_dir: str, headless: bool = False):
        self.logger = logger
        self.stop_event = stop_event
        self.headless = headless
        self.driver = None
        self.wait = None
        self.debug_dir = debug_dir

    # --------------------------
    # 공통 유틸
    # --------------------------
    def _dbg(self, msg: str):
        self.logger.log(msg)

    def _dbg_time_row(self, msg: str):
        if TIME_ROW_DEBUG_ENABLED:
            self.logger.log(msg)

    def _perf(self, msg: str):
        if PERF_LOG_ENABLED:
            self.logger.log(msg)

    def _check_stop(self):
        if self.stop_event.is_set():
            raise RuntimeError("중단 요청")

    def _new_driver(self):
        opts = ChromeOptions()
        opts.add_argument("--disable-gpu")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        opts.add_argument("--start-maximized")
        opts.add_experimental_option("prefs", {
            "credentials_enable_service": False,
            "profile.password_manager_enabled": False
        })

        if self.headless:
            opts.add_argument("--headless=new")

        self.driver = webdriver.Chrome(options=opts)
        #self.driver.set_window_rect(x=0, y=0, width=1280, height=900)
        self.wait = WebDriverWait(self.driver, 15)
    
    def _get_server_now(self) -> datetime:
        # 서버시간(Date 헤더) 시도 → 실패하면 로컬시간(KST) 사용
        kst = timezone(timedelta(hours=9))

        try:
            req = urllib.request.Request(self.BASE, method="HEAD")
            with urllib.request.urlopen(req, timeout=5) as resp:
                date_hdr = resp.headers.get("Date")
            if not date_hdr:
                return datetime.now(kst)

            dt_utc = parsedate_to_datetime(date_hdr)  # 보통 UTC
            return dt_utc.astimezone(kst)

        except Exception:
            # ✅ 여기로 떨어지면 (SSL 에러/차단/타임아웃 등) 로컬시간으로 대체
            return datetime.now(kst)
    
    def _wait_until_server_hhmm(self, target_hhmm: str = "09:00", poll_sec: float = 0.5):
        """
        서버시간 기준으로 target_hhmm까지 대기
        - 달력 화면까지 들어온 뒤 호출하는 용도
        """
        kst = timezone(timedelta(hours=9))
        now = self._get_server_now()
        hh, mm = [int(x) for x in target_hhmm.split(":")]
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)

        # 이미 지났으면 바로 진행
        if now >= target:
            self.logger.log(f"[WAIT] 서버시간 {target_hhmm} 이미 지남 → 즉시 진행 (server={now.strftime('%H:%M:%S')})")
            return

        self.logger.log(f"[WAIT] 서버시간 기준 {target_hhmm}까지 대기 시작 (server={now.strftime('%H:%M:%S')})")
        last_logged = None
        while True:
            self._check_stop()
            now = self._get_server_now()

            remain = (target - now).total_seconds()
            if remain <= 0:
                self.logger.log(f"[WAIT][GO] 서버시간 도달: {now.strftime('%H:%M:%S')}")
                return

            remain_int = int(remain)

            # 60초 초과 -> 60초마다 1번
            if remain_int > 60:
                if remain_int % 60 == 0 and last_logged != remain_int:
                    self.logger.log(f"[WAIT] 남은시간 약 {remain_int}초 (server={now.strftime('%H:%M:%S')})")
                    last_logged = remain_int

            # 60초 이내 -> 10초마다 1번
            elif remain_int > 10:
                if remain_int % 10 == 0 and last_logged != remain_int:
                    self.logger.log(f"[WAIT] 남은시간 약 {remain_int}초 (server={now.strftime('%H:%M:%S')})")
                    last_logged = remain_int

            # 10초 이내 -> 1초마다 1번
            else:
                if last_logged != remain_int:
                    self.logger.log(f"[WAIT] 남은시간 약 {remain_int}초 (server={now.strftime('%H:%M:%S')})")
                    last_logged = remain_int


            # 가까워질수록 촘촘히
            time.sleep(0.2 if remain <= 3 else poll_sec)

    def close(self):
        try:
            if self.driver:
                self.driver.quit()
        except Exception:
            pass

    def _save_debug(self, prefix: str):
        if not DEBUG_CAPTURE_ENABLED or not self.debug_dir:
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        png = os.path.join(self.debug_dir, f"{prefix}_{ts}.png")
        html = os.path.join(self.debug_dir, f"{prefix}_{ts}.html")
        try:
            self.driver.save_screenshot(png)
            with open(html, "w", encoding="utf-8") as f:
                f.write(self.driver.page_source)
            self.logger.log(f"[DEBUG] 캡처 저장: {png}")
            self.logger.log(f"[DEBUG] HTML 저장: {html}")
            self.logger.log(f"[DEBUG] 당시 URL: {self.driver.current_url}")
        except Exception:
            pass

    # --------------------------
    # ✅ 시간 선택 모듈 (완전 재작성)
    # --------------------------
    def _wait_for_selbookg_radios(self, timeout=8) -> bool:
        """시간 라디오(selBookg/selBook)가 나타날 때까지 대기"""
        try:
            WebDriverWait(self.driver, timeout).until(
                lambda d: (
                    len(d.find_elements(By.CSS_SELECTOR, "input[name='selBookg']")) > 0
                    or len(d.find_elements(By.CSS_SELECTOR, "input[name='selBook']")) > 0
                )
            )
            return True
        except Exception:
            return False

    def _parse_booktime_to_minutes(self, booktime: str) -> Optional[int]:
        """'0937' -> minutes"""
        if not booktime:
            return None
        bt = booktime.strip()
        if not re.fullmatch(r"\d{4}", bt):
            return None
        hh = int(bt[:2])
        mm = int(bt[2:])
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return None
        return hh * 60 + mm

    def _hhmm_to_minutes(self, hhmm: str) -> Optional[int]:
        """
        '10:00' / '1000' / '10' 지원
        """
        s = (hhmm or "").strip()

        m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
        if m:
            hh, mm = int(m.group(1)), int(m.group(2))
            return hh * 60 + mm

        m = re.fullmatch(r"(\d{2})(\d{2})", s)
        if m:
            hh, mm = int(m.group(1)), int(m.group(2))
            return hh * 60 + mm

        m = re.fullmatch(r"\d{1,2}", s)
        if m:
            hh = int(s)
            return hh * 60

        return None

    def _collect_time_radios(self) -> List[dict]:
        """
        selBookg(우선) → 없으면 selBook 수집
        시간은 booktime 속성이 아니라,
        같은 행(tr) 안의 HH:MM 텍스트에서 추출
        """
        rows = self.driver.execute_script(
            """
            const pick = (name) => Array.from(document.querySelectorAll(`input[name="${name}"]`));
            let radios = pick('selBookg');
            let selectorUsed = 'selBookg';
            if (!radios.length) {
                radios = pick('selBook');
                selectorUsed = 'selBook';
            }

            return {
                selector_used: selectorUsed,
                rows: radios.map((r, idx) => {
                    const tr = r.closest('tr');
                    const rowText = tr ? (tr.innerText || tr.textContent || '') : '';
                    return {
                        idx: idx + 1,
                        id: (r.id || '').trim(),
                        disabled: !!r.disabled,
                        row_text: rowText.trim(),
                    };
                })
            };
            """
        ) or {}

        selector_used = rows.get("selector_used", "selBookg")
        raw_rows = rows.get("rows", [])
        self._dbg(f"[TIME] radios selector={selector_used}, found={len(raw_rows)}")

        items = []

        for row in raw_rows:
            i = row.get("idx", len(items) + 1)
            try:
                row_text = (row.get("row_text") or "").strip()
                rid = (row.get("id") or "").strip()
                disabled = bool(row.get("disabled"))

                m = re.search(r"(\d{2}:\d{2})", row_text)
                minutes = None
                booktime = ""

                if m:
                    booktime = m.group(1).replace(":", "")
                    minutes = self._hhmm_to_minutes(m.group(1))

                self._dbg_time_row(
                    f"[TIME][{i}] row='{row_text}' booktime={booktime} "
                    f"minutes={minutes} disabled={disabled} id={rid}"
                )

                items.append({
                    "idx": i,
                    "selector_name": selector_used,
                    "booktime": booktime,
                    "minutes": minutes,
                    "disabled": disabled,
                    "id": rid,
                })

            except Exception as e:
                self._dbg_time_row(f"[TIME][{i}] read fail: {e}")

        parsed = [x for x in items if x["minutes"] is not None]
        disabled_count = sum(1 for x in parsed if x["disabled"])
        self._dbg(
            f"[TIME] parsed items={len(parsed)} "
            f"(enabled={len(parsed) - disabled_count}, disabled={disabled_count})"
        )
        return parsed

    def _resolve_time_radio_element(self, item: dict):
        rid = (item.get("id") or "").strip()
        if rid:
            try:
                return self.driver.find_element(By.ID, rid)
            except Exception:
                pass

        selector_name = (item.get("selector_name") or "selBookg").strip() or "selBookg"
        idx = max(1, int(item.get("idx", 1)))
        radios = self.driver.find_elements(By.CSS_SELECTOR, f"input[name='{selector_name}']")
        if 1 <= idx <= len(radios):
            return radios[idx - 1]
        raise RuntimeError(f"time radio not found: selector={selector_name} idx={idx} id={rid}")


    def _build_time_candidates_from_page(self, target_hhmm: str, max_candidates: int = 12) -> List[dict]:
        """
        목표시간 기준 거리순 후보 생성
        """
        started = time.perf_counter()
        target_min = self._hhmm_to_minutes(target_hhmm)
        if target_min is None:
            self._dbg(f"[TIME][FAIL] invalid target time: {target_hhmm}")
            return []

        wait_started = time.perf_counter()
        if not self._wait_for_selbookg_radios(timeout=8):
            self._dbg("[TIME][FAIL] radios not appeared")
            return []
        self._perf(f"[PERF] wait radios: {time.perf_counter() - wait_started:.3f}s")

        collect_started = time.perf_counter()
        items = self._collect_time_radios()
        self._perf(f"[PERF] collect times: {time.perf_counter() - collect_started:.3f}s")
        if not items:
            self._dbg("[TIME][FAIL] no parsed radios")
            return []

        sort_started = time.perf_counter()
        for it in items:
            it["dist"] = abs(it["minutes"] - target_min)

        items.sort(key=lambda x: (x["dist"], x["minutes"]))  # 거리 우선 + 빠른시간 우선
        items = items[:max_candidates]
        self._perf(f"[PERF] score/sort candidates: {time.perf_counter() - sort_started:.3f}s")

        self._dbg("[TIME] candidates(sorted): " + ", ".join([f"{x['booktime']}(d={x['dist']})" for x in items]))
        self._perf(f"[PERF] build candidates total: {time.perf_counter() - started:.3f}s")
        return items

    def _click_time_radio_robust(self, item: dict) -> bool:
        """
        1) label 클릭(있으면) -> 2) JS click -> 3) checked 강제 -> 확인
        """
        if item.get("disabled"):
            self._dbg(f"[CLICK][SKIP] disabled: {item.get('booktime')}")
            return False

        try:
            r = self._resolve_time_radio_element(item)

            # 1) label 클릭
            rid = item.get("id", "")
            if rid:
                labels = self.driver.find_elements(By.CSS_SELECTOR, f"label[for='{rid}']")
                if labels:
                    try:
                        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", labels[0])
                        labels[0].click()
                        time.sleep(0.05)
                    except Exception:
                        pass

            # 2) JS click
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", r)
            self.driver.execute_script("arguments[0].click();", r)
            time.sleep(0.05)

            # 3) 체크 확인/강제
            checked = False
            try:
                checked = r.is_selected()
            except Exception:
                checked = False

            if not checked:
                try:
                    self.driver.execute_script("arguments[0].checked = true;", r)
                    time.sleep(0.02)
                except Exception:
                    pass

            # 최종 확인
            try:
                checked2 = r.is_selected()
            except Exception:
                checked2 = (r.get_attribute("checked") is not None)

            self._dbg(f"[CLICK] {item.get('booktime')} checked={checked2}")
            return bool(checked2)

        except Exception as e:
            self._dbg(f"[CLICK][FAIL] {item.get('booktime')} err={e}")
            return False

    def select_time_by_target(self, target_hhmm: str, max_candidates: int = 12) -> bool:
        """
        목표시간 기준 후보 -> 순서대로 클릭
        """
        started = time.perf_counter()
        candidates = self._build_time_candidates_from_page(target_hhmm, max_candidates=max_candidates)
        if not candidates:
            return False

        self._dbg("[TIME] try click candidates...")
        for k, item in enumerate(candidates, start=1):
            click_started = time.perf_counter()
            self._dbg(f"[TIME] try#{k}: {item.get('booktime')} dist={item.get('dist')}")
            ok = self._click_time_radio_robust(item)
            self._perf(f"[PERF] click candidate #{k} {item.get('booktime')}: {time.perf_counter() - click_started:.3f}s")
            if ok:
                self._dbg(f"[TIME][OK] selected: {item.get('booktime')}")
                self._perf(f"[PERF] select_time_by_target total: {time.perf_counter() - started:.3f}s")
                return True

        self._dbg("[TIME][MISS] no candidate clickable/selected")
        self._perf(f"[PERF] select_time_by_target total: {time.perf_counter() - started:.3f}s")
        return False

    # --------------------------
    # 메인 플로우
    # --------------------------
    def run(self, user_id: str, password: str, course_key: str,
            priorities: List[PriorityItem], test_mode: bool, wait_open: bool = True,
            live_safety_block_submit: bool = True):
        """
        test_mode:
          True  -> 예약 직전(예약 버튼 클릭 전)까지만
          False -> 실제 예약까지
        """
        try:
            self.logger.log("=== 알펜시아 자동화 시작 ===")
            self._new_driver()

            self._check_stop()
            self.login(user_id, password)

            self._check_stop()
            self.go_to_golf_calendar(course_key)

            self._check_stop()
            if not self.check_calendar_loaded():
                self.logger.log("[ERROR] 달력 로딩 확인 실패")
                self._save_debug("calendar_not_loaded")
                return
            self.logger.log("[OK] 달력 로딩 확인 완료")

        except RuntimeError as e:
            self.logger.log(f"[STOP] {str(e)}")
            return
        except Exception as e:
            self.logger.log(f"[ERROR] {type(e).__name__}: {e}")
            self._save_debug("fatal_error")
            return

        if (not test_mode) and wait_open:
            self._wait_until_server_hhmm("09:00")
            self.logger.log("[WAIT][POST] 09:00 도달 후 달력 새로고침/확인")
            try:
                self.driver.refresh()
                time.sleep(0.5)
            except Exception:
                pass

            if not self.check_calendar_loaded():
                self.logger.log("[ERROR] 09:00 이후 달력 재로딩 실패")
                self._save_debug("calendar_reload_after_wait_fail")
                return
        elif not test_mode:
            self.logger.log("[WAIT] 서버시간 대기 OFF → 즉시 진행(리허설)")

        enabled = [p for p in priorities if p.enabled]
        if not enabled:
            self.logger.log("[WARN] 활성화된 예약 우선순위가 없습니다.")
            return

        if test_mode:
            self.logger.log("[TEST] 테스트 모드: 날짜 → 시간 선택 → 규약 동의 → '예약 버튼 클릭 직전'까지 진행합니다.")
        else:
            if live_safety_block_submit:
                self.logger.log("[LIVE][SAFE] 실제 예약 모드 안전 점검: 예약 버튼 클릭은 차단하고 예약 직전까지 점검합니다.")
            else:
                self.logger.log("[LIVE] 실제 예약 모드: 체크된 모든 순위를 '각각' 예약 시도합니다. (다건 예약)")

        success_count = 0
        fail_list = []

        for idx, pri in enumerate(enabled, start=1):
            self._check_stop()
            self.logger.log(f"[TRY] {idx}순위 시도: {pri.ymd} {pri.hhmm}")

            if not self.select_date(pri.ymd):
                self.logger.log("[WARN] 날짜 선택 실패 → 다음 순위로")
                self._save_debug(f"date_click_fail_{idx}")
                fail_list.append((idx, pri.ymd, pri.hhmm, "date_click_fail"))
                continue

            stop_before_submit = bool(test_mode or live_safety_block_submit)
            booked = self.try_book_with_time_candidates(
                ymd=pri.ymd,
                target_time=pri.hhmm,
                stop_before_submit=stop_before_submit,
            )

            if test_mode:
                self.logger.log(f"[TEST] {idx}순위: 예약 직전까지 완료 → 다음 순위로 진행")
                self.go_to_golf_calendar(course_key)
                if not self.check_calendar_loaded():
                    self.logger.log("[WARN] 테스트모드: 달력 복귀 실패(다음 순위 진행 불가) → 캡처")
                    self._save_debug(f"calendar_reload_fail_test_{idx}")
                    return
                continue

            if live_safety_block_submit:
                if booked:
                    success_count += 1
                    self.logger.log(f"[SAFE] {idx}순위: 예약 직전까지 점검 완료 ({success_count}/{len(enabled)})")
                else:
                    self.logger.log(f"[SAFE][WARN] {idx}순위: 예약 직전 점검 실패")
                    fail_list.append((idx, pri.ymd, pri.hhmm, "safe_dryrun_fail"))

                self.go_to_golf_calendar(course_key)
                if not self.check_calendar_loaded():
                    self.logger.log("[WARN] 안전 점검 모드 달력 복귀 실패(다음 순위 진행 불가) -> 캡처")
                    self._save_debug(f"calendar_reload_fail_safe_{idx}")
                    return
                continue

            if booked:
                success_count += 1
                self.logger.log(f"[OK] {idx}순위 예약 성공 ({success_count}/{len(enabled)})")
                self.go_to_golf_calendar(course_key)
                if not self.check_calendar_loaded():
                    self.logger.log("[WARN] 달력 복귀 후 로딩 실패(다음 순위 진행 불가 가능) → 캡처")
                    self._save_debug(f"calendar_reload_fail_{idx}")
                continue

            self.logger.log(f"[INFO] {idx}순위 예약 실패/매진 → 다음 순위로 진행")
            fail_list.append((idx, pri.ymd, pri.hhmm, "book_fail"))

        if test_mode:
            self.logger.log("=== 테스트 종료(모든 체크된 순위에 대해 예약 직전까지 수행) ===")
            return

        if live_safety_block_submit:
            if success_count == len(enabled):
                self.logger.log("[SAFE][DONE] 체크된 모든 순위 점검 완료(실제 예약 클릭 차단됨)")
            else:
                self.logger.log("[SAFE][DONE] 일부 순위 점검 실패")
                for (i, ymd, hhmm, reason) in fail_list:
                    self.logger.log(f" - 점검 실패: {i}순위 {ymd} {hhmm} ({reason})")
            return

        if success_count == len(enabled):
            self.logger.log("[SUCCESS] 체크된 모든 순위 예약 성공!")
        else:
            self.logger.log("[FAIL] 일부 순위 예약 실패")
            for (i, ymd, hhmm, reason) in fail_list:
                self.logger.log(f" - 실패: {i}순위 {ymd} {hhmm} ({reason})")
    def login(self, user_id: str, password: str):
        self.logger.log("[INFO] 로그인 페이지 이동")
        self.driver.get(f"{self.BASE}/login.do")

        self.wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(0.2)

        try:
            if self.driver.find_elements(By.CSS_SELECTOR, "a[href='/logout.do']"):
                self.logger.log("[OK] 이미 로그인 상태(로그아웃 링크 확인) → 스킵")
                return
        except Exception:
            pass

        self.logger.log("[INFO] 아이디/비밀번호 입력")

        id_input = WebDriverWait(self.driver, 20).until(
            EC.element_to_be_clickable((By.ID, "emplyrId"))
        )
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", id_input)
        id_input.clear()
        id_input.click()
        id_input.send_keys(user_id)

        pw_input = WebDriverWait(self.driver, 20).until(
            EC.element_to_be_clickable((By.ID, "password"))
        )
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", pw_input)
        pw_input.clear()
        pw_input.click()
        pw_input.send_keys(password)

        btn = WebDriverWait(self.driver, 20).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "button[type='submit']"))
        )
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)

        self.logger.log("[INFO] 로그인 버튼 클릭")
        btn.click()

        def logged_in(driver):
            try:
                if driver.find_elements(By.CSS_SELECTOR, "a[href='/logout.do']"):
                    return True
                if "main.do" in driver.current_url:
                    return True
            except Exception:
                return False
            return False

        WebDriverWait(self.driver, 15).until(logged_in)
        self.logger.log("[OK] 로그인 성공")

    def go_to_golf_calendar(self, course_key: str):
        yyyymm = yyyymm_now()
        self.logger.log(f"[INFO] 골프장 선택: {'알펜시아 700 G.C' if course_key=='700' else '알펜시아 C.C'}")
        self.logger.log(f"[INFO] 예약 페이지 이동 (searchYYMM={yyyymm})")

        if course_key == "700":
            candidates = [
                f"{self.BASE}/reservation/pgolf/golf.do?searchYYMM={yyyymm}",
                f"{self.BASE}/reservation/golf/golf.do?searchYYMM={yyyymm}",
            ]
        else:
            candidates = [
                f"{self.BASE}/reservation/pgolf/golfcc.do?searchYYMM={yyyymm}",
                f"{self.BASE}/reservation/golf/golfcc.do?searchYYMM={yyyymm}",
            ]

        last = None
        for u in candidates:
            self._check_stop()
            last = u
            self.driver.get(u)
            time.sleep(0.5)
            if self._looks_like_golf_page():
                self.logger.log(f"[OK] 예약 페이지 진입: {u}")
                return

        self.logger.log(f"[WARN] 예약 페이지 진입 판정 애매: 마지막 URL={last}")

    def _looks_like_golf_page(self) -> bool:
        try:
            body = self.driver.find_element(By.TAG_NAME, "body").text
            if "골프 예약" in body:
                return True
            if self.driver.find_elements(By.CSS_SELECTOR, "div.wrap.theme-reserve.calendar"):
                return True
        except Exception:
            return False
        return False

    def check_calendar_loaded(self) -> bool:
        self.logger.log("[INFO] 달력 로딩 확인 중...")
        try:
            def ready(driver):
                cal_wrap = driver.find_elements(By.CSS_SELECTOR, "div.wrap.theme-reserve.calendar")
                if not cal_wrap:
                    return False
                tables = driver.find_elements(By.CSS_SELECTOR, "div.wrap.theme-reserve.calendar table")
                if not tables:
                    return False
                tds = driver.find_elements(By.CSS_SELECTOR, "div.wrap.theme-reserve.calendar td")
                if len(tds) < 10:
                    return False
                return True

            WebDriverWait(self.driver, 15).until(ready)
            return True
        except Exception:
            return False

    def select_date(self, ymd: str) -> bool:
        try:
            _y, _m, _d = [int(x) for x in ymd.split("-")]
        except Exception:
            self.logger.log("[WARN] 날짜 형식 오류")
            return False

        target_date = ymd.replace("-", "")
        self.logger.log(f"[INFO] 달력 날짜 클릭 시도: {ymd}")

        try:
            WebDriverWait(self.driver, 10).until(
                lambda d: len(d.find_elements(By.CSS_SELECTOR, "div.wrap.theme-reserve.calendar")) > 0
            )
            cal = self.driver.find_element(By.CSS_SELECTOR, "div.wrap.theme-reserve.calendar")
        except Exception:
            return False

        # 목표 일자 링크를 직접 찾고, 클릭/전환을 재시도한다.
        target_selector = f"a.reservebtn[href*='workDate={target_date}']"
        target_found = False

        for attempt in range(1, 4):
            try:
                cal = self.driver.find_element(By.CSS_SELECTOR, "div.wrap.theme-reserve.calendar")
                target_links = cal.find_elements(By.CSS_SELECTOR, target_selector)
                if not target_links:
                    # 전체 링크에서 href 파싱 fallback
                    links = cal.find_elements(By.CSS_SELECTOR, "a.reservebtn")
                    for a in links:
                        href = a.get_attribute("href") or ""
                        if f"workDate={target_date}" in href:
                            target_links = [a]
                            break

                if not target_links:
                    self.logger.log(f"[WARN] 날짜 링크 미탐지(시도 {attempt}/3): {ymd}")
                    time.sleep(0.25)
                    continue

                target_found = True
                a = target_links[0]

                self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", a)
                time.sleep(0.1)

                try:
                    a.click()
                except Exception:
                    self.driver.execute_script("arguments[0].click();", a)

                WebDriverWait(self.driver, 10).until(
                    lambda d: (
                        "workDate=" + target_date in (d.current_url or "")
                        and (
                            "golfReservationPage.do" in (d.current_url or "")
                            or "golfccReservationPage.do" in (d.current_url or "")
                        )
                    )
                )
                self.logger.log("[OK] 날짜 클릭 후 시간표 페이지 이동 확인")
                return True

            except Exception as e:
                self.logger.log(f"[WARN] 날짜 클릭/전환 실패(시도 {attempt}/3): {e}")
                time.sleep(0.3)

        if target_found:
            self.logger.log("[WARN] 날짜 링크는 찾았지만 시간표 페이지 전환 실패")
        else:
            self.logger.log("[WARN] 날짜 링크(reservebtn)를 찾지 못함")
        return False

    # --------------------------
    # ✅ 예약 시도(시간선택/동의/예약버튼/alert/성공판정)
    # --------------------------
    def try_book_with_time_candidates(self, ymd: str, target_time: str, stop_before_submit: bool) -> bool:
        started = time.perf_counter()
        self.logger.log("[INFO] 시간 선택/예약 시도 준비")

        # ✅ (삭제됨) _wait_for_time_table() 사용 안함
        # 시간표는 '라디오 등장'을 기준으로 새 모듈에서 자체 대기함

        self.logger.log(f"[TRY] 목표 시간 기준 최근접 자동 선택: {target_time}")

        select_started = time.perf_counter()
        ok = self.select_time_by_target(target_hhmm=target_time, max_candidates=12)
        self._perf(f"[PERF] select nearest time: {time.perf_counter() - select_started:.3f}s")
        if not ok:
            self.logger.log("[MISS] 최근접 시간 선택 실패(라디오/시간 추출/클릭 실패)")
            self._save_debug("nearest_time_pick_fail")
            return False

        self.logger.log("[OK] 최근접 시간 선택 완료")

        if not self._ensure_agree_checked():
            self.logger.log("[WARN] 동의 체크 실패")
            self._save_debug("agree_fail")
            return False
        self._perf(f"[PERF] until agree checked: {time.perf_counter() - started:.3f}s")

        if stop_before_submit:
            self.logger.log("[DRYRUN] 예약 버튼 클릭 직전에서 중단(실제 클릭 차단)")
            self._save_debug("before_submit")
            self._perf(f"[PERF] try_book total(dryrun): {time.perf_counter() - started:.3f}s")
            return True

        self.logger.log("[INFO] 예약 버튼 클릭 시도")
        reserve_started = time.perf_counter()
        if not self._click_reserve_button():
            self.logger.log("[WARN] 예약 버튼 클릭 실패")
            self._save_debug("reserve_btn_fail")
            return False
        self._perf(f"[PERF] click reserve button: {time.perf_counter() - reserve_started:.3f}s")

        # ✅ (핵심) 예약 클릭 직후 confirm(브라우저 팝업) 강제 처리
        confirm_started = time.perf_counter()
        if not self._accept_confirm_after_submit(timeout=10):
            self.logger.log("[WARN] confirm 팝업 감지 실패(10초) → 캡처")
            self._save_debug("confirm_not_found")
            return False
        self._perf(f"[PERF] accept confirm: {time.perf_counter() - confirm_started:.3f}s")

        # ✅ (1) 성공 페이지를 먼저 짧게 기다림 (성공 케이스에서 10초 절약)
        success_started = time.perf_counter()
        if self._wait_success_quick(timeout=2):
            self.logger.log("[OK] 성공 페이지(예약 정보) 빠른 감지")
            self._save_debug("success")
            self._perf(f"[PERF] wait success quick#1: {time.perf_counter() - success_started:.3f}s")
            self._perf(f"[PERF] try_book total: {time.perf_counter() - started:.3f}s")
            return True
        self._perf(f"[PERF] wait success quick#1: {time.perf_counter() - success_started:.3f}s")
        
        # ✅ (2) 성공이 아니면 그때 alert/오류 처리 (빠른 모드)
        alert_started = time.perf_counter()
        result, msg = self._handle_alerts(max_rounds=1, per_wait=1)
        if msg:
            self.logger.log(f"[ALERT] {msg}")
        self._perf(f"[PERF] handle alerts: {time.perf_counter() - alert_started:.3f}s")
                      
        # alert 처리 후에도 성공일 수 있으니 한번 더 확인              
        success2_started = time.perf_counter()
        if self._wait_success_quick(timeout=2):
            self.logger.log("[OK] 성공 페이지 (예약정보) 감시(후속))")
            self._save_debug("success")
            self._perf(f"[PERF] wait success quick#2: {time.perf_counter() - success2_started:.3f}s")
            self._perf(f"[PERF] try_book total: {time.perf_counter() - started:.3f}s")
            return True
        self._perf(f"[PERF] wait success quick#2: {time.perf_counter() - success2_started:.3f}s")
    
        if result is False:
            self.logger.log("[INFO] 예약 불가/매진으로 판단")
            self._perf(f"[PERF] try_book total: {time.perf_counter() - started:.3f}s")
            return False

        self.logger.log("[WARN] 성공/실패 판정 애매 → 캡처")
        self._save_debug("unknown_after_submit")
        self._perf(f"[PERF] try_book total: {time.perf_counter() - started:.3f}s")
        return False
    
    def _wait_success_quick(self, timeout: int = 2) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            self._check_stop()
            try:
                url = self.driver.current_url or ""
                if "golfReservation.do" in url or "golfReservationComplete" in url:
                    return True
            except Exception:
                pass

            if self._is_success_page():
                return True

            time.sleep(0.1)
        return False

    def _ensure_agree_checked(self) -> bool:
        try:
            agree = self.driver.find_elements(By.CSS_SELECTOR, "input#agree-1")
            if not agree:
                agree = self.driver.find_elements(
                    By.CSS_SELECTOR,
                    "input[type='checkbox'][name*='agree'], input[type='checkbox'][id*='agree']"
                )
            if not agree:
                return False

            el = agree[0]
            if el.is_selected():
                return True

            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            time.sleep(0.1)
            try:
                el.click()
            except Exception:
                aid = el.get_attribute("id")
                if aid:
                    lab = self.driver.find_elements(By.CSS_SELECTOR, f"label[for='{aid}']")
                    if lab:
                        lab[0].click()
            time.sleep(0.2)
            return el.is_selected()
        except Exception:
            return False

    def _click_reserve_button(self) -> bool:
        """
        알펜시아 예약 버튼 클릭 (a.btn.wide '예약')
        - button이 아니라 <a class="btn wide">예약</a> 구조 
        - JS 클릭으로 이벤트 체인 보장
        """  

        try:
            # 1) 가장 정확: a.btn.wide 중 텍스트가 '예약'인 것
            btn = None
            candidates = self.driver.find_elements(By.CSS_SELECTOR, "a.btn.wide")
            for a in candidates:
                txt = (a.text or "").strip()
                href = (a.get_attribute("href") or "").strip()
                if txt == "예약":
                    btn = a
                    break
                # fallback: href에 fnNext 같은 예약 함수가 들어가면 그것도 예약 버튼일 확률 높음
                if ("fnNext" in href) and ("javascript:" in href):
                    btn = a
            # 2) CSS로 못 찾으면 XPath fallback
            if btn is None:
                btn = self.driver.find_element(
                    By.XPATH,
                    "//a[contains(@class,'btn') and contains(@class,'wide') and normalize-space(.)='예약']"
            )
            # 3) 클릭 안정화 (스크롤 + JS click)
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            time.sleep(0.2)
            self.driver.execute_script("arguments[0].click();", btn)
            time.sleep(0.2)

            self.logger.log("[OK] 예약 버튼 클릭 완료(a.btn.wide)")
            return True

        except Exception as e:
            self.logger.log(f"[ERR] 예약 버튼 클릭 실패: {e}")
            return False
                  
    def _handle_alerts(self, max_rounds: int = 3, per_wait: int = 3) -> Tuple[Optional[bool], str]:
        msg_all = []
        verdict: Optional[bool] = None

        for _ in range(max_rounds):
            self._check_stop()
            try:
                WebDriverWait(self.driver, per_wait).until(EC.alert_is_present())
            except Exception:
                break

            try:
                alert = self.driver.switch_to.alert
                text = (alert.text or "").strip()
                msg_all.append(text)

                alert.accept()
                time.sleep(0.2)

                lower = text.replace(" ", "")
                if any(k in lower for k in ["마감", "매진", "불가", "없습니다", "오류", "실패", "선택", "확인"]):
                    if "하시겠습니까" not in lower:
                        verdict = False
            except Exception:
                break

        return verdict, " / ".join([m for m in msg_all if m])

    def _accept_confirm_after_submit(self, timeout: int = 10) -> bool:
        """
        '골프 예약을 하시겠습니까?' 같은 브라우저 confirm을 안정적으로 accept
        - alert로 취급됨
        - timeout 동안 반복 대기/재시도
        """
        end = time.time() + timeout
        last_err = None

        while time.time() < end:
            self._check_stop()
            try:
                WebDriverWait(self.driver, 1).until(EC.alert_is_present())
                a = self.driver.switch_to.alert
                text = (a.text or "").strip()
                self.logger.log(f"[CONFIRM] {text}")
                a.accept()
                time.sleep(0.2)
                return True
            except Exception as e:
                last_err = e
                time.sleep(0.1)

        self.logger.log(f"[CONFIRM] not found (last={last_err})")
        return False

    def _is_success_page(self) -> bool:
        try:
            body = self.driver.find_element(By.TAG_NAME, "body").text
            key_ok = any(k in body for k in ["예약 정보", "예약완료", "예약 완료", "예약번호", "예약이 완료"])
            if key_ok:
                return True
        except Exception:
            return False
        return False

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self._icon_png = None
        self._apply_window_icon()
        self.title("알펜시아 예약")
        self.geometry("1280x720")
        self.configure(bg=APP_BG)

        self.stop_event = threading.Event()
        self.worker_thread = None
        self.bot: Optional[AlpensiaBot] = None
        self.saved_accounts: List[dict] = []
        self._cred_error_shown = False

        self._closing = False  # ✅ 기존 코드에 있었던 _closing 미정의 문제 해결

        self._apply_style()
        self._build_ui()
        self._load_credentials()
        self._load_config()

    def _apply_window_icon(self):
        ok = False
        ico_path = _first_existing_resource(ICON_FILENAME)
        try:
            if ico_path:
                self.iconbitmap(default=ico_path)
                ok = True
        except Exception as e:
            print(f"[ICON][WARN] iconbitmap failed: {e} ({ico_path})")

        try:
            if ico_path:
                self.wm_iconbitmap(ico_path)
                ok = True
        except Exception:
            pass

        try:
            icon_png = _first_existing_resource(LOGO_FILENAME)
            if icon_png:
                self._icon_png = tk.PhotoImage(file=icon_png)
                self.iconphoto(True, self._icon_png)
                ok = True
        except Exception as e:
            print(f"[ICON][WARN] iconphoto failed: {e}")

        if not ok:
            print(f"[ICON][WARN] no window icon applied. ico={ico_path}")

    def _apply_style(self):
        style = ttk.Style(self)

        try:
            style.theme_use("vista")
        except Exception:
            try:
                style.theme_use("winnative")
            except Exception:
                pass

        try:
            style.configure("Today.TButton", font=("맑은 고딕", 9, "bold"))
        except Exception:
            pass

        try:
            style.configure(".", background=APP_BG)
            style.configure("TLabelframe", background=APP_BG)
            style.configure("TLabelframe.Label", background=APP_BG)
            style.configure("TFrame", background=APP_BG)
            style.configure("TLabel", background=APP_BG)
        except Exception:
            pass

    def _build_ui(self):
        self.columnconfigure(0, weight=0)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        left = ttk.Frame(self, padding=12)
        left.grid(row=0, column=0, sticky="nsw")

        right = ttk.Frame(self, padding=12)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)

        header = ttk.Frame(left)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        header.columnconfigure(1, weight=1)

        self.logo_label = ttk.Label(header)
        self.logo_label.grid(row=0, column=0, sticky="w", padx=(0, 8))

        title = ttk.Label(header, text="알펜시아 예약", font=("맑은 고딕", 20, "bold"))
        title.grid(row=0, column=1, sticky="w")

        self._load_logo()

        lf_login = ttk.LabelFrame(left, text="로그인 정보", padding=10)
        lf_login.grid(row=1, column=0, sticky="ew", pady=(0, 10))

        ttk.Label(lf_login, text="아이디").grid(row=0, column=0, sticky="w")
        self.ent_id = ttk.Combobox(lf_login, width=26)
        self.ent_id.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.ent_id.bind("<<ComboboxSelected>>", self._on_id_selected)
        self.ent_id.bind("<FocusOut>", self._on_id_focus_out)
        self.btn_delete_id = ttk.Button(lf_login, text="삭제", width=6, command=self._delete_selected_account)
        self.btn_delete_id.grid(row=0, column=2, sticky="w", padx=(6, 0))

        ttk.Label(lf_login, text="비밀번호").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.ent_pw = tk.Entry(lf_login, width=28, bg=WHITE, show="*")
        self.ent_pw.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(8, 0))

        self.var_show_pw = tk.BooleanVar(value=False)
        chk_show = ttk.Checkbutton(
            lf_login, text="비밀번호 표시", variable=self.var_show_pw, command=self._toggle_pw
        )
        chk_show.grid(row=2, column=1, sticky="w", pady=(8, 0))

        self.var_remember = tk.BooleanVar(value=True)
        chk_remember = ttk.Checkbutton(lf_login, text="아이디/비밀번호 기억", variable=self.var_remember)
        chk_remember.grid(row=3, column=1, sticky="w", pady=(6, 0))

        lf_course = ttk.LabelFrame(left, text="골프장 선택", padding=10)
        lf_course.grid(row=2, column=0, sticky="ew", pady=(0, 10))

        self.var_course = tk.StringVar(value="700")
        ttk.Radiobutton(lf_course, text="알펜시아 700 G.C", value="700", variable=self.var_course).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Radiobutton(lf_course, text="알펜시아 C.C", value="cc", variable=self.var_course).grid(
            row=1, column=0, sticky="w", pady=(6, 0)
        )

        lf_mode = ttk.LabelFrame(left, text="실행 모드", padding=10)
        lf_mode.grid(row=3, column=0, sticky="ew", pady=(0, 10))

        self.var_test_mode = tk.IntVar(value=1) # 1: 테스트 모드, 0: 실제 예약 모드
        # ✅ 서버시간 대기 옵션 변수는 체크박스 만들기 전에 반드시 생성
        self.var_wait_open = tk.BooleanVar(value=True)
        # ✅ 체크박스: self로 잡아두기
        self.chk_wait_open = ttk.Checkbutton(
            lf_mode, text="서버시간 09:00까지 대기 (실제 예약 모드에서만)",
            variable=self.var_wait_open, command=self._on_wait_open_change
        )
        self.chk_wait_open.grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.var_live_safe = tk.BooleanVar(value=True)
        self.chk_live_safe = ttk.Checkbutton(
            lf_mode, text="실예약 안전 점검 (예약 클릭 차단)",
            variable=self.var_live_safe, command=self._on_live_safe_change
        )
        self.chk_live_safe.grid(row=3, column=0, sticky="w", pady=(6, 0))
        # ✅ 라디오버튼에 command 추가
        ttk.Radiobutton(
            lf_mode, text="테스트 모드 (예약 직전까지)", value=1, variable=self.var_test_mode, command=self._on_mode_change
        ).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(
            lf_mode, text="실제 예약 모드 (예약까지 진행)", value=0, variable=self.var_test_mode, command=self._on_mode_change
        ).grid(row=1, column=0, sticky="w", pady=(6, 0))
        # ✅ 초기 상태 반영(맨 마지막에 1줄)
        self._on_mode_change()

        lf_pri = ttk.LabelFrame(left, text="예약 우선순위 (1→2→3 순서, 다건 예약)", padding=10)
        lf_pri.grid(row=4, column=0, sticky="ew", pady=(0, 10))

        time_opts = [fmt_hhmm(m) for m in range(6 * 60, 19 * 60 + 1, 10)]

        self.pri_vars = []
        self.pri_widgets = []
        footer = tk.Label(
            self,
            text=f"Alpensia golf Reservation Tool v{APP_VERSION}\n© 2026 Dev. by Armatech",
            font=("Arial", 10, "italic"),
            fg="gray",
            justify="left",
        )
        footer.grid(row="999", column=0, columnspan=99, sticky="w", padx=10, pady=5)

        def add_row(row_idx: int):
            use_var = tk.BooleanVar(value=True if row_idx == 0 else False)
            date_var = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d"))
            time_var = tk.StringVar(value="09:00")

            chk = ttk.Checkbutton(
                lf_pri,
                text=f"{row_idx + 1}순위 사용",
                variable=use_var,
                command=lambda idx=row_idx: self._on_priority_toggle(idx),
            )
            chk.grid(row=row_idx, column=0, sticky="w", pady=(2, 2))

            ent_date = tk.Entry(lf_pri, width=12, bg=WHITE, textvariable=date_var, state="readonly", readonlybackground=WHITE)
            ent_date.grid(row=row_idx, column=1, sticky="w", padx=(8, 4))

            def open_calendar(_evt=None, v=date_var):
                DatePicker(self, v.get(), lambda ymd: v.set(ymd))

            ent_date.bind("<Button-1>", open_calendar)

            btn_cal = ttk.Button(lf_pri, text="📅", width=3, command=open_calendar)
            btn_cal.grid(row=row_idx, column=2, sticky="w", padx=(0, 10))

            cb = ttk.Combobox(lf_pri, width=7, state="readonly", values=time_opts, textvariable=time_var)
            try:
                cb.configure(background=WHITE)
            except Exception:
                pass
            cb.grid(row=row_idx, column=3, sticky="w")

            self.pri_vars.append((use_var, date_var, time_var))
            self.pri_widgets.append((chk, ent_date, btn_cal, cb))

            if not use_var.get():
                date_var.set("")
                time_var.set("")
            self._apply_priority_enabled_state(row_idx)

        for i in range(3):
            add_row(i)

        btn_frame = ttk.Frame(left)
        btn_frame.grid(row=5, column=0, sticky="ew", pady=(6, 0))
        btn_frame.columnconfigure(0, weight=1)
        btn_frame.columnconfigure(1, weight=1)

        self.btn_start = ttk.Button(btn_frame, text="예약 시작", command=self.start)
        self.btn_start.grid(row=0, column=0, sticky="ew", padx=(0, 6))

        self.btn_stop = ttk.Button(btn_frame, text="중단", command=self.stop, state="disabled")
        self.btn_stop.grid(row=0, column=1, sticky="ew", padx=(6, 0))

        lf_log = ttk.LabelFrame(right, text="실행 로그", padding=10)
        lf_log.grid(row=0, column=0, sticky="nsew")
        lf_log.rowconfigure(0, weight=1)
        lf_log.columnconfigure(0, weight=1)

        self.txt_log = tk.Text(lf_log, wrap="word", height=30)
        self.txt_log.grid(row=0, column=0, sticky="nsew")

        scroll = ttk.Scrollbar(lf_log, orient="vertical", command=self.txt_log.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.txt_log.configure(yscrollcommand=scroll.set)

        self.logger = UiLogger(self.txt_log)

        self.lbl_tip = ttk.Label(
            right,
            text="팁: 테스트 모드는 '예약 버튼 클릭 직전'까지, 실제 예약 모드는 confirm/alert 처리 후 성공 페이지까지 확인합니다.",
        )
        self.lbl_tip.grid(row=1, column=0, sticky="w", pady=(8, 0))
    def _on_mode_change(self):
        """
        테스트/실예약 모드 바뀔 때 호출.
        - 테스트 모드면 09:00 대기 체크박스 비활성화 + 체크 해제
        - 실예약 모드면 활성화
        """
        is_test = (self.var_test_mode.get() == 1)

        if is_test:
            # 테스트 모드: 대기 옵션 사용 못하게 + 체크 해제
            self.var_wait_open.set(False)
            self.var_live_safe.set(True)
            try:
                self.chk_wait_open.configure(state="disabled")
                self.chk_live_safe.configure(state="disabled")
            except Exception:
                pass
        else:
            # 실예약 모드: 대기 옵션 사용 가능
            try:
                self.chk_wait_open.configure(state="normal")
                self.chk_live_safe.configure(state="normal")
            except Exception:
                pass
            self._enforce_mode_option_exclusive(prefer="safe")

    def _enforce_mode_option_exclusive(self, prefer: str = "safe"):
        if self.var_wait_open.get() and self.var_live_safe.get():
            if prefer == "wait":
                self.var_live_safe.set(False)
            else:
                self.var_wait_open.set(False)

    def _on_wait_open_change(self):
        if self.var_test_mode.get() == 1:
            self.var_wait_open.set(False)
            return
        if self.var_wait_open.get():
            self.var_live_safe.set(False)

    def _on_live_safe_change(self):
        if self.var_test_mode.get() == 1:
            self.var_live_safe.set(True)
            return
        if self.var_live_safe.get():
            self.var_wait_open.set(False)

    def _apply_priority_enabled_state(self, index: int):
        enabled = bool(self.pri_vars[index][0].get())
        _chk, ent_date, btn_cal, cb = self.pri_widgets[index]
        entry_state = "readonly" if enabled else "disabled"
        button_state = "normal" if enabled else "disabled"
        combo_state = "readonly" if enabled else "disabled"
        try:
            ent_date.configure(state=entry_state)
        except Exception:
            pass
        try:
            btn_cal.configure(state=button_state)
        except Exception:
            pass
        try:
            cb.configure(state=combo_state)
        except Exception:
            pass

    def _on_priority_toggle(self, index: int):
        use_var, date_var, time_var = self.pri_vars[index]
        if not use_var.get():
            date_var.set("")
            time_var.set("")
        self._apply_priority_enabled_state(index)

    def _load_logo(self):
        try:
            from PIL import Image, ImageTk  # type: ignore
        except Exception:
            return

        path = _first_existing_resource(LOGO_FILENAME)
        if not path:
            return

        try:
            img = Image.open(path)
            img = img.resize((160, 45), Image.LANCZOS)
            self._logo_img = ImageTk.PhotoImage(img)
            self.logo_label.configure(image=self._logo_img)
        except Exception:
            pass

    def _toggle_pw(self):
        self.ent_pw.configure(show="" if self.var_show_pw.get() else "*")

    def _refresh_id_dropdown(self):
        ids = [acc["user_id"] for acc in self.saved_accounts if acc.get("user_id")]
        self.ent_id.configure(values=ids)
        try:
            self.btn_delete_id.configure(state="normal" if ids else "disabled")
        except Exception:
            pass

    def _find_saved_account(self, user_id: str) -> Optional[dict]:
        uid = user_id.strip()
        if not uid:
            return None
        for acc in self.saved_accounts:
            if acc.get("user_id") == uid:
                return acc
        return None

    def _autofill_password_for_id(self, user_id: str) -> bool:
        acc = self._find_saved_account(user_id)
        if not acc:
            return False
        self.ent_pw.delete(0, "end")
        self.ent_pw.insert(0, acc.get("password", ""))
        return True

    def _on_id_selected(self, _evt=None):
        self._autofill_password_for_id(self.ent_id.get())

    def _on_id_focus_out(self, _evt=None):
        uid = self.ent_id.get().strip()
        if uid and not self.ent_pw.get().strip():
            self._autofill_password_for_id(uid)

    def _show_cred_warn_once(self, msg: str):
        if self._cred_error_shown:
            return
        self._cred_error_shown = True
        try:
            messagebox.showwarning("계정 저장", msg)
        except Exception:
            pass

    def _load_credentials(self):
        self.saved_accounts = []
        if not os.path.exists(CREDENTIALS_PATH):
            self._refresh_id_dropdown()
            return
        try:
            with open(CREDENTIALS_PATH, "r", encoding="utf-8") as f:
                payload = json.load(f)

            loaded = []
            for row in payload.get("accounts", []):
                uid_enc = row.get("user_id_enc", "")
                pw_enc = row.get("password_enc", "")
                if not uid_enc or not pw_enc:
                    continue
                uid = _dpapi_decrypt(uid_enc)
                pw = _dpapi_decrypt(pw_enc)
                if not uid:
                    continue
                loaded.append({
                    "user_id": uid,
                    "password": pw,
                    "updated_at": int(row.get("updated_at", 0)),
                })

            loaded.sort(key=lambda x: x.get("updated_at", 0), reverse=True)
            self.saved_accounts = loaded[:MAX_SAVED_ACCOUNTS]
        except Exception:
            self.saved_accounts = []

        self._refresh_id_dropdown()

    def _persist_credentials(self):
        payload = {"version": 1, "accounts": []}
        for acc in self.saved_accounts[:MAX_SAVED_ACCOUNTS]:
            uid = acc.get("user_id", "").strip()
            pw = acc.get("password", "")
            if not uid or not pw:
                continue
            payload["accounts"].append({
                "user_id_enc": _dpapi_encrypt(uid),
                "password_enc": _dpapi_encrypt(pw),
                "updated_at": int(acc.get("updated_at", int(time.time()))),
            })

        with open(CREDENTIALS_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    def _remember_credentials(self, user_id: str, password: str):
        uid = user_id.strip()
        pw = password.strip()
        if not uid or not pw:
            return
        try:
            rest = [acc for acc in self.saved_accounts if acc.get("user_id") != uid]
            rest.insert(0, {"user_id": uid, "password": pw, "updated_at": int(time.time())})
            self.saved_accounts = rest[:MAX_SAVED_ACCOUNTS]
            self._refresh_id_dropdown()
            self._persist_credentials()
        except Exception:
            self._show_cred_warn_once("아이디/비밀번호 암호화 저장에 실패했습니다.")

    def _delete_selected_account(self):
        uid = self.ent_id.get().strip()
        if not uid:
            messagebox.showinfo("계정 삭제", "삭제할 아이디를 먼저 선택해 주세요.")
            return

        if not self._find_saved_account(uid):
            messagebox.showinfo("계정 삭제", "저장된 목록에 없는 아이디입니다.")
            return

        ok = messagebox.askyesno("계정 삭제", f"'{uid}' 계정을 저장 목록에서 삭제할까요?")
        if not ok:
            return

        try:
            self.saved_accounts = [acc for acc in self.saved_accounts if acc.get("user_id") != uid]
            self._persist_credentials()
            self._refresh_id_dropdown()

            ids = [acc["user_id"] for acc in self.saved_accounts if acc.get("user_id")]
            if ids:
                self.ent_id.set(ids[0])
                self._autofill_password_for_id(ids[0])
            else:
                self.ent_id.set("")
                self.ent_pw.delete(0, "end")
        except Exception:
            self._show_cred_warn_once("저장 계정 삭제에 실패했습니다.")

    def _load_config(self):
        if not os.path.exists(CONFIG_PATH):
            if self.saved_accounts:
                first_id = self.saved_accounts[0].get("user_id", "")
                if first_id:
                    self.ent_id.set(first_id)
                    self._autofill_password_for_id(first_id)
            return
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)

            self.ent_pw.delete(0, "end")
            self.var_remember.set(cfg.get("remember", True))

            saved_user_id = cfg.get("user_id", "").strip()
            if saved_user_id:
                self.ent_id.set(saved_user_id)
                self._autofill_password_for_id(saved_user_id)
            elif self.saved_accounts and self.var_remember.get():
                first_id = self.saved_accounts[0].get("user_id", "")
                if first_id:
                    self.ent_id.set(first_id)
                    self._autofill_password_for_id(first_id)

            self.var_course.set(cfg.get("course", "700"))
            self.var_test_mode.set(cfg.get("test_mode", 1))  # 1: 테스트 모드, 0: 실제 예약 모드
            self.var_wait_open.set(cfg.get("wait_open", True))
            self.var_live_safe.set(cfg.get("live_safety_block_submit", True))
            self._enforce_mode_option_exclusive(prefer="safe")

            pri = cfg.get("priorities", [])
            for i in range(min(3, len(pri))):
                enabled = bool(pri[i].get("enabled", True if i == 0 else False))
                self.pri_vars[i][0].set(enabled)
                self.pri_vars[i][1].set(pri[i].get("ymd", datetime.now().strftime("%Y-%m-%d")) if enabled else "")
                self.pri_vars[i][2].set(pri[i].get("hhmm", "09:00") if enabled else "")
                self._apply_priority_enabled_state(i)
        except Exception:
            pass

    def _save_config(self):
        pri = []
        for i in range(3):
            use_var, date_var, time_var = self.pri_vars[i]
            pri.append({
                "enabled": bool(use_var.get()),
                "ymd": date_var.get().strip(),
                "hhmm": time_var.get().strip(),
            })

        cfg = {
            "user_id": self.ent_id.get().strip(),
            "remember": bool(self.var_remember.get()),
            "course": self.var_course.get(),
            "test_mode": int(self.var_test_mode.get()), # 1: 테스트 모드, 0: 실제 예약 모드
            "wait_open": bool(self.var_wait_open.get()),
            "live_safety_block_submit": bool(self.var_live_safe.get()),
            "priorities": pri,
        }
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _make_debug_dir(self) -> str:
        here = os.path.dirname(os.path.abspath(__file__))
        d = os.path.join(here, "debug_captures")
        os.makedirs(d, exist_ok=True)
        return d

    def start(self):
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("안내", "이미 실행 중입니다.")
            return

        user_id = self.ent_id.get().strip()
        password = self.ent_pw.get().strip()
        if user_id and not password:
            self._autofill_password_for_id(user_id)
            password = self.ent_pw.get().strip()

        if not user_id or not password:
            messagebox.showwarning("입력 확인", "아이디/비밀번호를 입력해 주세요.")
            return

        priorities: List[PriorityItem] = []
        for i in range(3):
            use_var, date_var, time_var = self.pri_vars[i]
            priorities.append(PriorityItem(
                enabled=bool(use_var.get()),
                ymd=date_var.get().strip(),
                hhmm=time_var.get().strip(),
            ))

        for p in priorities:
            if p.enabled:
                if len(p.ymd) != 10 or p.ymd[4] != "-" or p.ymd[7] != "-":
                    messagebox.showwarning("입력 확인", f"날짜 형식이 올바르지 않습니다: {p.ymd}\n예: 2026-02-09")
                    return
                if parse_hhmm(p.hhmm) is None:
                    messagebox.showwarning("입력 확인", f"시간 형식이 올바르지 않습니다: {p.hhmm}\n예: 09:00")
                    return

        if bool(self.var_remember.get()):
            self._remember_credentials(user_id, password)
        self._save_config()

        self.stop_event.clear()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")

        self.txt_log.delete("1.0", "end")

        if DEBUG_CAPTURE_ENABLED:
            debug_dir = self._make_debug_dir()
            self.logger.log(f"[INFO] 디버그 캡처 폴더: {debug_dir}")
        else:
            debug_dir = ""
            self.logger.log("[INFO] 디버그 캡처 비활성화 (배포 기본값)")

        course_key = self.var_course.get()
        test_mode = (self.var_test_mode.get() == 1)  # 1: 테스트 모드, 0: 실제 예약 모드
        wait_open = bool(self.var_wait_open.get())
        live_safety_block_submit = bool(self.var_live_safe.get())
        # ✅ 안전장치: 테스트 모드면 대기 옵션 무조건 OFF
        if test_mode:
            wait_open = False
            live_safety_block_submit = True
            
        self.bot = AlpensiaBot(logger=self.logger, stop_event=self.stop_event, debug_dir=debug_dir, headless=False)

        def worker():
            try:
                self.bot.run(
                    user_id=user_id,
                    password=password,
                    course_key=course_key,
                    priorities=priorities,
                    test_mode=test_mode,
                    wait_open=wait_open,
                    live_safety_block_submit=live_safety_block_submit
                )
            finally:
                try:
                    if not self._closing and self.winfo_exists():
                        self.after(0, self._on_worker_done)
                except Exception:
                    pass

        self.worker_thread = threading.Thread(target=worker, daemon=True)
        self.worker_thread.start()

    def stop(self):
        self.stop_event.set()
        self.logger.log("[INFO] 중단 요청")
        self.btn_stop.configure(state="disabled")
        try:
            if self.bot:
                self.bot.close()
                self.logger.log("[INFO] 브라우저 종료")
        except Exception:
            pass

    def _on_worker_done(self):
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self._save_config()
        self.logger.log("[END] 작업 종료(대기 중 아님)")

    def on_close(self):
        self._closing = True
        try:
            self.stop_event.set()
            if self.bot:
                self.bot.close()
        except Exception:
            pass
        self.destroy()


if __name__ == "__main__":
    _set_windows_app_id()
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()







