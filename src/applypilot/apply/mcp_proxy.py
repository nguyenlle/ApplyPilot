"""Narrow stdio MCP relay: native-form browsing cannot escape approved URLs/tools."""

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import threading
from pathlib import Path

ALLOWED = {"browser_navigate", "browser_snapshot", "browser_click", "browser_fill_form", "browser_type",
           "browser_select_option", "browser_file_upload", "browser_press_key", "browser_wait_for",
           "browser_take_screenshot", "browser_tabs"}
KEYS = {"Tab", "Shift+Tab", "Enter", "Space", "ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight", "Escape"}


def guard_call(name: str, arguments: dict, policy: dict) -> None:
    """Refuse code execution, arbitrary navigation/files, and browser shortcuts."""
    if name not in ALLOWED:
        raise ValueError("Tool is outside the native-form allowlist")
    if name == "browser_navigate" and arguments.get("url") not in policy["urls"]:
        raise ValueError("Navigation is outside reviewed adapter URLs")
    if name == "browser_tabs" and arguments.get("action") not in {"list", "select"}:
        raise ValueError("New/closed tabs are unsupported in a native-form attempt")
    if name == "browser_press_key" and arguments.get("key") not in KEYS:
        raise ValueError("Browser shortcut is not permitted")
    if name == "browser_file_upload":
        paths = arguments.get("paths")
        if not isinstance(paths, list) or not paths:
            raise ValueError("Upload requires verified document paths")
        for path in paths:
            digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            if digest not in policy["file_sha256"]:
                raise ValueError("Upload bytes are not an approved job artifact")
    if name in {"browser_take_screenshot", "browser_snapshot"} and arguments.get("filename"):
        filename = arguments["filename"]
        suffixes = {".png"} if name == "browser_take_screenshot" else {".txt", ".md"}
        if not isinstance(filename, str) or Path(filename).name != filename or Path(filename).suffix not in suffixes:
            raise ValueError("Output filename must be a plain approved name in the private output directory")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    options = parser.parse_args()
    if not 1 <= options.port <= 65535:
        raise ValueError("Invalid CDP port")
    policy = json.loads(options.policy.read_text(encoding="utf-8"))
    npx = shutil.which("npx")
    if not npx:
        raise RuntimeError("npx is not installed")
    command = [npx, "-y", "@playwright/mcp@0.0.81", f"--cdp-endpoint=http://127.0.0.1:{options.port}"]
    if platform.system() == "Windows":
        node = shutil.which("node")
        cli = Path(npx).parent / "node_modules/npm/bin/npx-cli.js"
        if not node or not cli.is_file():
            raise RuntimeError("Cannot resolve npm's JavaScript CLI; install standard Node.js/npm")
        command = [node, str(cli), *command[1:]]
    child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr,
                             text=True, encoding="utf-8", bufsize=1,
                             start_new_session=platform.system() != "Windows",
                             creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0)
    output_lock = threading.Lock()

    def emit(message):
        with output_lock:
            sys.stdout.write(message + "\n")
            sys.stdout.flush()

    def relay_output():
        for line in child.stdout:
            emit(line.rstrip("\n"))

    thread = threading.Thread(target=relay_output, daemon=True)
    thread.start()
    try:
        for line in sys.stdin:
            message = json.loads(line)
            if message.get("method") == "tools/call":
                params = message.get("params", {})
                try:
                    guard_call(params.get("name", ""), params.get("arguments", {}), policy)
                except (ValueError, OSError, TypeError) as exc:
                    emit(json.dumps({"jsonrpc": "2.0", "id": message.get("id"), "result": {
                        "isError": True, "content": [{"type": "text", "text": f"Application guard blocked: {exc}"}]}}))
                    continue
            child.stdin.write(line)
            child.stdin.flush()
    finally:
        child.stdin.close()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            from applypilot.apply.chrome import _kill_process_tree
            _kill_process_tree(child.pid)
        thread.join(timeout=2)


if __name__ == "__main__":
    main()
