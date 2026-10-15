"""Optional local browser execution of official ChatGPT Turnstile challenges.

Only the official challenge, its requirements token, and the matching user agent
cross stdin. The worker opens a fresh, unauthenticated browser context. It never
receives ChatGPT account credentials. See browser_turnstile_vm.js for provenance.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import uuid


WORKER_TIMEOUT_SECONDS = 45
CHATGPT_PAGE = "https://chatgpt.com/gpts"
MAX_PAYLOAD_BYTES = 2_000_000
WORKER_MARKER_ENV = "CHATGPT2API_BROWSER_WORKER_ID"
ERROR_MESSAGES = {
    "invalid_input": "Browser Turnstile requires a challenge, requirements token, and user agent.",
    "playwright_missing": "Browser Turnstile requires Playwright: run uv sync --group browser and python -m playwright install chromium.",
    "browser_start_failed": "Browser Turnstile could not start Chromium. Install its browser and system dependencies; check CHATGPT2API_BROWSER_EXECUTABLE. Disable the sandbox only explicitly with CHATGPT2API_BROWSER_NO_SANDBOX=1 if required by this host.",
    "page_unavailable": "Browser Turnstile could not load the official ChatGPT page.",
    "page_challenge": "Browser Turnstile encountered a challenge on the official ChatGPT page.",
    "wrong_origin": "Browser Turnstile refused an unexpected page origin.",
    "vm_failed": "Browser Turnstile could not execute the official challenge.",
    "empty_result": "Browser Turnstile returned no token.",
    "vm_timeout": "Browser Turnstile timed out while executing the challenge.",
    "vm_error": "Browser Turnstile returned an encoded execution error.",
    "invalid_result": "Browser Turnstile returned an invalid token.",
    "worker_timeout": "Browser Turnstile exceeded its 45-second time limit.",
    "worker_failed": "Browser Turnstile worker failed.",
    "worker_protocol": "Browser Turnstile worker returned an invalid response.",
}


class BrowserTurnstileError(RuntimeError):
    """Safe error containing no challenge, token, or raw browser diagnostics."""

    def __init__(self, code: str):
        self.code = code if code in ERROR_MESSAGES else "worker_failed"
        super().__init__(ERROR_MESSAGES[self.code])


def _validate_input(data: object) -> dict[str, str]:
    if not isinstance(data, dict) or set(data) != {"dx", "p", "user_agent"}:
        raise BrowserTurnstileError("invalid_input")
    if any(not isinstance(value, str) or not value.strip() for value in data.values()):
        raise BrowserTurnstileError("invalid_input")
    return data


def _validate_token(token: object) -> str:
    if not isinstance(token, str) or not token.strip():
        raise BrowserTurnstileError("empty_result")
    if re.fullmatch(r"\d+", token):
        # The VM returns its executed-step count when its soft timeout elapses.
        raise BrowserTurnstileError("vm_timeout")
    try:
        decoded = base64.b64decode(token, validate=True).decode("utf-8", errors="replace")
    except Exception:
        raise BrowserTurnstileError("invalid_result") from None
    if not decoded:
        raise BrowserTurnstileError("empty_result")
    if re.match(r"^\d+:\s", decoded) or re.match(
        r"^(?:TypeError|ReferenceError|SyntaxError|RangeError|Error):", decoded
    ):
        raise BrowserTurnstileError("vm_error")
    return token


def _worker_environment() -> dict[str, str]:
    # Keep runtime paths/settings, without forwarding service credentials or
    # proxy credentials. This worker performs no account-authenticated request.
    names = {
        "PATH", "HOME", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL",
        "SYSTEMROOT", "WINDIR", "USERPROFILE", "LOCALAPPDATA",
        "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "LD_LIBRARY_PATH",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "PLAYWRIGHT_BROWSERS_PATH",
        "CHATGPT2API_BROWSER_EXECUTABLE", "CHATGPT2API_BROWSER_NO_SANDBOX",
        "CHATGPT2API_BROWSER_TIMEZONE",
    }
    return {key: value for key, value in os.environ.items() if key in names}


def _proc_identity(pid: int) -> tuple[int, int, int] | None:
    """Return (parent PID, process group, start ticks), without command data."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(fields[1]), int(fields[2]), int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def _worker_processes(process: subprocess.Popen) -> dict[int, tuple[int, int, int]]:
    """Find descendants even after Playwright detaches or the worker exits."""
    marker = getattr(process, "_browser_worker_marker", None)
    needle = (WORKER_MARKER_ENV + "=" + marker).encode() if isinstance(marker, str) else None
    records = {}
    expected = getattr(process, "_browser_worker_identity", None)
    current = _proc_identity(process.pid)
    root_matches = current is not None and (
        (isinstance(expected, tuple) and current[2] == expected[2])
        or (not isinstance(expected, tuple) and process.poll() is None)
    )
    owned = {process.pid} if root_matches else set()
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return records
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        identity = _proc_identity(pid)
        if identity is None:
            continue
        records[pid] = identity
        if needle:
            try:
                if needle in (entry / "environ").read_bytes().split(b"\0"):
                    owned.add(pid)
            except OSError:
                pass
    # Renderers may hide their environment; their browser parent's ownership is
    # sufficient. The random marker also finds reparented detached group leaders.
    while True:
        descendants = {pid for pid, info in records.items() if info[0] in owned}
        if descendants <= owned:
            break
        owned.update(descendants)
    return {pid: info for pid, info in records.items() if pid in owned}


