import asyncio
import base64
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import traceback
import unittest
import uuid
from unittest.mock import Mock, patch

from utils import browser_turnstile as solver


TOKEN = base64.b64encode(b"opaque-browser-proof").decode()
CHALLENGE = "private-official-challenge"
REQUIREMENTS = "private-requirements-token"


class BrowserTurnstileTests(unittest.TestCase):
    def setUp(self):
        self.process = Mock(pid=987654, returncode=0)
        self.process.communicate.return_value = (json.dumps({"token": TOKEN}), None)
        self.popen = patch.object(solver.subprocess, "Popen", return_value=self.process).start()
        self.killpg = patch.object(solver.os, "killpg", create=True).start()
        patch.object(solver, "_worker_processes", return_value={987654: (1, 987654, 100)}).start()
        patch.object(solver, "_proc_identity", return_value=(1, 987654, 100)).start()
        self.addCleanup(patch.stopall)

    def solve(self):
        return solver.solve_browser_turnstile(CHALLENGE, REQUIREMENTS, "MatchingBrowserUA")

    def assert_private(self, error):
        message = "".join(traceback.format_exception(error))
        for secret in (CHALLENGE, REQUIREMENTS, TOKEN, "private-account-auth"):
            self.assertNotIn(secret, message)

    def test_only_challenge_and_matching_ua_cross_stdin(self):
        with patch.dict(os.environ, {
            "CHATGPT2API_AUTH_KEY": "private-account-auth", "OPENAI_API_KEY": "private-account-auth",
            "CHATGPT2API_BROWSER_TIMEZONE": "Asia/Singapore",
        }):
            self.assertEqual(self.solve(), TOKEN)
        args, kwargs = self.popen.call_args
        self.assertEqual(args[0], [sys.executable, "-m", "utils.browser_turnstile"])
        self.assertTrue(kwargs["start_new_session"])
        self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
        self.assertNotIn("CHATGPT2API_AUTH_KEY", kwargs["env"])
        self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
        self.assertEqual(kwargs["env"]["CHATGPT2API_BROWSER_TIMEZONE"], "Asia/Singapore")
        sent = json.loads(self.process.communicate.call_args_list[0].args[0])
        self.assertEqual(sent, {"dx": CHALLENGE, "p": REQUIREMENTS, "user_agent": "MatchingBrowserUA"})
        self.assertNotIn(CHALLENGE, repr(args))

    def test_known_worker_error_preserves_actionable_hint(self):
        self.process.returncode = 1
        self.process.communicate.return_value = (json.dumps({"error": "playwright_missing"}), None)
        with self.assertRaisesRegex(solver.BrowserTurnstileError, "uv sync --group browser") as raised:
            self.solve()
        self.assert_private(raised.exception)

    def test_untrusted_worker_errors_are_not_echoed(self):
        for result in ({"error": CHALLENGE + REQUIREMENTS}, {"error": {"token": TOKEN}}):
            with self.subTest(result_type=type(result["error"]).__name__):
                self.process.communicate.return_value = (json.dumps(result), None)
                with self.assertRaises(solver.BrowserTurnstileError) as raised:
                    self.solve()
                self.assert_private(raised.exception)

    def test_timeout_cleans_entire_process_group_without_leaking_inputs(self):
        timeout = subprocess.TimeoutExpired([CHALLENGE], 43, output=TOKEN, stderr=REQUIREMENTS)
        self.process.communicate.side_effect = [timeout, ("", None), ("", None)]
        with self.assertRaises(solver.BrowserTurnstileError) as raised:
            self.solve()
        self.assertEqual(raised.exception.code, "worker_timeout")
        self.assert_private(raised.exception)
        if os.name == "posix":
            self.killpg.assert_any_call(self.process.pid, signal.SIGTERM)
            self.killpg.assert_any_call(self.process.pid, signal.SIGKILL)

    def test_completed_leader_still_cleans_browser_descendants(self):
        self.process.poll.return_value = 0
        self.assertEqual(self.solve(), TOKEN)
        if os.name == "posix":
            self.killpg.assert_any_call(self.process.pid, signal.SIGKILL)

    def test_corrupt_protocol_and_launch_failure_do_not_leak(self):
        for result in (CHALLENGE + TOKEN, "[]", "null"):
            self.process.communicate.return_value = (result, None)
            with self.assertRaises(solver.BrowserTurnstileError) as raised:
                self.solve()
            self.assert_private(raised.exception)
        self.popen.side_effect = OSError(CHALLENGE + REQUIREMENTS)
        with self.assertRaises(solver.BrowserTurnstileError) as raised:
            self.solve()
        self.assert_private(raised.exception)

    def test_invalid_input_does_not_start_a_browser(self):
        for bad_value in (None, "", "  ", 123):
            with self.subTest(value_type=type(bad_value).__name__):
                with self.assertRaises(solver.BrowserTurnstileError):
                    solver.solve_browser_turnstile(bad_value, REQUIREMENTS, "UA")
        self.popen.assert_not_called()

    def test_empty_timeout_and_encoded_error_are_not_tokens(self):
        cases = [
            (None, "empty_result"), ("", "empty_result"), ("42", "vm_timeout"),
            ("not-base64", "invalid_result"),
            (base64.b64encode(b"12: TypeError: secret-challenge").decode(), "vm_error"),
            (base64.b64encode(b"ReferenceError: secret-challenge").decode(), "vm_error"),
        ]
        for token, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(solver.BrowserTurnstileError) as raised:
                    solver._validate_token(token)
                self.assertEqual(raised.exception.code, code)
                self.assertNotIn("secret-challenge", str(raised.exception))

    def test_missing_playwright_is_an_explicit_optional_dependency_error(self):
        with patch.dict(sys.modules, {"playwright": None, "playwright.async_api": None}):
            with self.assertRaises(solver.BrowserTurnstileError) as raised:
                asyncio.run(solver._browser_token({"dx": CHALLENGE, "p": REQUIREMENTS, "user_agent": "UA"}))
        self.assertEqual(raised.exception.code, "playwright_missing")


