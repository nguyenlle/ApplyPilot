"""Owned Chrome processes with persistent, exclusively locked automation profiles."""

import json
import logging
import os
import platform
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

from applypilot import config

logger = logging.getLogger(__name__)
BASE_CDP_PORT = 9222
_chrome_procs: dict[int, subprocess.Popen] = {}
_chrome_ports: dict[int, int] = {}
_profile_locks: dict[int, object] = {}
_chrome_lock = threading.RLock()


def _kill_process_tree(pid: int) -> None:
    """Last-resort cleanup of a process explicitly launched by this module."""
    try:
        if platform.system() == "Windows":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10, check=False, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            import signal
            # MCP launchers may establish their own process groups. Include
            # descendants before killing the original group so none are orphaned.
            listing = subprocess.run(["ps", "-eo", "pid=,ppid="], capture_output=True,
                                     text=True, timeout=5, check=False)
            parents = {}
            for line in listing.stdout.splitlines():
                columns = line.split()
                if len(columns) == 2 and all(value.isdigit() for value in columns):
                    parents[int(columns[0])] = int(columns[1])
            descendants = [pid]
            for parent in descendants:
                descendants.extend(child for child, owner in parents.items()
                                   if owner == parent and child not in descendants)
            for child in reversed(descendants[1:]):
                try:
                    os.kill(child, signal.SIGKILL)
                except ProcessLookupError:
                    continue
            try:
                if os.getpgid(pid) == pid:
                    os.killpg(pid, signal.SIGKILL)
                else:
                    os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    except (OSError, subprocess.SubprocessError):
        logger.debug("Owned Chrome process already exited", exc_info=True)


def _acquire_profile_lock(worker_id: int):
    """Hold an OS lock until cleanup; the OS releases it on process death."""
    config.CHROME_WORKER_DIR.mkdir(parents=True, exist_ok=True)
    lock = (config.CHROME_WORKER_DIR / f"worker-{worker_id}.lock").open("a+b")
    try:
        lock.seek(0)
        if platform.system() == "Windows":
            import msvcrt
            if lock.read(1) == b"":
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        lock.close()
        raise RuntimeError(f"Worker {worker_id} Chrome profile is already in use") from exc
    return lock


def setup_worker_profile(worker_id: int) -> Path:
    """Reuse a dedicated profile; never copy personal browser cookies/passwords."""
    if worker_id < 0:
        raise ValueError("worker_id must be nonnegative")
    profile_dir = config.CHROME_WORKER_DIR / f"worker-{worker_id}"
    profile_dir.mkdir(parents=True, exist_ok=True)
    return profile_dir


def _suppress_restore_nag(profile_dir: Path) -> None:
    prefs_file = profile_dir / "Default" / "Preferences"
    if not prefs_file.exists():
        return
    try:
        prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
        prefs.setdefault("profile", {})["exit_type"] = "Normal"
        prefs["credentials_enable_service"] = False
        prefs.setdefault("password_manager", {})["saving_enabled"] = False
        prefs_file.write_text(json.dumps(prefs), encoding="utf-8")
    except (OSError, ValueError):
        logger.debug("Could not patch Chrome preferences", exc_info=True)


