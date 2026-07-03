import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tkinter as tk
import zipfile
from tkinter import messagebox
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


LAUNCHER_VERSION = "1.2.1"
APP_EXE_NAME = "Alpensia.exe"
CANCEL_EXE_NAME = "Alpensia_CancelWatcher.exe"
LOCAL_VERSION_FILE = "app_version.json"
LOCAL_CANCEL_VERSION_FILE = "cancel_watcher_version.json"
CONFIG_FILE = "launcher_config.json"

DEFAULT_CONFIG = {
    "github_owner": "claire8201-web",
    "github_repo": "Alpensia",
    "app_asset_name": APP_EXE_NAME,
    "version_asset_name": "version.json",
    "cancel_watcher_asset_name": "Alpensia_CancelWatcher_v1.0.1.zip",
    "cancel_watcher_exe_name": CANCEL_EXE_NAME,
    "launch_args": [],
}


def base_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def config_path() -> str:
    return os.path.join(base_dir(), CONFIG_FILE)


def local_app_path() -> str:
    return os.path.join(base_dir(), APP_EXE_NAME)


def local_cancel_path(cfg: dict) -> str:
    exe_name = str(cfg.get("cancel_watcher_exe_name") or CANCEL_EXE_NAME)
    return os.path.join(base_dir(), exe_name)


def local_version_path() -> str:
    return os.path.join(base_dir(), LOCAL_VERSION_FILE)


def local_cancel_version_path() -> str:
    return os.path.join(base_dir(), LOCAL_CANCEL_VERSION_FILE)


def temp_download_dir() -> str:
    path = os.path.join(base_dir(), "_update_tmp")
    os.makedirs(path, exist_ok=True)
    return path


def load_config() -> dict:
    data = dict(DEFAULT_CONFIG)
    path = config_path()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8-sig") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            data.update(loaded)
    return data


def ensure_config_ready(cfg: dict) -> None:
    owner = str(cfg.get("github_owner", "")).strip()
    repo = str(cfg.get("github_repo", "")).strip()
    if not owner or not repo or owner == "REPLACE_ME" or repo == "REPLACE_ME":
        raise RuntimeError("launcher_config.json에 GitHub 저장소 정보를 입력해야 합니다.")


def request_headers() -> dict:
    return {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"AlpensiaLauncher/{LAUNCHER_VERSION}",
    }


def api_get_json(url: str) -> dict:
    req = Request(url, headers=request_headers())
    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8-sig"))


def download_bytes(url: str) -> bytes:
    req = Request(url, headers={"User-Agent": f"AlpensiaLauncher/{LAUNCHER_VERSION}"})
    with urlopen(req, timeout=120) as resp:
        return resp.read()


def latest_release(cfg: dict) -> dict:
    return api_get_json(
        f"https://api.github.com/repos/{cfg['github_owner']}/{cfg['github_repo']}/releases/latest"
    )


def release_assets(release: dict) -> dict:
    return {asset.get("name", ""): asset for asset in release.get("assets", [])}


def parse_version_parts(version: str) -> tuple:
    parts = []
    for chunk in str(version).strip().lstrip("vV").split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            digits = "".join(ch for ch in chunk if ch.isdigit())
            parts.append(int(digits) if digits else 0)
    return tuple(parts)


def is_update_needed(local_version: str, remote_version: str) -> bool:
    return parse_version_parts(remote_version) > parse_version_parts(local_version)


def read_version_file(path: str) -> str:
    if not os.path.exists(path):
        return "0.0.0"
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            payload = json.load(f)
        return str(payload.get("version", "0.0.0"))
    except Exception:
        return "0.0.0"