@unittest.skipUnless(os.name == "posix" and Path("/proc").exists(), "Linux process-group cleanup check")
class BrowserWorkerCleanupTests(unittest.TestCase):
    def test_sigterm_resistant_descendant_is_killed(self):
        self.check_descendant_cleanup(detached=False, exit_leader=False)

    def test_detached_sigterm_resistant_descendant_is_killed(self):
        self.check_descendant_cleanup(detached=True, exit_leader=False)

    def test_detached_child_is_found_after_leader_has_exited(self):
        self.check_descendant_cleanup(detached=True, exit_leader=True)

    def check_descendant_cleanup(self, detached, exit_leader):
        child_code = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(60)"
        leader_code = (
            "import subprocess,sys,time; "
            f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}],start_new_session={detached!r},stdout=subprocess.PIPE,text=True); "
            f"p.stdout.readline(); print(p.pid,flush=True); time.sleep({0 if exit_leader else 60})"
        )
        marker = uuid.uuid4().hex
        environment = dict(os.environ, **{solver.WORKER_MARKER_ENV: marker})
        process = subprocess.Popen(
            [sys.executable, "-c", leader_code], start_new_session=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            env=environment,
        )
        process._browser_worker_marker = marker
        try:
            descendant = int(process.stdout.readline())
            if exit_leader:
                process.wait(timeout=2)
            solver._stop_worker(process)
            self.assertIsNotNone(process.poll())
            deadline = time.monotonic() + 1
            running = True
            while time.monotonic() < deadline:
                try:
                    # A zombie awaits host reaping, but has no running code or browser resources.
                    running = Path(f"/proc/{descendant}/stat").read_text().split()[2] != "Z"
                except FileNotFoundError:
                    running = False
                if not running:
                    break
                time.sleep(0.02)
            self.assertFalse(running, "the worker's descendant survived cleanup")
        finally:
            solver._stop_worker(process)

    def test_reused_pid_is_not_signaled(self):
        with patch.object(solver, "_proc_identity", return_value=(1, 100, 999)), patch.object(solver.os, "killpg") as killpg:
            solver._signal_owned({100: (1, 100, 123)}, signal.SIGKILL)
        killpg.assert_not_called()


if __name__ == "__main__":
    unittest.main()
