import copy
import json
import os
import subprocess
import traceback
import unittest
from unittest.mock import Mock, patch

from services.openai_backend_api import OpenAIBackendAPI
from utils import browser_fingerprint as fingerprint
from utils.browser_turnstile import BrowserTurnstileError


class BrowserFingerprintTests(unittest.TestCase):
    def setUp(self):
        fingerprint._chromium_major.cache_clear()
        self.addCleanup(fingerprint._chromium_major.cache_clear)

    def test_explicit_chromium_and_chrome_executables_report_their_real_major(self):
        for output, expected in (
            ("Chromium 151.0.7890.4 built on Debian\n", "151"),
            ("Google Chrome 152.0.1234.0\n", "152"),
            ("Google Chrome for Testing 156.0.8078.4\n", "156"),
        ):
            with self.subTest(output=output):
                fingerprint._chromium_major.cache_clear()
                with patch.object(fingerprint.subprocess, "run", return_value=Mock(stdout=output)) as run:
                    self.assertEqual(fingerprint._chromium_major("/browser/chromium"), expected)
                self.assertEqual(run.call_args.args[0], ["/browser/chromium", "--version"])
                self.assertTrue(run.call_args.kwargs["check"])
                self.assertEqual(run.call_args.kwargs["timeout"], 5)

    def test_default_browser_version_comes_from_packaged_chromium_metadata(self):
        metadata = {"browsers": [
            {"name": "firefox", "browserVersion": "157.0"},
            {"name": "chromium-headless-shell", "browserVersion": "155.0.1.1"},
            {"name": "chromium", "browserVersion": "156.0.8078.4"},
        ]}
        package = Mock()
        package.joinpath.return_value.read_text.return_value = json.dumps(metadata)
        with patch.object(fingerprint.resources, "files", return_value=package) as files, patch.object(
            fingerprint.subprocess, "run"
        ) as run:
            self.assertEqual(fingerprint._chromium_major(""), "156")
        files.assert_called_once_with("playwright")
        package.joinpath.assert_called_once_with("driver/package/browsers.json")
        run.assert_not_called()

    def test_major_is_cached_per_executable_path(self):
        with patch.object(fingerprint.subprocess, "run", side_effect=[
            Mock(stdout="Chromium 151.0.1.1"), Mock(stdout="Chromium 152.0.1.1"),
        ]) as run:
            self.assertEqual(fingerprint._chromium_major("/browser/one"), "151")
            self.assertEqual(fingerprint._chromium_major("/browser/one"), "151")
            self.assertEqual(fingerprint._chromium_major("/browser/two"), "152")
            self.assertEqual(fingerprint._chromium_major("/browser/one"), "151")
        self.assertEqual(run.call_count, 2)

    def test_version_subprocess_receives_no_service_or_proxy_credentials(self):
        with patch.dict(os.environ, {
            "CHATGPT2API_AUTH_KEY": "private-auth", "OPENAI_API_KEY": "private-api-key",
            "HTTP_PROXY": "http://private-proxy", "GIT_TOKEN": "private-git-token",
        }), patch.object(fingerprint.subprocess, "run", return_value=Mock(stdout="Chromium 151.0.1.1")) as run:
            fingerprint._chromium_major("/browser/chromium")
        self.assertEqual(run.call_args.args[0], ["/browser/chromium", "--version"])
        sent = repr(run.call_args)
        for secret in ("private-auth", "private-api-key", "private-proxy", "private-git-token"):
            self.assertNotIn(secret, sent)
        for name in ("CHATGPT2API_AUTH_KEY", "OPENAI_API_KEY", "HTTP_PROXY", "GIT_TOKEN"):
            self.assertNotIn(name, run.call_args.kwargs["env"])

    def test_version_failures_are_actionable_and_do_not_expose_process_output(self):
        for failure in (
            OSError("private-browser-diagnostic"),
            subprocess.TimeoutExpired(["private-browser-diagnostic"], 5, output="private-browser-diagnostic"),
            subprocess.CalledProcessError(1, ["private-browser-diagnostic"], stderr="private-browser-diagnostic"),
        ):
            with self.subTest(failure=type(failure).__name__):
                with patch.object(fingerprint.subprocess, "run", side_effect=failure):
                    with self.assertRaises(BrowserTurnstileError) as raised:
                        fingerprint._chromium_major("/browser/chromium")
                self.assertEqual(raised.exception.code, "browser_start_failed")
                self.assertIn("CHATGPT2API_BROWSER_EXECUTABLE", str(raised.exception))
                self.assertNotIn("private-browser-diagnostic", "".join(traceback.format_exception(raised.exception)))

    def test_missing_optional_playwright_has_install_hint(self):
        with patch.object(fingerprint.resources, "files", side_effect=ModuleNotFoundError("private-details")):
            with self.assertRaises(BrowserTurnstileError) as raised:
                fingerprint._chromium_major("")
        self.assertEqual(raised.exception.code, "playwright_missing")
        self.assertIn("uv sync --group browser", str(raised.exception))
        self.assertNotIn("private-details", "".join(traceback.format_exception(raised.exception)))

    def test_invalid_metadata_is_not_used_as_a_user_agent_version(self):
        for metadata in ("invalid-json", {"browsers": []}, {"browsers": [
            {"name": "chromium", "browserVersion": "private-invalid-version"},
        ]}):
            with self.subTest(metadata=metadata):
                package = Mock()
                package.joinpath.return_value.read_text.return_value = (
                    metadata if isinstance(metadata, str) else json.dumps(metadata)
                )
                with patch.object(fingerprint.resources, "files", return_value=package):
                    with self.assertRaises(BrowserTurnstileError) as raised:
                        fingerprint._chromium_major("")
                self.assertEqual(raised.exception.code, "browser_start_failed")
                self.assertNotIn("private-invalid-version", str(raised.exception))

    def test_failed_version_probe_is_not_cached(self):
        with patch.object(fingerprint.subprocess, "run", side_effect=[
            OSError("not installed yet"), Mock(stdout="Chromium 151.0.1.1"),
        ]) as run:
            with self.assertRaises(BrowserTurnstileError):
                fingerprint._chromium_major("/browser/chromium")
            self.assertEqual(fingerprint._chromium_major("/browser/chromium"), "151")
        self.assertEqual(run.call_count, 2)

    def test_generated_user_agent_and_hints_match_browser_major_and_host_platform(self):
        for host, platform, ua_platform in (
            ("linux", "Linux", "X11; Linux x86_64"),
            ("darwin", "macOS", "Macintosh; Intel Mac OS X 10_15_7"),
            ("win32", "Windows", "Windows NT 10.0; Win64; x64"),
        ):
            with self.subTest(host=host):
                with patch.object(fingerprint.sys, "platform", host), patch.object(
                    fingerprint, "_chromium_major", return_value="151"
                ) as major, patch.dict(os.environ, {"CHATGPT2API_BROWSER_EXECUTABLE": " /browser/chromium "}):
                    result = fingerprint.browser_fingerprint()
                major.assert_called_once_with("/browser/chromium")
                self.assertIn("Chrome/151.0.0.0", result["user-agent"])
                self.assertIn(ua_platform, result["user-agent"])
                self.assertEqual(result["sec-ch-ua"], '"Chromium";v="151"')
                self.assertEqual(result["sec-ch-ua-platform"], f'"{platform}"')


