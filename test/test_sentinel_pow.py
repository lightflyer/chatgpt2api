import itertools
import unittest
from unittest.mock import patch

from utils.pow import _pow_generate, _sentinel_hash, build_proof_token


class SentinelProofTests(unittest.TestCase):
    # Fixtures were evaluated with the public ChatGPT frontend's HJt function
    # and _runCheck on 2026-10-15, using Node.js (not the Python implementation).
    # Source: https://chatgpt.com/cdn/assets/4813494d-e688mn57gf7k52mc.js
    def test_hash_matches_javascript_utf16_and_unsigned_overflow(self):
        for text, expected in (
            ("", "ab3e7c0b"),
            ("abc", "1cc93dbc"),
            ("diagnostic-seedWzEsMiwzXQ==", "50a35394"),
            ("你好", "3ced938d"),
            ("😀", "6a85a426"),
        ):
            with self.subTest(text=text):
                self.assertEqual(_sentinel_hash(text), expected)

    def test_proof_matches_javascript_with_odd_and_even_difficulty_lengths(self):
        config = [
            3000, "Thu Oct 15 2026", 4294705152, 1, "Diagnostic/1.0",
            "https://chatgpt.com/backend-api/sentinel/sdk.js", "prod-fixture",
            "en-US", "en-US", 0, "中😀",
        ]
        expected_answers = {
            "0": (
                "WzMwMDAsIlRodSBPY3QgMTUgMjAyNiIsNDI5NDcwNTE1Miw1LCJEaWFnbm9zdGljLzEuMCIs"
                "Imh0dHBzOi8vY2hhdGdwdC5jb20vYmFja2VuZC1hcGkvc2VudGluZWwvc2RrLmpzIiwicHJv"
                "ZC1maXh0dXJlIiwiZW4tVVMiLCJlbi1VUyIsMiwi5Lit8J+YgCJd~S"
            ),
            "00": (
                "WzMwMDAsIlRodSBPY3QgMTUgMjAyNiIsNDI5NDcwNTE1Miw2MTQsIkRpYWdub3N0aWMvMS4wIiw"
                "iaHR0cHM6Ly9jaGF0Z3B0LmNvbS9iYWNrZW5kLWFwaS9zZW50aW5lbC9zZGsuanMiLCJwcm9k"
                "LWZpeHR1cmUiLCJlbi1VUyIsImVuLVVTIiwyLCLkuK3wn5iAIl0=~S"
            ),
            "000": (
                "WzMwMDAsIlRodSBPY3QgMTUgMjAyNiIsNDI5NDcwNTE1MiwxODc1LCJEaWFnbm9zdGljLzEuMCIs"
                "Imh0dHBzOi8vY2hhdGdwdC5jb20vYmFja2VuZC1hcGkvc2VudGluZWwvc2RrLmpzIiwicHJv"
                "ZC1maXh0dXJlIiwiZW4tVVMiLCJlbi1VUyIsMiwi5Lit8J+YgCJd~S"
            ),
        }
        for difficulty, expected in expected_answers.items():
            with self.subTest(difficulty=difficulty):
                clock = itertools.chain([1.0], itertools.repeat(1.002))
                with patch("utils.pow.time.perf_counter", side_effect=clock):
                    answer, solved = _pow_generate("diagnostic-seed", difficulty, config)
                self.assertTrue(solved)
                self.assertEqual(answer, expected)
        self.assertEqual(config[3], 1)
        self.assertEqual(config[9], 0)

    def test_public_builder_keeps_prefix_and_current_answer_suffix(self):
        with patch("utils.pow.build_pow_config", return_value=list(range(11))):
            token = build_proof_token("seed", "ffffffff", "Diagnostic/1.0")
        self.assertTrue(token.startswith("gAAAAAB"))
        self.assertTrue(token.endswith("~S"))

    def test_exhausted_search_cannot_be_used_as_a_proof(self):
        with patch("utils.pow._pow_generate", return_value=("failure-result", False)):
            with self.assertRaisesRegex(RuntimeError, "failed to solve proof token"):
                build_proof_token("seed", "00000000", "Diagnostic/1.0")


if __name__ == "__main__":
    unittest.main()