def write_version_file(path: str, payload: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def read_local_version() -> str:
    return read_version_file(local_version_path())


def read_local_cancel_version() -> str:
    return read_version_file(local_cancel_version_path())


def write_local_version(payload: dict) -> None:
    write_version_file(local_version_path(), payload)


def write_local_cancel_version(payload: dict) -> None:
    write_version_file(local_cancel_version_path(), payload)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def find_cancel_watcher_asset(assets: dict, preferred_name: str) -> dict:
    preferred = assets.get(preferred_name)
    if preferred:
        return preferred

    candidates = []
    for name, asset in assets.items():
        lower_name = name.lower()
        if lower_name.startswith("alpensia_cancelwatcher") and lower_name.endswith(".zip"):
            candidates.append((name, asset))
    if not candidates:
        raise RuntimeError(f"GitHub Release 자산에서 {preferred_name} 파일을 찾지 못했습니다.")
    candidates.sort(key=lambda item: parse_version_parts(item[0]), reverse=True)
    return candidates[0][1]


def fetch_release_info(cfg: dict) -> dict:
    release = latest_release(cfg)
    assets = release_assets(release)

    version_asset_name = str(cfg.get("version_asset_name", "version.json"))
    version_asset = assets.get(version_asset_name)
    if not version_asset:
        raise RuntimeError(f"GitHub Release 자산에서 {version_asset_name} 파일을 찾지 못했습니다.")

    version_payload = json.loads(download_bytes(version_asset["browser_download_url"]).decode("utf-8-sig"))
    version = str(version_payload.get("version", "")).strip()
    if not version:
        raise RuntimeError("version.json에 version 값이 없습니다.")

    app_asset_name = str(version_payload.get("asset_name") or cfg.get("app_asset_name") or APP_EXE_NAME)
    app_asset = assets.get(app_asset_name)
    if not app_asset:
        raise RuntimeError(f"GitHub Release 자산에서 앱 파일 {app_asset_name}을 찾지 못했습니다.")

    cancel_asset_name = str(
        version_payload.get("cancel_watcher_asset_name")
        or cfg.get("cancel_watcher_asset_name")
        or "Alpensia_CancelWatcher_v1.0.1.zip"
    )
    cancel_asset = find_cancel_watcher_asset(assets, cancel_asset_name)
    cancel_version = str(version_payload.get("cancel_watcher_version") or "1.0.1").strip()

    return {
        "version": version,
        "notes": str(version_payload.get("notes", "")).strip(),
        "sha256": str(version_payload.get("sha256", "")).strip().lower(),
        "download_url": app_asset["browser_download_url"],
        "cancel_watcher_version": cancel_version,
        "cancel_watcher_sha256": str(version_payload.get("cancel_watcher_sha256", "")).strip().lower(),
        "cancel_watcher_download_url": cancel_asset["browser_download_url"],
        "raw_version_payload": version_payload,
    }


def download_release_binary(url: str, expected_sha256: str = "", suffix: str = ".exe") -> str:
    data = download_bytes(url)
    if expected_sha256:
        actual = sha256_bytes(data)
        if actual.lower() != expected_sha256.lower():
            raise RuntimeError("다운로드한 파일의 SHA256 검증에 실패했습니다.")

    fd, temp_path = tempfile.mkstemp(prefix="alpensia_update_", suffix=suffix, dir=temp_download_dir())
    os.close(fd)
    with open(temp_path, "wb") as f:
        f.write(data)
    return temp_path


def replace_file(temp_path: str, target: str, app_label: str) -> None:
    backup = target + ".bak"
    for _ in range(3):
        try:
            if os.path.exists(backup):
                os.remove(backup)
            if os.path.exists(target):
                os.replace(target, backup)
            os.replace(temp_path, target)
            if os.path.exists(backup):
                os.remove(backup)
            return
        except PermissionError:
            retry = messagebox.askretrycancel(
                "업데이트 대기",
                f"{app_label}이 실행 중이라 파일을 교체할 수 없습니다.\n"
                "프로그램을 종료한 뒤 다시 시도해 주세요.",
            )
            if not retry:
                raise RuntimeError("업데이트가 취소되었습니다.")
            time.sleep(1.0)
        except Exception:
            if os.path.exists(backup) and not os.path.exists(target):
                os.replace(backup, target)
            raise
    raise RuntimeError("파일 교체에 실패했습니다.")


def launch_exe(path: str, args: Optional[list] = None) -> None:
    if not os.path.exists(path):
        raise RuntimeError(f"실행 파일을 찾지 못했습니다: {os.path.basename(path)}")
    command = [path]
    if isinstance(args, list):
        command.extend(str(x) for x in args)
    subprocess.Popen(command, cwd=base_dir())


def launch_app(cfg: dict) -> None:
    launch_exe(local_app_path(), cfg.get("launch_args", []))


def launch_cancel_watcher(cfg: dict) -> None:
    launch_exe(local_cancel_path(cfg))


def install_or_update_reservation(cfg: dict, remote: dict) -> bool:
    app_exists = os.path.exists(local_app_path())
    local_version = read_local_version()
    if app_exists and not is_update_needed(local_version, remote["version"]):
        return False

    temp_path = download_release_binary(remote["download_url"], remote.get("sha256", ""), suffix=".exe")
    try:
        replace_file(temp_path, local_app_path(), "알펜시아 예약 프로그램")
        write_local_version(remote["raw_version_payload"])
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
    return True


def install_or_update_cancel_watcher(cfg: dict, remote: dict) -> bool:
    target = local_cancel_path(cfg)
    app_exists = os.path.exists(target)
    local_version = read_local_cancel_version()
    remote_version = remote["cancel_watcher_version"]
    if app_exists and not is_update_needed(local_version, remote_version):
        return False

    zip_path = download_release_binary(
        remote["cancel_watcher_download_url"],
        remote.get("cancel_watcher_sha256", ""),
        suffix=".zip",
    )
    extract_dir = tempfile.mkdtemp(prefix="alpensia_cancel_", dir=temp_download_dir())
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)

        exe_name = str(cfg.get("cancel_watcher_exe_name") or CANCEL_EXE_NAME)
        extracted = os.path.join(extract_dir, exe_name)
        if not os.path.exists(extracted):
            matches = []
            for root, _dirs, files in os.walk(extract_dir):
                for name in files:
                    if name.lower().endswith(".exe"):
                        matches.append(os.path.join(root, name))
            if not matches:
                raise RuntimeError("압축 파일 안에서 취소티 감시 EXE를 찾지 못했습니다.")
            extracted = matches[0]

        temp_exe = os.path.join(temp_download_dir(), exe_name + ".tmp")
        if os.path.exists(temp_exe):
            os.remove(temp_exe)
        shutil.copy2(extracted, temp_exe)
        replace_file(temp_exe, target, "알펜시아 취소티 감시")
        write_local_cancel_version({"version": remote_version})
    finally:
        if os.path.exists(zip_path):
            os.remove(zip_path)
        shutil.rmtree(extract_dir, ignore_errors=True)
    return True


