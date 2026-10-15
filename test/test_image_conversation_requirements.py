import json
import os
import re
import traceback
import unittest
from unittest.mock import Mock, patch

from curl_cffi import requests

from services.openai_backend_api import ChatRequirements, OpenAIBackendAPI
from utils.browser_turnstile import BrowserTurnstileError


def json_response(payload):
    response = requests.Response()
    response.content = json.dumps(payload).encode("utf-8")
    return response


class ImageConversationRequirementsTests(unittest.TestCase):
    def setUp(self):
        # Avoid account storage, fingerprint setup and actual network sessions.
        self.backend = object.__new__(OpenAIBackendAPI)
        self.backend.base_url = "https://upstream.invalid"
        self.backend.access_token = "private-access-token"
        self.backend.account = {}
        self.backend.user_agent = "test-user-agent"
        self.backend.pow_script_sources = []
        self.backend.pow_data_build = "test-build"
        self.backend.progress_callback = None
        self.backend.session = Mock()
        self.backend._headers = Mock(side_effect=lambda path, headers=None: dict(headers or {}))
        self.backend._bootstrap = Mock()
        self.solver = self.patch_backend("solve_turnstile_token", "private-turnstile-token")
        self.p_builder = self.patch_backend("build_legacy_requirements_token", "private-p-token")
        self.proof_builder = self.patch_backend("build_proof_token", "private-proof-token")

    def patch_backend(self, name, result):
        patcher = patch(f"services.openai_backend_api.{name}", return_value=result)
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    def prepare_data(self, turnstile=None):
        return {
            "prepare_token": "private-prepare-token",
            "proofofwork": {"required": True, "seed": "seed", "difficulty": "difficulty"},
            "turnstile": turnstile if turnstile is not None else {"required": True, "dx": "private-challenge"},
        }

    def assert_no_secrets(self, error):
        message = "".join(traceback.format_exception(error))
        for secret in (
            "private-access-token", "private-p-token", "private-proof-token",
            "private-turnstile-token", "private-prepare-token", "private-challenge",
        ):
            self.assertNotIn(secret, message)

    def test_finalize_uses_current_proof_and_turnstile_fields(self):
        self.backend.session.post.side_effect = [
            json_response(self.prepare_data()),
            json_response({"token": "chat-token", "so_token": "so-token"}),
        ]

        requirements = self.backend._get_chat_requirements()

        prepare_call, finalize_call = self.backend.session.post.call_args_list
        self.assertTrue(prepare_call.args[0].endswith("/sentinel/chat-requirements/prepare"))
        self.assertEqual(prepare_call.kwargs["json"], {"p": "private-p-token"})
        self.assertTrue(finalize_call.args[0].endswith("/sentinel/chat-requirements/finalize"))
        self.assertEqual(finalize_call.kwargs["json"], {
            "prepare_token": "private-prepare-token",
            "proofofwork": "private-proof-token",
            "turnstile": "private-turnstile-token",
        })
        self.solver.assert_called_once_with("private-challenge", "private-p-token")
        self.assertEqual(requirements.token, "chat-token")
        self.assertEqual(requirements.proof_token, "private-proof-token")
        self.assertEqual(requirements.turnstile_token, "private-turnstile-token")
        self.assertEqual(requirements.so_token, "so-token")

    def test_optional_challenges_do_not_invoke_solvers(self):
        prepare_data = {
            "prepare_token": "prepare-token",
            "turnstile": {"required": False, "dx": "unused"},
        }
        self.backend.session.post.side_effect = [
            json_response(prepare_data), json_response({"token": "chat-token"}),
        ]

        requirements = self.backend._get_chat_requirements()

        self.assertEqual(self.backend.session.post.call_args.kwargs["json"], {
            "prepare_token": "prepare-token", "proofofwork": "", "turnstile": "",
        })
        self.assertEqual(requirements.turnstile_token, "")
        self.assertEqual(self.backend._build_requirements(prepare_data).turnstile_token, "")
        self.solver.assert_not_called()
        self.proof_builder.assert_not_called()

    def test_browser_solver_uses_matching_user_agent_and_keeps_token(self):
        with patch.dict(os.environ, {"CHATGPT2API_TURNSTILE_SOLVER": "browser"}), patch(
            "services.openai_backend_api.solve_browser_turnstile", return_value="browser-token"
        ) as browser_solver:
            requirements = self.backend._build_requirements(self.prepare_data(), "private-p-token")

        self.assertEqual(requirements.turnstile_token, "browser-token")
        browser_solver.assert_called_once_with("private-challenge", "private-p-token", "test-user-agent")
        self.solver.assert_not_called()

    def test_browser_failure_is_actionable_and_does_not_fall_back_to_empty_token(self):
        self.backend.session.post.return_value = json_response(self.prepare_data())
        with patch.dict(os.environ, {"CHATGPT2API_TURNSTILE_SOLVER": "browser"}), patch(
            "services.openai_backend_api.solve_browser_turnstile",
            side_effect=BrowserTurnstileError("playwright_missing"),
        ):
            with self.assertRaisesRegex(RuntimeError, "Playwright.*uv sync") as raised:
                self.backend._get_chat_requirements()

        self.assertEqual(self.backend.session.post.call_count, 1)
        self.assert_no_secrets(raised.exception)
        self.solver.assert_not_called()

    def test_build_requirements_keeps_required_tokens(self):
        data = self.prepare_data()
        data.update({"token": "chat-token", "so_token": "so-token"})

        requirements = self.backend._build_requirements(data, "private-p-token")

        self.assertEqual(requirements.token, "chat-token")
        self.assertEqual(requirements.turnstile_token, "private-turnstile-token")
        self.assertEqual(requirements.proof_token, "private-proof-token")
        self.assertEqual(requirements.so_token, "so-token")
        self.solver.assert_called_once_with("private-challenge", "private-p-token")
        self.backend.session.post.assert_not_called()

    def test_required_missing_challenge_aborts_before_finalize(self):
        for challenge in (None, "", "  ", 123):
            with self.subTest(challenge=challenge):
                data = self.prepare_data({"required": True, "dx": challenge})
                self.backend.session.post.reset_mock()
                self.backend.session.post.return_value = json_response(data)

                with self.assertRaisesRegex(RuntimeError, "turnstile.*challenge is missing") as raised:
                    self.backend._get_chat_requirements()

                self.assertEqual(self.backend.session.post.call_count, 1)
                self.assert_no_secrets(raised.exception)
                with self.assertRaisesRegex(RuntimeError, "turnstile.*challenge is missing"):
                    self.backend._build_requirements(data, "private-p-token")
                self.solver.assert_not_called()

    def test_empty_solver_result_aborts_both_requirement_paths(self):
        for token in (None, "", "  ", 123):
            with self.subTest(token=token):
                self.solver.return_value = token
                data = self.prepare_data()
                self.backend.session.post.reset_mock()
                self.backend.session.post.return_value = json_response(data)

                with self.assertRaisesRegex(RuntimeError, "turnstile.*could not be solved") as raised:
                    self.backend._get_chat_requirements()

                self.assertEqual(self.backend.session.post.call_count, 1)
                self.assert_no_secrets(raised.exception)
                with self.assertRaisesRegex(RuntimeError, "turnstile.*could not be solved"):
                    self.backend._build_requirements(data, "private-p-token")

    def test_solver_exception_does_not_expose_challenge_or_tokens(self):
        self.solver.side_effect = ValueError("private-challenge private-p-token")
        data = self.prepare_data()
        self.backend.session.post.return_value = json_response(data)

        for get_requirements in (
            self.backend._get_chat_requirements,
            lambda: self.backend._build_requirements(data, "private-p-token"),
        ):
            with self.subTest(get_requirements=get_requirements):
                with self.assertRaisesRegex(RuntimeError, "turnstile.*could not be solved") as raised:
                    get_requirements()
                self.assert_no_secrets(raised.exception)
        self.assertEqual(self.backend.session.post.call_count, 1)

    def test_missing_required_token_stops_text_and_image_conversation_requests(self):
        self.solver.return_value = None
        self.backend._prepare_image_conversation = Mock()
        self.backend._start_image_generation = Mock()
        for hints in ([], ["picture_v2"]):
            with self.subTest(system_hints=hints):
                self.backend.session.post.reset_mock()
                self.backend.session.post.return_value = json_response(self.prepare_data())

                with self.assertRaisesRegex(RuntimeError, "turnstile.*could not be solved"):
                    list(self.backend.stream_conversation(prompt="test", model="gpt-image-2", system_hints=hints))

                self.assertEqual(self.backend.session.post.call_count, 1)
                self.assertTrue(self.backend.session.post.call_args.args[0].endswith("/prepare"))
                self.backend._prepare_image_conversation.assert_not_called()
                self.backend._start_image_generation.assert_not_called()

    def test_image_headers_forward_same_sentinel_tokens_as_chat(self):
        requirements = ChatRequirements("chat-token", "proof-token", "turnstile-token", "so-token")
        chat_headers = self.backend._conversation_headers("/conversation", requirements)
        for accept in ("*/*", "text/event-stream"):
            with self.subTest(accept=accept):
                image_headers = self.backend._image_headers("/f/conversation", requirements, "conduit-token", accept)

                for key in (
                    "OpenAI-Sentinel-Chat-Requirements-Token", "OpenAI-Sentinel-Proof-Token",
                    "OpenAI-Sentinel-Turnstile-Token", "OpenAI-Sentinel-SO-Token",
                ):
                    self.assertEqual(image_headers[key], chat_headers[key])
                self.assertEqual(image_headers["Accept"], accept)
                self.assertEqual(image_headers["X-Conduit-Token"], "conduit-token")
                self.assertEqual("X-Oai-Turn-Trace-Id" in image_headers, accept == "text/event-stream")

    def test_image_headers_omit_absent_optional_tokens(self):
        headers = self.backend._image_headers("/f/conversation", ChatRequirements("chat-token"))

        self.assertEqual(headers["OpenAI-Sentinel-Chat-Requirements-Token"], "chat-token")
        for key in (
            "OpenAI-Sentinel-Proof-Token", "OpenAI-Sentinel-Turnstile-Token",
            "OpenAI-Sentinel-SO-Token", "X-Conduit-Token",
        ):
            self.assertNotIn(key, headers)

    def test_image_model_uses_account_default_or_auto(self):
        for default, expected in (
            ("current-model", "current-model"), (" current-model ", "current-model"),
            (None, "auto"), ("", "auto"), ("  ", "auto"), (123, "auto"), ({}, "auto"),
        ):
            with self.subTest(default=default):
                self.backend.account = {"default_model_slug": default}
                self.assertEqual(self.backend._image_model_slug("gpt-image-2"), expected)

        self.backend.account = {}
        self.assertEqual(self.backend._image_model_slug("gpt-image-2"), "auto")

    def test_default_fingerprint_matches_ua_platform_and_tls_generation(self):
        fingerprint = self.backend._build_fp()
        major = re.search(r"Chrome/(\d+)", fingerprint["user-agent"]).group(1)
        self.assertEqual(fingerprint["impersonate"], "chrome" + major)
        self.assertIn('v="' + major + '"', fingerprint["sec-ch-ua"])
        self.assertIn("Linux", fingerprint["user-agent"])
        self.assertEqual(fingerprint["sec-ch-ua-platform"], '"Linux"')

    def test_explicit_account_fingerprint_is_preserved(self):
        custom = {
            "user-agent": "account-browser", "impersonate": "chrome145",
            "sec-ch-ua": '"Custom Browser";v="145"',
            "sec-ch-ua-platform": '"Windows"', "oai-device-id": "account-device",
        }
        self.backend.account = {"fp": custom}
        fingerprint = self.backend._build_fp()
        for key, value in custom.items():
            self.assertEqual(fingerprint[key], value)

    def test_codex_and_unknown_model_mappings_remain_unchanged(self):
        self.backend.account = {"default_model_slug": "current-model"}
        for model in ("codex-gpt-image-2", "plus-codex-gpt-image-2", "team-codex-gpt-image-2", "pro-codex-gpt-image-2"):
            with self.subTest(model=model):
                self.assertEqual(self.backend._image_model_slug(model), "codex-gpt-image-2")
        self.assertEqual(self.backend._image_model_slug("unknown-model"), "auto")

    def test_prepare_and_generation_payloads_use_the_same_current_model(self):
        self.backend.account = {"default_model_slug": "current-model"}
        requirements = ChatRequirements("chat-token", "proof-token", "turnstile-token", "so-token")
        self.backend.session.post.side_effect = [
            json_response({"conduit_token": "conduit-token"}), json_response({}),
        ]

        conduit = self.backend._prepare_image_conversation("test image", requirements, "gpt-image-2")
        self.backend._start_image_generation("test image", requirements, conduit, "gpt-image-2")

        prepare_call, generate_call = self.backend.session.post.call_args_list
        for request in (prepare_call, generate_call):
            self.assertEqual(request.kwargs["json"]["model"], "current-model")
            self.assertEqual(request.kwargs["json"]["parent_message_id"], "client-created-root")
            self.assertEqual(request.kwargs["headers"]["OpenAI-Sentinel-Turnstile-Token"], "turnstile-token")
            self.assertEqual(request.kwargs["headers"]["OpenAI-Sentinel-SO-Token"], "so-token")
        self.assertEqual(generate_call.kwargs["headers"]["X-Conduit-Token"], conduit)
        self.assertTrue(generate_call.kwargs["stream"])


if __name__ == "__main__":
    unittest.main()
