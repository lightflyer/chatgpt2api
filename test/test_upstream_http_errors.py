import itertools
import unittest
from unittest.mock import Mock

from curl_cffi import requests

from utils.helper import UpstreamHTTPError, ensure_ok


class StreamingResponse(requests.Response):
    """Keep curl_cffi's empty content/text behavior without making a request."""

    def __init__(self, chunks, status_code=404):
        super().__init__()
        self.status_code = status_code
        self.chunks = chunks
        self.chunks_read = 0
        self.closed = False

    def iter_content(self):
        for chunk in self.chunks:
            self.chunks_read += 1
            yield chunk

    def close(self):
        self.closed = True


class UpstreamHTTPErrorTests(unittest.TestCase):
    def test_streamed_error_retains_json_body_status_context_and_retry_after(self):
        response = StreamingResponse([b'{"detail":', b'"Model not found"}'])
        response.headers["Retry-After"] = " 9 "
        self.assertEqual(response.text, "")

        with self.assertRaises(UpstreamHTTPError) as raised:
            ensure_ok(response, "/backend-api/f/conversation")

        error = raised.exception
        self.assertEqual(error.body, {"detail": "Model not found"})
        self.assertEqual(error.status_code, 404)
        self.assertEqual(error.context, "/backend-api/f/conversation")
        self.assertEqual(error.retry_after, 9)
        self.assertIn("Model not found", str(error))
        self.assertTrue(response.closed)

    def test_successful_stream_is_not_consumed_parsed_or_closed(self):
        for status in (200, 201, 204, 299):
            with self.subTest(status=status):
                response = StreamingResponse([b"data: pending\n\n"], status)
                response.json = Mock(side_effect=AssertionError("must not parse"))

                ensure_ok(response, "conversation")

                self.assertEqual(response.chunks_read, 0)
                self.assertFalse(response.closed)
                response.json.assert_not_called()
                self.assertFalse(hasattr(response, "_text"))

    def test_buffered_json_error_does_not_read_stream(self):
        response = requests.Response()
        response.status_code = 429
        response.content = b'{"error":{"message":"Rate limited"}}'
        response.headers["retry-after"] = "12"
        response.iter_content = Mock(side_effect=AssertionError("must not read"))
        response.close = Mock()

        with self.assertRaises(UpstreamHTTPError) as raised:
            ensure_ok(response, "prepare")

        self.assertEqual(raised.exception.body, {"error": {"message": "Rate limited"}})
        self.assertEqual(raised.exception.retry_after, 12)
        response.iter_content.assert_not_called()
        response.close.assert_called_once_with()

    def test_plain_text_error_retains_its_encoding(self):
        response = StreamingResponse([b"caf\xe9 unavailable"], 503)
        response.headers["Content-Type"] = "text/plain; charset=iso-8859-1"

        with self.assertRaises(UpstreamHTTPError) as raised:
            ensure_ok(response, "conversation")

        self.assertEqual(raised.exception.body, "café unavailable")
        self.assertTrue(response.closed)

    def test_invalid_encoding_falls_back_to_utf8_with_replacement(self):
        response = StreamingResponse([b"upstream: \xff"], 502)
        response.encoding = "unknown-encoding"

        with self.assertRaises(UpstreamHTTPError) as raised:
            ensure_ok(response, "conversation")

        self.assertEqual(raised.exception.body, "upstream: �")
        self.assertTrue(response.closed)

    def test_empty_non_streaming_response_preserves_original_http_error(self):
        response = requests.Response()
        response.status_code = 404

        with self.assertRaises(UpstreamHTTPError) as raised:
            ensure_ok(response, "conversation")

        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(raised.exception.body, "")
        self.assertIsNone(raised.exception.retry_after)

    def test_infinite_error_stream_stops_at_64_kib_and_closes(self):
        response = StreamingResponse(itertools.repeat(b"x" * 8192), 502)

        with self.assertRaises(UpstreamHTTPError) as raised:
            ensure_ok(response, "conversation")

        self.assertEqual(raised.exception.body, "x" * (64 * 1024) + "…[truncated]")
        self.assertEqual(response.chunks_read, 8)
        self.assertTrue(response.closed)
        self.assertLess(len(str(raised.exception)), 600)

    def test_large_single_chunk_is_also_capped(self):
        response = StreamingResponse([b"x" * (128 * 1024), b"do not read"], 502)

        with self.assertRaises(UpstreamHTTPError) as raised:
            ensure_ok(response, "conversation")

        self.assertEqual(raised.exception.body, "x" * (64 * 1024) + "…[truncated]")
        self.assertEqual(response.chunks_read, 1)
        self.assertTrue(response.closed)

    def test_read_failure_keeps_received_json(self):
        def interrupted_stream():
            yield b'{"detail":"Model not found"}'
            raise TimeoutError("body read timed out")

        response = StreamingResponse(interrupted_stream())

        with self.assertRaises(UpstreamHTTPError) as raised:
            ensure_ok(response, "conversation")

        self.assertEqual(raised.exception.body, {"detail": "Model not found"})
        self.assertTrue(response.closed)

    def test_read_and_cleanup_failures_do_not_hide_http_error(self):
        def interrupted_stream():
            yield b"partial body"
            raise TimeoutError("body read timed out")

        response = StreamingResponse(interrupted_stream(), 503)
        response.close = Mock(side_effect=RuntimeError("cleanup failed"))

        with self.assertRaises(UpstreamHTTPError) as raised:
            ensure_ok(response, "conversation")

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.body, "partial body")
        response.close.assert_called_once_with()

    def test_minimal_non_streaming_response_does_not_require_close_or_headers(self):
        class MinimalResponse:
            status_code = 404
            text = "Not found"

            def json(self):
                raise ValueError("not JSON")

        with self.assertRaises(UpstreamHTTPError) as raised:
            ensure_ok(MinimalResponse(), "conversation")

        self.assertEqual(raised.exception.body, "Not found")
        self.assertIsNone(raised.exception.retry_after)


if __name__ == "__main__":
    unittest.main()