def run_with_online_update(cfg: dict, updater, launcher, fallback_path: str, missing_message: str) -> None:
    try:
        ensure_config_ready(cfg)
        remote = fetch_release_info(cfg)
        updater(cfg, remote)
        launcher(cfg)
    except (HTTPError, URLError) as e:
        if os.path.exists(fallback_path):
            messagebox.showwarning(
                "업데이트 확인 실패",
                f"GitHub Releases 확인에 실패했습니다.\n기존 프로그램을 실행합니다.\n\n사유: {e}",
            )
            launcher(cfg)
            return
        messagebox.showerror("실행 실패", f"{missing_message}\n\n사유: {e}")
    except Exception as e:
        if os.path.exists(fallback_path):
            answer = messagebox.askyesno(
                "업데이트 오류",
                f"업데이트 처리 중 문제가 생겼습니다.\n기존 프로그램을 실행할까요?\n\n사유: {e}",
            )
            if answer:
                launcher(cfg)
                return
        messagebox.showerror("실행 실패", str(e))


def run_reservation_app(cfg: dict) -> None:
    run_with_online_update(
        cfg,
        install_or_update_reservation,
        launch_app,
        local_app_path(),
        "예약 프로그램 파일을 찾지 못했고 업데이트 파일도 가져오지 못했습니다.",
    )


def run_cancel_watcher(cfg: dict) -> None:
    run_with_online_update(
        cfg,
        install_or_update_cancel_watcher,
        launch_cancel_watcher,
        local_cancel_path(cfg),
        "취소티 감시 프로그램 파일을 찾지 못했고 업데이트 파일도 가져오지 못했습니다.",
    )


def build_menu(root: tk.Tk, cfg: dict) -> None:
    root.title("알펜시아 런처")
    root.geometry("430x240")
    root.resizable(False, False)

    frame = tk.Frame(root, padx=22, pady=18)
    frame.pack(fill="both", expand=True)

    title = tk.Label(frame, text="알펜시아 런처", font=("맑은 고딕", 18, "bold"))
    title.pack(anchor="w")
    subtitle = tk.Label(frame, text="예약 프로그램과 취소티 감시 실행 / 업데이트", fg="gray")
    subtitle.pack(anchor="w", pady=(2, 16))

    def run_and_close(fn):
        root.withdraw()
        try:
            fn(cfg)
        finally:
            root.destroy()

    btn_reserve = tk.Button(
        frame,
        text="예약 프로그램 실행 / 업데이트",
        height=2,
        command=lambda: run_and_close(run_reservation_app),
    )
    btn_reserve.pack(fill="x", pady=(0, 10))

    btn_cancel = tk.Button(
        frame,
        text="취소티 감시 프로그램 실행 / 업데이트",
        height=2,
        command=lambda: run_and_close(run_cancel_watcher),
    )
    btn_cancel.pack(fill="x", pady=(0, 10))

    btn_close = tk.Button(frame, text="닫기", command=root.destroy)
    btn_close.pack(anchor="e", pady=(6, 0))


def main() -> None:
    cfg = load_config()
    root = tk.Tk()
    build_menu(root, cfg)
    root.mainloop()


if __name__ == "__main__":
    main()
