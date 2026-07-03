import base64
import ctypes
import json
import os
import re
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import messagebox, ttk

from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
    RESOURCE_DIR = os.path.join(APP_DIR, "_internal")
    if not os.path.isdir(RESOURCE_DIR):
        RESOURCE_DIR = APP_DIR
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
    RESOURCE_DIR = APP_DIR

APP_DATA_DIR = os.path.join(os.environ.get("APPDATA", APP_DIR), "Armatech", "Alpensia")
os.makedirs(APP_DATA_DIR, exist_ok=True)


def _state_path(filename: str) -> str:
    target = os.path.join(APP_DATA_DIR, filename)
    legacy = os.path.join(APP_DIR, filename)
    if not os.path.exists(target) and os.path.exists(legacy):
        try:
            import shutil
            shutil.copy2(legacy, target)
        except Exception:
            return legacy
    return target


CONFIG_PATH = _state_path("cancel_watcher_config.json")
CREDENTIALS_PATH = _state_path("credentials.enc.json")
LOGO_FILENAME = "alpensia_logo.png"
ICON_FILENAME = "alpensia_logo.ico"
APP_BG = "#f0f0f0"
WHITE = "#ffffff"
APP_TITLE = "알펜시아 취소티 감시"
APP_VERSION = "1.0.1"
MAX_SAVED_ACCOUNTS = 20
WATCH_COUNT = 3
DPAPI_ENTROPY = b"Alpensia_V4.1.1_Credentials"
DEBUG_CAPTURE_ENABLED = False


def _resource_candidates(filename: str):
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        yield os.path.join(meipass, filename)
    yield os.path.join(RESOURCE_DIR, filename)
    yield os.path.join(APP_DIR, filename)
    yield os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)


def _first_existing_resource(filename: str) -> Optional[str]:
    for path in _resource_candidates(filename):
        if os.path.exists(path):
            return path
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


def fmt_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def hhmm_to_minutes(text: str) -> Optional[int]:
    s = (text or "").strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return hh * 60 + mm
    m = re.fullmatch(r"(\d{2})(\d{2})", s)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return hh * 60 + mm
    return None


@dataclass
class WatchItem:
    enabled: bool
    ymd: str
    start_hhmm: str
    end_hhmm: str

    @property
    def yyyymmdd(self) -> str:
        return self.ymd.replace("-", "")


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
        self.geometry(f"+{master.winfo_rootx() + 350}+{master.winfo_rooty() + 250}")

    def _build(self):
        top = tk.Frame(self, bg=APP_BG)
        top.pack(fill="x", padx=10, pady=8)
        ttk.Button(top, text="<", width=4, command=self._prev_month).pack(side="left")
        self.lbl = tk.Label(top, text="", bg=APP_BG, font=("맑은 고딕", 11, "bold"))
        self.lbl.pack(side="left", expand=True)
        ttk.Button(top, text=">", width=4, command=self._next_month).pack(side="right")
        self.grid_frame = tk.Frame(self, bg=APP_BG)
        self.grid_frame.pack(padx=10, pady=(0, 10))
        self._render()

    def _render(self):
        for w in self.grid_frame.winfo_children():
            w.destroy()
        y, m = self.cur.year, self.cur.month
        self.lbl.config(text=f"{y:04d}-{m:02d}")
        for c, name in enumerate(["일", "월", "화", "수", "목", "금", "토"]):
            tk.Label(self.grid_frame, text=name, bg=APP_BG, width=4, font=("맑은 고딕", 9, "bold")).grid(
                row=0, column=c, padx=2, pady=2
            )

        first = date(y, m, 1)
        start_col = (first.weekday() + 1) % 7
        last = date(y + 1, 1, 1) - timedelta(days=1) if m == 12 else date(y, m + 1, 1) - timedelta(days=1)
        r, c = 1, start_col
        for day in range(1, last.day + 1):
            cur_day = date(y, m, day)

            def pick(d=cur_day):
                self.on_pick(d.strftime("%Y-%m-%d"))
                self.destroy()

            ttk.Button(self.grid_frame, text=f"{day:02d}", width=4, command=pick).grid(row=r, column=c, padx=2, pady=2)
            c += 1
            if c >= 7:
                c = 0
                r += 1

    def _prev_month(self):
        y, m = self.cur.year, self.cur.month
        self.cur = date(y - 1, 12, 1) if m == 1 else date(y, m - 1, 1)
        self._render()

    def _next_month(self):
        y, m = self.cur.year, self.cur.month
        self.cur = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
        self._render()


