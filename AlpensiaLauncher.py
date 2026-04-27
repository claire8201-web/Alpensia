import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import tkinter as tk
from tkinter import messagebox
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


LAUNCHER_VERSION = "1.0.0"
APP_EXE_NAME = "Alpensia.exe"
LOCAL_VERSION_FILE = "app_version.json"
CONFIG_FILE = "launcher_config.json"
DEFAULT_CONFIG = {
    "github_owner": "REPLACE_ME",
    "github_repo": "REPLACE_ME",
    "app_asset_name": APP_EXE_NAME,
    "version_asset_name": "version.json",
    "launch_args": [],
    "allow_prerelease": False,
}


def base_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def config_path() -> str:
    return os.path.join(base_dir(), CONFIG_FILE)


def local_app_path() -> str:
    return os.path.join(base_dir(), APP_EXE_NAME)


def local_version_path() -> str:
    return os.path.join(base_dir(), LOCAL_VERSION_FILE)


def temp_download_dir() -> str:
    path = os.path.join(base_dir(), "_update_tmp")
    os.makedirs(path, exist_ok=True)
    return path


def load_config() -> dict:
    data = dict(DEFAULT_CONFIG)
    path = config_path()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            data.update(loaded)
    return data


def ensure_config_ready(cfg: dict) -> None:
    owner = str(cfg.get("github_owner", "")).strip()
    repo = str(cfg.get("github_repo", "")).strip()
    if not owner or not repo or owner == "REPLACE_ME" or repo == "REPLACE_ME":
        raise RuntimeError(
            "launcher_config.json에 GitHub 저장소 정보를 먼저 입력해야 합니다.\n"
            "예: github_owner, github_repo"
        )


def api_get_json(url: str) -> dict:
    req = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"AlpensiaLauncher/{LAUNCHER_VERSION}",
        },
    )
    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def download_bytes(url: str) -> bytes:
    req = Request(url, headers={"User-Agent": f"AlpensiaLauncher/{LAUNCHER_VERSION}"})
    with urlopen(req, timeout=60) as resp:
        return resp.read()


def parse_version_parts(version: str) -> tuple:
    parts = []
    for chunk in str(version).strip().lstrip("vV").split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            digits = "".join(ch for ch in chunk if ch.isdigit())
            parts.append(int(digits) if digits else 0)
    return tuple(parts)


def read_local_version() -> str:
    path = local_version_path()
    if not os.path.exists(path):
        return "0.0.0"
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        return str(payload.get("version", "0.0.0"))
    except Exception:
        return "0.0.0"