class BackendFingerprintTests(unittest.TestCase):
    def setUp(self):
        self.backend = object.__new__(OpenAIBackendAPI)
        self.backend.account = {}
        self.generated = {
            "user-agent": "Browser151UA", "sec-ch-ua": '"Chromium";v="151"',
            "sec-ch-ua-platform": '"Linux"',
        }

    def test_browser_mode_fills_missing_fingerprint_without_mutating_account(self):
        self.backend.account = {"fp": {"oai-device-id": "existing-device"}}
        original = copy.deepcopy(self.backend.account)
        with patch.dict(os.environ, {"CHATGPT2API_TURNSTILE_SOLVER": "browser"}), patch(
            "services.openai_backend_api.browser_fingerprint", return_value=self.generated
        ) as browser:
            result = self.backend._build_fp()
        browser.assert_called_once_with()
        for key, value in self.generated.items():
            self.assertEqual(result[key], value)
        self.assertEqual(result["impersonate"], "chrome146")
        self.assertEqual(result["oai-device-id"], "existing-device")
        self.assertEqual(self.backend.account, original)

    def test_explicit_account_fingerprint_takes_priority_and_skips_browser_probe(self):
        self.backend.account = {"fp": {
            "User-Agent": "ExplicitUA", "sec-ch-ua": "ExplicitHints",
            "sec-ch-ua-platform": '"Windows"', "impersonate": "chrome145",
            "oai-device-id": "device", "oai-session-id": "session",
        }}
        with patch.dict(os.environ, {"CHATGPT2API_TURNSTILE_SOLVER": "browser"}), patch(
            "services.openai_backend_api.browser_fingerprint", side_effect=AssertionError("must not probe")
        ) as browser:
            result = self.backend._build_fp()
        browser.assert_not_called()
        for key, value in self.backend.account["fp"].items():
            self.assertEqual(result[key.lower()], value)

    def test_top_level_account_fingerprint_overrides_nested_values(self):
        self.backend.account = {
            "fp": {"user-agent": "NestedUA", "impersonate": "chrome145"},
            "user-agent": " TopLevelUA ", "impersonate": "chrome146",
            "sec-ch-ua": "TopLevelHints", "sec-ch-ua-platform": '"Linux"',
        }
        with patch.dict(os.environ, {"CHATGPT2API_TURNSTILE_SOLVER": "browser"}), patch(
            "services.openai_backend_api.browser_fingerprint"
        ) as browser:
            result = self.backend._build_fp()
        browser.assert_not_called()
        self.assertEqual(result["user-agent"], "TopLevelUA")
        self.assertEqual(result["impersonate"], "chrome146")
        self.assertEqual(result["sec-ch-ua"], "TopLevelHints")

    def test_empty_saved_user_agent_uses_the_detected_browser(self):
        for empty in ("", "  ", None):
            with self.subTest(empty=empty):
                self.backend.account = {"fp": {"user-agent": empty}}
                with patch.dict(os.environ, {"CHATGPT2API_TURNSTILE_SOLVER": "browser"}), patch(
                    "services.openai_backend_api.browser_fingerprint", return_value=self.generated
                ) as browser:
                    result = self.backend._build_fp()
                browser.assert_called_once_with()
                self.assertEqual(result["user-agent"], self.generated["user-agent"])

    def test_legacy_mode_does_not_require_optional_browser_dependency(self):
        with patch.dict(os.environ, {"CHATGPT2API_TURNSTILE_SOLVER": "legacy"}), patch(
            "services.openai_backend_api.browser_fingerprint", side_effect=AssertionError("must not probe")
        ) as browser:
            result = self.backend._build_fp()
        browser.assert_not_called()
        self.assertTrue(result["user-agent"])
        self.assertEqual(result["impersonate"], "chrome146")


if __name__ == "__main__":
    unittest.main()