class CancelWatcherBot:
    BASE = "https://www.alpensia.com"

    def __init__(
        self,
        logger: UiLogger,
        stop_event: threading.Event,
        pause_event: threading.Event,
        resume_now_event: threading.Event,
        debug_dir: str,
    ):
        self.logger = logger
        self.stop_event = stop_event
        self.pause_event = pause_event
        self.resume_now_event = resume_now_event
        self.debug_dir = debug_dir
        self.driver = None
        self.wait = None
        self.user_id = ""
        self.password = ""

    def _check_stop(self):
        if self.stop_event.is_set():
            raise RuntimeError("중단 요청")

    def _wait_if_paused(self):
        if self.pause_event.is_set():
            self.logger.log("[PAUSE] 감시 일시중지 중")
        while self.pause_event.is_set():
            self._check_stop()
            time.sleep(0.2)
        self._check_stop()

    def _new_driver(self):
        opts = ChromeOptions()
        opts.add_argument("--disable-gpu")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--start-maximized")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        opts.add_experimental_option("prefs", {
            "credentials_enable_service": False,
            "profile.password_manager_enabled": False,
        })
        self.driver = webdriver.Chrome(options=opts)
        self.wait = WebDriverWait(self.driver, 15)

    def close(self):
        try:
            if self.driver:
                self.driver.quit()
        except Exception:
            pass

    def _save_debug(self, prefix: str):
        if not DEBUG_CAPTURE_ENABLED or not self.debug_dir or not self.driver:
            return
        try:
            os.makedirs(self.debug_dir, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            png = os.path.join(self.debug_dir, f"{prefix}_{ts}.png")
            html = os.path.join(self.debug_dir, f"{prefix}_{ts}.html")
            self.driver.save_screenshot(png)
            with open(html, "w", encoding="utf-8") as f:
                f.write(self.driver.page_source)
            self.logger.log(f"[DEBUG] 캡처 저장: {png}")
        except Exception:
            pass

    def login(self, user_id: str, password: str):
        self.user_id = user_id
        self.password = password
        self._check_stop()
        self.logger.log("[INFO] 로그인 페이지 이동")
        self.driver.get(f"{self.BASE}/login.do")
        WebDriverWait(self.driver, 15).until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[name='emplyrId']")))

        id_input = self.driver.find_element(By.CSS_SELECTOR, "input[name='emplyrId']")
        pw_input = self.driver.find_element(By.CSS_SELECTOR, "input[name='password']")
        id_input.clear()
        id_input.send_keys(user_id)
        pw_input.clear()
        pw_input.send_keys(password)

        btn = WebDriverWait(self.driver, 15).until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button[type='submit']")))
        btn.click()

        WebDriverWait(self.driver, 15).until(lambda d: self._looks_logged_in() or "login.do" not in (d.current_url or ""))
        if not self._looks_logged_in():
            self._save_debug("login_failed")
            raise RuntimeError("로그인 확인 실패")
        self.logger.log("[OK] 로그인 성공")

    def _looks_logged_in(self) -> bool:
        try:
            if self.driver.find_elements(By.CSS_SELECTOR, "a[href='/logout.do']"):
                return True
            if "main.do" in (self.driver.current_url or ""):
                return True
        except Exception:
            pass
        return False

    def _is_login_page(self) -> bool:
        try:
            url = self.driver.current_url or ""
            if "login.do" in url:
                return True
            if self.driver.find_elements(By.CSS_SELECTOR, "input[name='emplyrId'], input[name='password']"):
                return True
        except Exception:
            pass
        return False

    def _ensure_logged_in(self) -> bool:
        if self._is_login_page():
            self.logger.log("[WARN] 로그인 세션 만료 감지 → 재로그인")
            self.login(self.user_id, self.password)
            return True
        return False

    def _reservation_url(self, yyyymmdd: str) -> str:
        return f"{self.BASE}/reservation/pgolf/golfReservationPage.do?workDate={yyyymmdd}"

    def _open_watch_date(self, item: WatchItem):
        self._check_stop()
        url = self._reservation_url(item.yyyymmdd)
        self.driver.get(url)
        time.sleep(0.4)
        relogged = self._ensure_logged_in()
        if relogged:
            self.driver.get(url)
            time.sleep(0.4)
        if self._is_login_page():
            raise RuntimeError("재로그인 후에도 로그인 페이지입니다.")

    def _collect_slots(self) -> List[dict]:
        slots = self.driver.execute_script(
            """
            const radios = Array.from(document.querySelectorAll('input[name="selBookg"], input[name="selBook"]'));
            return radios.map((r, idx) => {
                const tr = r.closest('tr');
                const text = tr ? (tr.innerText || tr.textContent || '') : '';
                return {
                    idx: idx + 1,
                    id: (r.id || '').trim(),
                    name: (r.name || '').trim(),
                    disabled: !!r.disabled,
                    bookgdate: (r.getAttribute('bookgdate') || '').trim(),
                    bookgtime: (r.getAttribute('bookgtime') || '').trim(),
                    bookgcourse: (r.getAttribute('bookgcourse') || '').trim(),
                    bookgcoursenm: (r.getAttribute('bookgcoursenm') || '').trim(),
                    bookgseq: (r.getAttribute('bookgseq') || '').trim(),
                    fee: (r.getAttribute('fee') || '').trim(),
                    row_text: text.trim()
                };
            });
            """
        ) or []

        parsed = []
        for s in slots:
            minutes = hhmm_to_minutes(s.get("bookgtime") or "")
            if minutes is None:
                m = re.search(r"(\d{2}:\d{2})", s.get("row_text") or "")
                minutes = hhmm_to_minutes(m.group(1)) if m else None
            if minutes is None:
                continue
            s["minutes"] = minutes
            parsed.append(s)
        return parsed

    def _candidate_slots(self, item: WatchItem) -> List[dict]:
        start_min = hhmm_to_minutes(item.start_hhmm)
        end_min = hhmm_to_minutes(item.end_hhmm)
        if start_min is None or end_min is None or start_min > end_min:
            raise RuntimeError(f"시간 범위 오류: {item.ymd} {item.start_hhmm}~{item.end_hhmm}")

        candidates = []
        for slot in self._collect_slots():
            if slot.get("disabled"):
                continue
            if item.yyyymmdd and slot.get("bookgdate") and slot.get("bookgdate") != item.yyyymmdd:
                continue
            minutes = int(slot["minutes"])
            if start_min <= minutes <= end_min:
                candidates.append(slot)

        candidates.sort(key=lambda x: (int(x["minutes"]), int(x.get("idx", 9999))))
        return candidates

    def _resolve_slot_element(self, slot: dict):
        rid = (slot.get("id") or "").strip()
        if rid:
            try:
                return self.driver.find_element(By.ID, rid)
            except Exception:
                pass
        name = (slot.get("name") or "selBookg").strip() or "selBookg"
        idx = max(1, int(slot.get("idx", 1)))
        radios = self.driver.find_elements(By.CSS_SELECTOR, f"input[name='{name}']")
        if 1 <= idx <= len(radios):
            return radios[idx - 1]
        raise RuntimeError("시간 라디오를 다시 찾지 못했습니다.")

    def _click_slot(self, slot: dict):
        el = self._resolve_slot_element(slot)
        try:
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        except Exception:
            pass
        time.sleep(0.05)

        rid = el.get_attribute("id")
        clicked = False
        if rid:
            labels = self.driver.find_elements(By.CSS_SELECTOR, f"label[for='{rid}']")
            for label in labels[:1]:
                try:
                    label.click()
                    clicked = True
                    break
                except Exception:
                    try:
                        self.driver.execute_script("arguments[0].click();", label)
                        clicked = True
                        break
                    except Exception:
                        pass
        if not clicked:
            try:
                el.click()
                clicked = True
            except Exception:
                pass
        if not clicked:
            try:
                self.driver.execute_script("arguments[0].click();", el)
                clicked = True
            except Exception:
                pass

        # Some Alpensia radios/labels can have zero visual size. The form only
        # needs the radio to be checked before fnSubmit copies its attributes.
        self.driver.execute_script(
            """
            const el = arguments[0];
            el.checked = true;
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
            """,
            el,
        )
        time.sleep(0.1)
        if not el.is_selected():
            raise RuntimeError("시간 라디오 선택 실패")

    def _ensure_agree_checked(self) -> bool:
        selectors = [
            "input#agree-1",
            "input[name='agree-1']",
            "input[type='radio'][name*='agree']",
            "input[type='checkbox'][name*='agree'], input[type='checkbox'][id*='agree']",
        ]
        for selector in selectors:
            els = self.driver.find_elements(By.CSS_SELECTOR, selector)
            if not els:
                continue
            el = els[0]
            if el.is_selected():
                return True
            try:
                self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            except Exception:
                pass
            time.sleep(0.05)
            clicked = False
            try:
                el.click()
                clicked = True
            except Exception:
                eid = el.get_attribute("id")
                if eid:
                    labels = self.driver.find_elements(By.CSS_SELECTOR, f"label[for='{eid}']")
                    if labels:
                        try:
                            labels[0].click()
                            clicked = True
                        except Exception:
                            try:
                                self.driver.execute_script("arguments[0].click();", labels[0])
                                clicked = True
                            except Exception:
                                pass
            if not clicked:
                try:
                    self.driver.execute_script("arguments[0].click();", el)
                    clicked = True
                except Exception:
                    pass
            if not clicked:
                self.driver.execute_script(
                    """
                    const el = arguments[0];
                    el.checked = true;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    """,
                    el,
                )
            time.sleep(0.1)
            if not el.is_selected():
                self.driver.execute_script(
                    """
                    const el = arguments[0];
                    el.checked = true;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    """,
                    el,
                )
                time.sleep(0.05)
            return el.is_selected()
        return False

    def _click_reserve_button(self) -> bool:
        candidates = self.driver.find_elements(By.CSS_SELECTOR, "a.btn.wide")
        btn = None
        for a in candidates:
            text = (a.text or "").strip()
            href = (a.get_attribute("href") or "").strip()
            if text == "예약" or "fnSubmit" in href:
                btn = a
                break
        if btn is None:
            btn = self.driver.find_element(
                By.XPATH,
                "//a[contains(@class,'btn') and contains(@class,'wide') and contains(normalize-space(.),'예약')]",
            )
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        time.sleep(0.1)
        self.driver.execute_script("arguments[0].click();", btn)
        return True

    def _accept_confirm(self, timeout: int = 8) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            self._check_stop()
            try:
                WebDriverWait(self.driver, 1).until(EC.alert_is_present())
                alert = self.driver.switch_to.alert
                text = (alert.text or "").strip()
                compact = text.replace(" ", "")
                if "하시겠습니까" not in compact:
                    self.logger.log(f"[ALERT] {text}")
                    alert.accept()
                    return False
                self.logger.log(f"[CONFIRM] {text}")
                alert.accept()
                time.sleep(0.2)
                return True
            except Exception:
                time.sleep(0.1)
        return False

    def _handle_alerts(self) -> Tuple[Optional[bool], str]:
        messages = []
        verdict: Optional[bool] = None
        for _ in range(3):
            try:
                WebDriverWait(self.driver, 1).until(EC.alert_is_present())
                alert = self.driver.switch_to.alert
                text = (alert.text or "").strip()
                messages.append(text)
                alert.accept()
                compact = text.replace(" ", "")
                if any(k in compact for k in ["완료", "성공", "정상적으로처리"]):
                    verdict = True
                elif any(k in compact for k in ["마감", "매진", "불가", "없습니다", "오류", "실패", "중복"]):
                    verdict = False
            except Exception:
                break
        return verdict, " / ".join(messages)

    def _is_success_page(self) -> bool:
        try:
            url = self.driver.current_url or ""
            if "golfReservationComplete" in url:
                return True
            body = self.driver.find_element(By.TAG_NAME, "body").text
            success_keys = ["예약 완료!", "골프 예약이 완료되었습니다", "예약이 완료되었습니다"]
            return any(k in body for k in success_keys)
        except Exception:
            return False

    def _wait_success(self, timeout: int = 4) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            self._check_stop()
            if self._is_success_page():
                return True
            time.sleep(0.15)
        return False

    def _try_book_slot(self, item: WatchItem, slot: dict, test_mode: bool) -> bool:
        hhmm = fmt_hhmm(int(slot["minutes"]))
        course = slot.get("bookgcoursenm") or slot.get("bookgcourse") or "-"
        self.logger.log(f"[FOUND] {item.ymd} {hhmm} 후보 발견 ({course})")
        self._click_slot(slot)

        if not self._ensure_agree_checked():
            self.logger.log("[WARN] 예약 규약 동의 체크 실패")
            self._save_debug("agree_fail")
            return False

        if test_mode:
            self.logger.log(f"[TEST] {item.ymd} {hhmm} 예약 버튼 클릭 직전까지 확인")
            return True

        self.logger.log(f"[TRY] {item.ymd} {hhmm} 예약 버튼 클릭")
        if not self._click_reserve_button():
            self.logger.log("[WARN] 예약 버튼 클릭 실패")
            self._save_debug("reserve_button_fail")
            return False

        if not self._accept_confirm(timeout=8):
            self.logger.log("[WARN] 예약 confirm 팝업 감지 실패")
            self._save_debug("confirm_fail")
            return False

        if self._wait_success(timeout=4):
            self.logger.log(f"[OK] {item.ymd} {hhmm} 예약 성공")
            self._save_debug("success")
            return True

        verdict, msg = self._handle_alerts()
        if msg:
            self.logger.log(f"[ALERT] {msg}")
        if verdict is True or self._wait_success(timeout=2):
            self.logger.log(f"[OK] {item.ymd} {hhmm} 예약 성공")
            self._save_debug("success")
            return True

        self.logger.log(f"[MISS] {item.ymd} {hhmm} 예약 실패/마감으로 판단")
        self._save_debug("book_fail")
        return False

    def _sleep_interval(self, seconds: int):
        end = time.time() + max(1, seconds)
        while time.time() < end:
            self._check_stop()
            if self.pause_event.is_set():
                self._wait_if_paused()
                if self.resume_now_event.is_set():
                    self.resume_now_event.clear()
                    return
                end = time.time() + max(1, seconds)
            if self.resume_now_event.is_set():
                self.resume_now_event.clear()
                return
            time.sleep(0.2)

    def run(
        self,
        user_id: str,
        password: str,
        get_runtime_settings: Callable[[], Tuple[List[WatchItem], int, bool]],
    ):
        status: Dict[str, str] = {}
        try:
            self.logger.log("=== 알펜시아 취소티 감시 시작 ===")
            self._new_driver()
            self.login(user_id, password)

            watches, interval_sec, test_mode = get_runtime_settings()
            active = [w for w in watches if w.enabled]
            if not active:
                self.logger.log("[WARN] 활성화된 감시 날짜가 없습니다.")
                return
            for w in active:
                status.setdefault(w.ymd, "WATCHING")

            label = "테스트 모드" if test_mode else "실예약 모드"
            self.logger.log(f"[MODE] {label}, 감시 주기 {interval_sec}초, 날짜 {len(active)}개")

            while True:
                self._check_stop()
                self._wait_if_paused()
                watches, interval_sec, test_mode = get_runtime_settings()
                active = [w for w in watches if w.enabled]
                for w in active:
                    status.setdefault(w.ymd, "WATCHING")

                pending = [w for w in active if status.get(w.ymd) != "BOOKED"]
                if not pending:
                    self.logger.log("[DONE] 모든 활성 날짜 예약 성공. 감시를 종료합니다.")
                    return

                for item in pending:
                    self._check_stop()
                    self._wait_if_paused()
                    self.logger.log(f"[CHECK] {item.ymd} {item.start_hhmm}~{item.end_hhmm} 확인")
                    try:
                        self._open_watch_date(item)
                        candidates = self._candidate_slots(item)
                    except Exception as e:
                        self.logger.log(f"[WARN] {item.ymd} 확인 실패: {e}")
                        self._save_debug(f"check_fail_{item.yyyymmdd}")
                        continue

                    if not candidates:
                        self.logger.log(f"[EMPTY] {item.ymd} 조건 내 취소티 없음")
                        continue

                    for slot in candidates:
                        self._check_stop()
                        ok = self._try_book_slot(item, slot, test_mode=test_mode)
                        if ok:
                            if test_mode:
                                self.logger.log(f"[TEST] {item.ymd} 점검 완료. 실제 예약은 하지 않아 감시는 계속됩니다.")
                            else:
                                status[item.ymd] = "BOOKED"
                                self.logger.log(f"[BOOKED] {item.ymd} 감시 종료. 나머지 날짜 감시는 계속합니다.")
                            break
                        self._open_watch_date(item)

                self.logger.log(f"[WAIT] 다음 감시까지 {interval_sec}초 대기")
                self._sleep_interval(interval_sec)

        except RuntimeError as e:
            self.logger.log(f"[STOP] {e}")
        except Exception as e:
            self.logger.log(f"[ERROR] {type(e).__name__}: {e}")
            self._save_debug("fatal_error")
        finally:
            self.close()
            self.logger.log("=== 감시 종료 ===")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self._icon_png = None
        self.title(APP_TITLE)
        self.geometry("1180x720")
        self.configure(bg=APP_BG)
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.resume_now_event = threading.Event()
        self.worker_thread = None
        self.bot: Optional[CancelWatcherBot] = None
        self.saved_accounts: List[dict] = []
        self.runtime_lock = threading.Lock()
        self.runtime_watches: List[WatchItem] = []
        self.runtime_interval_sec = 30
        self.runtime_test_mode = True
        self._cred_error_shown = False
        self._closing = False

        self._apply_window_icon()
        self._apply_style()
        self._build_ui()
        self._load_credentials()
        self._load_config()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _apply_window_icon(self):
        ico_path = _first_existing_resource(ICON_FILENAME)
        try:
            if ico_path:
                self.iconbitmap(default=ico_path)
        except Exception:
            pass
        try:
            png_path = _first_existing_resource(LOGO_FILENAME)
            if png_path:
                self._icon_png = tk.PhotoImage(file=png_path)
                self.iconphoto(True, self._icon_png)
        except Exception:
            pass

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
            style.configure(".", background=APP_BG)
            style.configure("TFrame", background=APP_BG)
            style.configure("TLabel", background=APP_BG)
            style.configure("TLabelframe", background=APP_BG)
            style.configure("TLabelframe.Label", background=APP_BG)
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
        ttk.Label(header, text=APP_TITLE, font=("맑은 고딕", 20, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(header, text=f"v{APP_VERSION}", foreground="gray").grid(row=1, column=0, sticky="w")

        lf_login = ttk.LabelFrame(left, text="로그인 정보", padding=10)
        lf_login.grid(row=1, column=0, sticky="ew", pady=(0, 10))

        ttk.Label(lf_login, text="아이디").grid(row=0, column=0, sticky="w")
        self.ent_id = ttk.Combobox(lf_login, width=26)
        self.ent_id.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.ent_id.bind("<<ComboboxSelected>>", self._on_id_selected)
        self.ent_id.bind("<FocusOut>", self._on_id_focus_out)
        self.btn_delete_id = ttk.Button(lf_login, text="삭제", width=6, command=self._delete_selected_account)
        self.btn_delete_id.grid(row=0, column=2, padx=(6, 0))

        ttk.Label(lf_login, text="비밀번호").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.ent_pw = tk.Entry(lf_login, width=28, bg=WHITE, show="*")
        self.ent_pw.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(8, 0))

        self.var_show_pw = tk.BooleanVar(value=False)
        ttk.Checkbutton(lf_login, text="비밀번호 표시", variable=self.var_show_pw, command=self._toggle_pw).grid(
            row=2, column=1, sticky="w", pady=(8, 0)
        )
        self.var_remember = tk.BooleanVar(value=True)
        ttk.Checkbutton(lf_login, text="아이디/비밀번호 기억", variable=self.var_remember).grid(
            row=3, column=1, sticky="w", pady=(6, 0)
        )

        lf_settings = ttk.LabelFrame(left, text="취소티 감시 설정", padding=10)
        lf_settings.grid(row=2, column=0, sticky="ew", pady=(0, 10))

        ttk.Label(lf_settings, text="골프장").grid(row=0, column=0, sticky="w")
        ttk.Label(lf_settings, text="알펜시아 700 G.C", font=("맑은 고딕", 9, "bold")).grid(
            row=0, column=1, sticky="w", padx=(8, 0), columnspan=5
        )

        ttk.Label(lf_settings, text="감시 주기").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.var_interval = tk.StringVar(value="30초")
        self.cb_interval = ttk.Combobox(lf_settings, width=8, state="readonly", values=["10초", "30초", "5분"], textvariable=self.var_interval)
        self.cb_interval.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(8, 0))

        self.var_test_mode = tk.BooleanVar(value=True)
        ttk.Checkbutton(lf_settings, text="테스트 모드 (예약 버튼 클릭 직전까지만)", variable=self.var_test_mode).grid(
            row=2, column=1, columnspan=5, sticky="w", padx=(8, 0), pady=(8, 0)
        )

        lf_watch = ttk.LabelFrame(left, text="감시 날짜 (최대 3개)", padding=10)
        lf_watch.grid(row=3, column=0, sticky="ew", pady=(0, 10))

        time_opts = [fmt_hhmm(m) for m in range(5 * 60, 19 * 60 + 1, 10)]
        self.watch_vars = []
        self.watch_widgets = []

        ttk.Label(lf_watch, text="사용").grid(row=0, column=0, sticky="w")
        ttk.Label(lf_watch, text="날짜").grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Label(lf_watch, text="시작").grid(row=0, column=3, sticky="w", padx=(8, 0))
        ttk.Label(lf_watch, text="종료").grid(row=0, column=4, sticky="w", padx=(8, 0))

        defaults = ["2026-08-01", "2026-08-02", ""]
        for i in range(WATCH_COUNT):
            use_var = tk.BooleanVar(value=i < 2)
            date_var = tk.StringVar(value=defaults[i])
            start_var = tk.StringVar(value="06:00")
            end_var = tk.StringVar(value="14:00")

            chk = ttk.Checkbutton(lf_watch, variable=use_var, command=lambda idx=i: self._apply_watch_enabled_state(idx))
            chk.grid(row=i + 1, column=0, sticky="w", pady=(4, 0))

            ent_date = tk.Entry(lf_watch, width=12, bg=WHITE, textvariable=date_var, state="readonly", readonlybackground=WHITE)
            ent_date.grid(row=i + 1, column=1, sticky="w", padx=(8, 4), pady=(4, 0))

            def open_calendar(_evt=None, v=date_var):
                DatePicker(self, v.get(), lambda ymd: v.set(ymd))

            ent_date.bind("<Button-1>", open_calendar)
            btn_cal = ttk.Button(lf_watch, text="달력", width=5, command=open_calendar)
            btn_cal.grid(row=i + 1, column=2, sticky="w", pady=(4, 0))

            cb_start = ttk.Combobox(lf_watch, width=7, state="readonly", values=time_opts, textvariable=start_var)
            cb_start.grid(row=i + 1, column=3, sticky="w", padx=(8, 0), pady=(4, 0))
            cb_end = ttk.Combobox(lf_watch, width=7, state="readonly", values=time_opts, textvariable=end_var)
            cb_end.grid(row=i + 1, column=4, sticky="w", padx=(8, 0), pady=(4, 0))

            self.watch_vars.append((use_var, date_var, start_var, end_var))
            self.watch_widgets.append((chk, ent_date, btn_cal, cb_start, cb_end))
            self._apply_watch_enabled_state(i)

        btn_frame = ttk.Frame(left)
        btn_frame.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        for c in range(3):
            btn_frame.columnconfigure(c, weight=1)

        self.btn_start = ttk.Button(btn_frame, text="감시 시작", command=self.start)
        self.btn_start.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        self.btn_pause = ttk.Button(btn_frame, text="일시중지", command=self.toggle_pause, state="disabled")
        self.btn_pause.grid(row=0, column=1, sticky="ew", padx=5)
        self.btn_stop = ttk.Button(btn_frame, text="완전 중단", command=self.stop, state="disabled")
        self.btn_stop.grid(row=0, column=2, sticky="ew", padx=(5, 0))

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
            text="성공한 날짜는 감시 종료, 아직 성공하지 않은 날짜는 계속 감시합니다. ALPS/ASIA는 구분하지 않습니다.",
        )
        self.lbl_tip.grid(row=1, column=0, sticky="w", pady=(8, 0))

    def _apply_watch_enabled_state(self, index: int):
        enabled = bool(self.watch_vars[index][0].get())
        _chk, ent_date, btn_cal, cb_start, cb_end = self.watch_widgets[index]
        ent_date.configure(state="readonly" if enabled else "disabled")
        btn_cal.configure(state="normal" if enabled else "disabled")
        cb_start.configure(state="readonly" if enabled else "disabled")
        cb_end.configure(state="readonly" if enabled else "disabled")

    def _toggle_pw(self):
        self.ent_pw.configure(show="" if self.var_show_pw.get() else "*")

    def _refresh_id_dropdown(self):
        ids = [acc["user_id"] for acc in self.saved_accounts if acc.get("user_id")]
        self.ent_id.configure(values=ids)
        self.btn_delete_id.configure(state="normal" if ids else "disabled")

    def _find_saved_account(self, user_id: str) -> Optional[dict]:
        uid = user_id.strip()
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
        messagebox.showwarning("계정 저장", msg)

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
                if uid:
                    loaded.append({"user_id": uid, "password": pw, "updated_at": int(row.get("updated_at", 0))})
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
        if not messagebox.askyesno("계정 삭제", f"'{uid}' 계정을 저장 목록에서 삭제할까요?"):
            return
        try:
            self.saved_accounts = [acc for acc in self.saved_accounts if acc.get("user_id") != uid]
            self._persist_credentials()
            self._refresh_id_dropdown()
            self.ent_id.set("")
            self.ent_pw.delete(0, "end")
        except Exception:
            self._show_cred_warn_once("저장 계정 삭제에 실패했습니다.")

    def _load_config(self):
        if self.saved_accounts:
            first = self.saved_accounts[0].get("user_id", "")
            if first:
                self.ent_id.set(first)
                self._autofill_password_for_id(first)
        if not os.path.exists(CONFIG_PATH):
            return
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            self.var_remember.set(cfg.get("remember", True))
            self.var_interval.set(cfg.get("interval_label", "30초"))
            self.var_test_mode.set(bool(cfg.get("test_mode", True)))
            uid = cfg.get("user_id", "").strip()
            if uid:
                self.ent_id.set(uid)
                self._autofill_password_for_id(uid)
            watches = cfg.get("watches", [])
            for i in range(min(WATCH_COUNT, len(watches))):
                row = watches[i]
                self.watch_vars[i][0].set(bool(row.get("enabled", i < 2)))
                self.watch_vars[i][1].set(row.get("ymd", self.watch_vars[i][1].get()))
                self.watch_vars[i][2].set(row.get("start_hhmm", "06:00"))
                self.watch_vars[i][3].set(row.get("end_hhmm", "14:00"))
                self._apply_watch_enabled_state(i)
        except Exception:
            pass

    def _save_config(self):
        watches = []
        for use_var, date_var, start_var, end_var in self.watch_vars:
            watches.append({
                "enabled": bool(use_var.get()),
                "ymd": date_var.get().strip(),
                "start_hhmm": start_var.get().strip(),
                "end_hhmm": end_var.get().strip(),
            })
        cfg = {
            "user_id": self.ent_id.get().strip(),
            "remember": bool(self.var_remember.get()),
            "interval_label": self.var_interval.get(),
            "test_mode": bool(self.var_test_mode.get()),
            "watches": watches,
        }
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _interval_seconds(self) -> int:
        label = self.var_interval.get()
        if label == "10초":
            return 10
        if label == "5분":
            return 300
        return 30

    def _collect_watches(self) -> List[WatchItem]:
        watches = []
        for use_var, date_var, start_var, end_var in self.watch_vars:
            watches.append(WatchItem(
                enabled=bool(use_var.get()),
                ymd=date_var.get().strip(),
                start_hhmm=start_var.get().strip(),
                end_hhmm=end_var.get().strip(),
            ))
        return watches

    def _validate_inputs(self, watches: List[WatchItem]) -> bool:
        active = [w for w in watches if w.enabled]
        if not active:
            messagebox.showwarning("입력 확인", "감시할 날짜를 하나 이상 선택해 주세요.")
            return False
        seen = set()
        for w in active:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", w.ymd or ""):
                messagebox.showwarning("입력 확인", f"날짜 형식이 올바르지 않습니다: {w.ymd}")
                return False
            if w.ymd in seen:
                messagebox.showwarning("입력 확인", f"중복 날짜가 있습니다: {w.ymd}")
                return False
            seen.add(w.ymd)
            start = hhmm_to_minutes(w.start_hhmm)
            end = hhmm_to_minutes(w.end_hhmm)
            if start is None or end is None or start > end:
                messagebox.showwarning("입력 확인", f"시간 범위가 올바르지 않습니다: {w.ymd}")
                return False
        return True

    def _set_runtime_settings(self, watches: List[WatchItem], interval_sec: int, test_mode: bool):
        copied = [
            WatchItem(
                enabled=w.enabled,
                ymd=w.ymd,
                start_hhmm=w.start_hhmm,
                end_hhmm=w.end_hhmm,
            )
            for w in watches
        ]
        with self.runtime_lock:
            self.runtime_watches = copied
            self.runtime_interval_sec = interval_sec
            self.runtime_test_mode = test_mode

    def _get_runtime_settings(self) -> Tuple[List[WatchItem], int, bool]:
        with self.runtime_lock:
            watches = [
                WatchItem(
                    enabled=w.enabled,
                    ymd=w.ymd,
                    start_hhmm=w.start_hhmm,
                    end_hhmm=w.end_hhmm,
                )
                for w in self.runtime_watches
            ]
            return watches, self.runtime_interval_sec, self.runtime_test_mode

    def _refresh_runtime_settings_from_ui(self) -> bool:
        watches = self._collect_watches()
        if not self._validate_inputs(watches):
            return False
        self._set_runtime_settings(watches, self._interval_seconds(), bool(self.var_test_mode.get()))
        self._save_config()
        return True

    def start(self):
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("안내", "이미 감시 중입니다.")
            return

        user_id = self.ent_id.get().strip()
        password = self.ent_pw.get().strip()
        if user_id and not password:
            self._autofill_password_for_id(user_id)
            password = self.ent_pw.get().strip()
        if not user_id or not password:
            messagebox.showwarning("입력 확인", "아이디/비밀번호를 입력해 주세요.")
            return

        watches = self._collect_watches()
        if not self._validate_inputs(watches):
            return

        if not self.var_test_mode.get():
            ok = messagebox.askyesno(
                "실예약 모드 확인",
                "실예약 모드입니다. 조건에 맞는 취소티가 나오면 예약 버튼까지 자동 진행합니다.\n계속할까요?",
            )
            if not ok:
                return

        self._save_config()
        self._set_runtime_settings(watches, self._interval_seconds(), bool(self.var_test_mode.get()))
        if self.var_remember.get():
            self._remember_credentials(user_id, password)

        self.stop_event.clear()
        self.pause_event.clear()
        self.btn_start.configure(state="disabled")
        self.btn_pause.configure(state="normal", text="일시중지")
        self.btn_stop.configure(state="normal")

        debug_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cancel_watcher_debug")
        self.resume_now_event.clear()
        self.bot = CancelWatcherBot(self.logger, self.stop_event, self.pause_event, self.resume_now_event, debug_dir)

        self.worker_thread = threading.Thread(
            target=self._run_worker,
            args=(user_id, password),
            daemon=True,
        )
        self.worker_thread.start()

    def _run_worker(self, user_id: str, password: str):
        try:
            assert self.bot is not None
            self.bot.run(user_id, password, self._get_runtime_settings)
        finally:
            self.after(0, self._reset_buttons)

    def toggle_pause(self):
        if not (self.worker_thread and self.worker_thread.is_alive()):
            return
        if self.pause_event.is_set():
            if not self._refresh_runtime_settings_from_ui():
                return
            self.pause_event.clear()
            self.resume_now_event.set()
            self.btn_pause.configure(text="일시중지")
            watches, interval_sec, test_mode = self._get_runtime_settings()
            active_count = len([w for w in watches if w.enabled])
            label = "테스트 모드" if test_mode else "실예약 모드"
            self.logger.log(f"[RESUME] 감시 재개 ({label}, {interval_sec}초, 날짜 {active_count}개)")
        else:
            self.pause_event.set()
            self.btn_pause.configure(text="감시 재개")
            self.logger.log("[PAUSE] 현재 예약 시도가 끝난 뒤 일시중지됩니다.")

    def stop(self):
        self.stop_event.set()
        self.pause_event.clear()
        self.resume_now_event.set()
        self.logger.log("[STOP] 완전 중단 요청")
        self.btn_stop.configure(state="disabled")
        self.btn_pause.configure(state="disabled")

    def _reset_buttons(self):
        self.btn_start.configure(state="normal")
        self.btn_pause.configure(state="disabled", text="일시중지")
        self.btn_stop.configure(state="disabled")

    def _on_close(self):
        if self.worker_thread and self.worker_thread.is_alive():
            if not messagebox.askyesno("종료 확인", "감시가 실행 중입니다. 중단하고 종료할까요?"):
                return
            self.stop_event.set()
            self.pause_event.clear()
            self.resume_now_event.set()
        self.destroy()


if __name__ == "__main__":
    app = App()
    app.mainloop()