def write_local_version(payload: dict) -> None:
    with open(local_version_path(), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch_release_info(cfg: dict) -> dict:
    latest = api_get_json(
        f"https://api.github.com/repos/{cfg['github_owner']}/{cfg['github_repo']}/releases/latest"
    )
    assets = {asset.get("name", ""): asset for asset in latest.get("assets", [])}

    version_asset_name = str(cfg.get("version_asset_name", "version.json"))
    version_asset = assets.get(version_asset_name)
    if not version_asset:
        raise RuntimeError(
            f"GitHub Release 자산에서 {version_asset_name} 파일을 찾지 못했습니다."
        )

    version_payload = json.loads(
        download_bytes(version_asset["browser_download_url"]).decode("utf-8")
    )
    version = str(version_payload.get("version", "")).strip()
    if not version:
        raise RuntimeError("version.json에 version 값이 없습니다.")

    app_asset_name = str(
        version_payload.get("asset_name") or cfg.get("app_asset_name") or APP_EXE_NAME
    )
    app_asset = assets.get(app_asset_name)
    if not app_asset:
        raise RuntimeError(
            f"GitHub Release 자산에서 앱 파일 {app_asset_name} 을(를) 찾지 못했습니다."
        )

    return {
        "version": version,
        "notes": str(version_payload.get("notes", "")).strip(),
        "sha256": str(version_payload.get("sha256", "")).strip().lower(),
        "download_url": app_asset["browser_download_url"],
        "published_at": latest.get("published_at", ""),
        "release_name": latest.get("name") or latest.get("tag_name") or version,
        "raw_version_payload": version_payload,
    }


def is_update_needed(local_version: str, remote_version: str) -> bool:
    return parse_version_parts(remote_version) > parse_version_parts(local_version)


def download_release_binary(url: str, expected_sha256: str) -> str:
    data = download_bytes(url)
    if expected_sha256:
        actual = sha256_bytes(data)
        if actual.lower() != expected_sha256.lower():
            raise RuntimeError("다운로드한 파일의 SHA256 검증에 실패했습니다.")

    fd, temp_path = tempfile.mkstemp(
        prefix="alpensia_update_",
        suffix=".exe",
        dir=temp_download_dir(),
    )
    os.close(fd)
    with open(temp_path, "wb") as f:
        f.write(data)
    return temp_path


def replace_app_binary(temp_path: str) -> None:
    target = local_app_path()
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
            answer = messagebox.askretrycancel(
                "업데이트 대기",
                "기존 프로그램이 실행 중이라 파일을 교체할 수 없습니다.\n"
                "알펜시아 예약 프로그램을 모두 닫은 뒤 [다시 시도]를 눌러주세요.",
            )
            if not answer:
                raise RuntimeError("사용자가 업데이트를 취소했습니다.")
            time.sleep(1.0)
        except Exception:
            if os.path.exists(backup) and not os.path.exists(target):
                os.replace(backup, target)
            raise

    raise RuntimeError("앱 파일 교체에 실패했습니다.")


def launch_app(cfg: dict) -> None:
    app_path = local_app_path()
    if not os.path.exists(app_path):
        raise RuntimeError(f"{APP_EXE_NAME} 파일을 찾지 못했습니다.")
    args = [app_path]
    extra_args = cfg.get("launch_args", [])
    if isinstance(extra_args, list):
        args.extend(str(x) for x in extra_args)
    subprocess.Popen(args, cwd=base_dir())


def format_release_message(info: dict) -> str:
    notes = info.get("notes", "").strip()
    if not notes:
        return f"새 버전 {info['version']} 이(가) 있습니다.\n지금 업데이트할까요?"
    return (
        f"새 버전 {info['version']} 이(가) 있습니다.\n\n"
        f"변경사항:\n{notes}\n\n"
        "지금 업데이트할까요?"
    )


def install_or_update(cfg: dict, remote: dict, local_version: str) -> bool:
    app_exists = os.path.exists(local_app_path())
    needs_update = (not app_exists) or is_update_needed(local_version, remote["version"])
    if not needs_update:
        return False

    if app_exists:
        if not messagebox.askyesno("업데이트", format_release_message(remote)):
            return False
    else:
        messagebox.showinfo(
            "초기 설치",
            f"최신 버전 {remote['version']} 을(를) 내려받아 설치합니다.",
        )

    temp_path = download_release_binary(remote["download_url"], remote.get("sha256", ""))
    try:
        replace_app_binary(temp_path)
        write_local_version(remote["raw_version_payload"])
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

    messagebox.showinfo("업데이트 완료", f"버전 {remote['version']} 적용이 완료되었습니다.")
    return True


def main() -> None:
    root = tk.Tk()
    root.withdraw()

    cfg = load_config()
    try:
        ensure_config_ready(cfg)
    except Exception as e:
        messagebox.showerror("런처 설정 필요", str(e))
        return

    try:
        local_version = read_local_version()
        remote = fetch_release_info(cfg)
        install_or_update(cfg, remote, local_version)
        launch_app(cfg)
    except (HTTPError, URLError) as e:
        if os.path.exists(local_app_path()):
            messagebox.showwarning(
                "업데이트 확인 실패",
                f"GitHub Releases 확인에 실패했습니다.\n기존 버전을 실행합니다.\n\n사유: {e}",
            )
            launch_app(cfg)
            return
        messagebox.showerror(
            "실행 실패",
            "최초 설치에 필요한 파일을 가져오지 못했습니다.\n"
            f"네트워크 상태를 확인해주세요.\n\n사유: {e}",
        )
    except Exception as e:
        if os.path.exists(local_app_path()):
            answer = messagebox.askyesno(
                "업데이트 오류",
                f"업데이트 처리 중 문제가 생겼습니다.\n기존 버전을 실행할까요?\n\n사유: {e}",
            )
            if answer:
                launch_app(cfg)
                return
        messagebox.showerror("실행 실패", str(e))
    finally:
        try:
            root.destroy()
        except Exception:
            pass


if __name__ == "__main__":
    main()