def _signal_owned(records: dict[int, tuple[int, int, int]], sig: int) -> None:
    signaled_groups = set()
    for pid, identity in records.items():
        current = _proc_identity(pid)
        if current is None or current[2] != identity[2]:
            continue  # PID has exited or been reused by an unrelated process.
        group = current[1]
        try:
            if group > 1 and group != os.getpgrp():
                if group not in signaled_groups:
                    os.killpg(group, sig)
                    signaled_groups.add(group)
            else:
                os.kill(pid, sig)
        except (ProcessLookupError, OSError):
            pass


def _stop_worker(process: subprocess.Popen) -> None:
    """Clean worker and detached Chromium groups, including orphaned children."""
    linux = sys.platform.startswith("linux")
    records = _worker_processes(process) if linux else {}
    if linux:
        _signal_owned(records, signal.SIGTERM)
    else:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            elif process.poll() is None:
                process.terminate()
        except (ProcessLookupError, OSError):
            pass
    try:
        process.communicate(timeout=1)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass
    if linux:
        records.update(_worker_processes(process))
        _signal_owned(records, signal.SIGKILL)
    else:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            elif process.poll() is None:
                process.kill()
        except (ProcessLookupError, OSError):
            pass
    try:
        process.communicate(timeout=1)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass


def solve_browser_turnstile(dx: str, p: str, user_agent: str) -> str:
    """Return a browser-computed token, or raise a sanitized actionable error."""
    payload = json.dumps(_validate_input({"dx": dx, "p": p, "user_agent": user_agent}))
    if len(payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise BrowserTurnstileError("invalid_input")
    process = None
    try:
        marker = uuid.uuid4().hex
        environment = _worker_environment()
        environment[WORKER_MARKER_ENV] = marker
        process = subprocess.Popen(
            [sys.executable, "-m", "utils.browser_turnstile"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            cwd=str(Path(__file__).resolve().parents[1]),
            env=environment,
            start_new_session=True,
        )
        process._browser_worker_marker = marker
        process._browser_worker_identity = _proc_identity(process.pid)
        try:
            # Reserve two seconds for process-group cleanup within the budget.
            stdout, _ = process.communicate(payload + "\n", timeout=WORKER_TIMEOUT_SECONDS - 2)
        except subprocess.TimeoutExpired:
            raise BrowserTurnstileError("worker_timeout") from None
        try:
            result = json.loads(stdout)
        except (TypeError, ValueError):
            raise BrowserTurnstileError("worker_protocol") from None
        if not isinstance(result, dict):
            raise BrowserTurnstileError("worker_protocol")
        if "error" in result:
            code = result["error"]
            raise BrowserTurnstileError(code if isinstance(code, str) else "worker_failed")
        if process.returncode != 0:
            raise BrowserTurnstileError("worker_failed")
        return _validate_token(result.get("token"))
    except BrowserTurnstileError:
        raise
    except Exception:
        raise BrowserTurnstileError("worker_failed") from None
    finally:
        if process is not None:
            _stop_worker(process)


async def _browser_token(data: dict[str, str]) -> str:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise BrowserTurnstileError("playwright_missing") from None
    playwright = browser = context = None
    try:
        try:
            playwright = await async_playwright().start()
            launch = {
                "headless": True,
                "chromium_sandbox": os.environ.get("CHATGPT2API_BROWSER_NO_SANDBOX") != "1",
                "args": ["--disable-dev-shm-usage"],
                "timeout": 10000,
            }
            executable = os.environ.get("CHATGPT2API_BROWSER_EXECUTABLE", "").strip()
            if executable:
                launch["executable_path"] = executable
            else:
                # Use full Chromium rather than the separate headless shell.
                launch["channel"] = "chromium"
            browser = await playwright.chromium.launch(**launch)
            context = await browser.new_context(
                user_agent=data["user_agent"],
                viewport={"width": 1280, "height": 800},
                locale="zh-CN",
                timezone_id=os.environ.get("CHATGPT2API_BROWSER_TIMEZONE", "Asia/Shanghai"),
                accept_downloads=False,
            )
            await context.add_init_script(path=str(Path(__file__).with_name("browser_turnstile_vm.js")))
            page = await context.new_page()
        except Exception:
            raise BrowserTurnstileError("browser_start_failed") from None
        try:
            response = await page.goto(CHATGPT_PAGE, wait_until="domcontentloaded", timeout=25000)
            if response is not None and response.status >= 400:
                raise BrowserTurnstileError("page_unavailable")
            if not await page.evaluate("location.origin === 'https://chatgpt.com'"):
                raise BrowserTurnstileError("wrong_origin")
            title = (await page.title()).lower()
            if any(value in title for value in ("just a moment", "attention required", "请稍候")):
                raise BrowserTurnstileError("page_challenge")
        except BrowserTurnstileError:
            raise
        except Exception:
            raise BrowserTurnstileError("page_unavailable") from None
        try:
            token = await asyncio.wait_for(
                page.evaluate("([dx,p]) => window.__sentinelVM.turnstile(dx,p)", [data["dx"], data["p"]]),
                timeout=5,
            )
        except Exception:
            raise BrowserTurnstileError("vm_failed") from None
        return _validate_token(token)
    finally:
        for resource in (context, browser, playwright):
            if resource is None:
                continue
            try:
                close = resource.stop if resource is playwright else resource.close
                await asyncio.wait_for(close(), timeout=1)
            except Exception:
                pass


def _main() -> int:
    try:
        raw = sys.stdin.buffer.readline(MAX_PAYLOAD_BYTES + 1)
        if len(raw) > MAX_PAYLOAD_BYTES:
            raise BrowserTurnstileError("invalid_input")
        try:
            data = _validate_input(json.loads(raw))
        except (ValueError, TypeError):
            raise BrowserTurnstileError("invalid_input") from None
        token = asyncio.run(_browser_token(data))
        result, status = {"token": token}, 0
    except BrowserTurnstileError as exc:
        result, status = {"error": exc.code}, 1
    except Exception:
        result, status = {"error": "worker_failed"}, 1
    # Protocol only: never print browser exception text, inputs, or diagnostics.
    print(json.dumps(result), flush=True)
    return status


if __name__ == "__main__":
    raise SystemExit(_main())