def _port_available(port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def launch_chrome(worker_id: int, port: int | None = None,
                  headless: bool = False) -> subprocess.Popen:
    """Launch owned Chrome; reject busy ports/profiles instead of killing others."""
    import httpx

    port = BASE_CDP_PORT + worker_id if port is None else port
    if not 1 <= port <= 65535:
        raise ValueError("Invalid CDP port")
    with _chrome_lock:
        if worker_id in _chrome_procs:
            raise RuntimeError(f"Worker {worker_id} already owns Chrome")
        if not _port_available(port):
            raise RuntimeError(f"CDP port {port} is already occupied; choose another port")
        lock = _acquire_profile_lock(worker_id)
        proc = None
        try:
            profile_dir = setup_worker_profile(worker_id)
            _suppress_restore_nag(profile_dir)
            cmd = [config.get_chrome_path(), f"--remote-debugging-port={port}",
                   "--remote-debugging-address=127.0.0.1", f"--user-data-dir={profile_dir}",
                   "--profile-directory=Default", "--no-first-run", "--no-default-browser-check",
                   "--window-size=1280,900", "--disable-session-crashed-bubble",
                   "--deny-permission-prompts", "--disable-notifications", "--disable-extensions",
                   "--disable-component-extensions-with-background-pages"]
            if headless:
                cmd.append("--headless=new")
            # A fresh browser must not start a network-active new-tab page.
            cmd.append("about:blank")
            kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
            if platform.system() != "Windows":
                kwargs["start_new_session"] = True
            else:
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            proc = subprocess.Popen(cmd, **kwargs)
            with httpx.Client(trust_env=False, timeout=0.5) as client:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if proc.poll() is not None:
                        raise RuntimeError("Chrome exited before its debug endpoint became ready")
                    try:
                        response = client.get(f"http://127.0.0.1:{port}/json/version")
                        if response.is_success and response.json().get("webSocketDebuggerUrl"):
                            break
                    except (httpx.HTTPError, ValueError):
                        pass
                    time.sleep(0.1)
                else:
                    raise RuntimeError("Chrome debug endpoint did not become ready")
            _chrome_procs[worker_id] = proc
            _chrome_ports[worker_id] = port
            _profile_locks[worker_id] = lock
            return proc
        except BaseException:
            if proc is not None and proc.poll() is None:
                _kill_process_tree(proc.pid)
            lock.close()
            raise


def cleanup_worker(worker_id: int, process: subprocess.Popen | None) -> None:
    """Flush session cookies with Browser.close before an owned-process fallback."""
    with _chrome_lock:
        owned = _chrome_procs.get(worker_id)
        if owned is None or (process is not None and process is not owned):
            return
        port = _chrome_ports[worker_id]
        if owned.poll() is None:
            try:
                from playwright.sync_api import Error as PlaywrightError
                from playwright.sync_api import sync_playwright
                with sync_playwright() as playwright:
                    browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=3000)
                    try:
                        browser.new_browser_cdp_session().send("Browser.close")
                    except PlaywrightError:
                        logger.debug("Chrome closed its CDP transport", exc_info=True)
                owned.wait(timeout=5)
            except (PlaywrightError, OSError, subprocess.SubprocessError):
                if owned.poll() is None:
                    _kill_process_tree(owned.pid)
                try:
                    owned.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.error("Owned Chrome did not exit for worker %s", worker_id)
                    return  # Keep profile locked while a process may still use it.
        _chrome_procs.pop(worker_id, None)
        _chrome_ports.pop(worker_id, None)
        lock = _profile_locks.pop(worker_id, None)
        if lock:
            lock.close()


def kill_all_chrome() -> None:
    """Close only tracked processes. Never sweep ports or global Chrome processes."""
    with _chrome_lock:
        workers = list(_chrome_procs.items())
    for worker_id, process in workers:
        cleanup_worker(worker_id, process)


def abort_owned_chrome(port: int) -> None:
    """Fail closed if the application network gate dies; never kill a port owner blindly."""
    with _chrome_lock:
        for worker_id, owned_port in _chrome_ports.items():
            process = _chrome_procs.get(worker_id)
            if owned_port == port and process is not None and process.poll() is None:
                _kill_process_tree(process.pid)


def close_owned_chrome(port: int) -> None:
    """Gracefully close a tracked browser while its network guard is still attached."""
    with _chrome_lock:
        owners = [(worker_id, _chrome_procs.get(worker_id)) for worker_id, owned_port in _chrome_ports.items()
                  if owned_port == port]
    if len(owners) != 1:
        raise RuntimeError("Cannot detach an application gate from an unowned browser")
    cleanup_worker(*owners[0])
    if owners[0][1] is not None and owners[0][1].poll() is None:
        raise RuntimeError("Cannot detach the gate while its Chrome process is still running")


def reset_worker_dir(worker_id: int) -> Path:
    """Recreate an isolated scratch folder; uploads/evidence live elsewhere."""
    if worker_id < 0:
        raise ValueError("worker_id must be nonnegative")
    root = config.APPLY_WORKER_DIR.resolve()
    worker_dir = root / f"worker-{worker_id}"
    if worker_dir.resolve().parent != root:
        raise ValueError("Worker directory resolves outside the application workspace")
    if worker_dir.exists():
        shutil.rmtree(worker_dir)
    worker_dir.mkdir(parents=True)
    return worker_dir


def cleanup_on_exit() -> None:
    kill_all_chrome()
