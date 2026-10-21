from __future__ import annotations

import copy
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from services import openai_backend_api as api
from services.protocol import conversation as protocol


def text_document(text="请上传需要修改的原图，这条消息里还没有看到图片。"):
    return {"current_node": "answer", "mapping": {
        "user": {"parent": None, "message": {"author": {"role": "user"},
            "content": {"content_type": "text", "parts": ["把衣服改成蓝色"]}}},
        "answer": {"parent": "user", "message": {"author": {"role": "assistant"},
            "recipient": "all", "status": "finished_successfully", "end_turn": True,
            "content": {"content_type": "text", "parts": [text]}}},
    }}


def with_tool(document):
    document = copy.deepcopy(document)
    document["mapping"]["tool"] = {"parent": "user", "message": {
        "author": {"role": "assistant"}, "recipient": "image_gen.text2im",
        "status": "finished_successfully", "end_turn": False,
        "content": {"content_type": "text", "parts": ['{"prompt":"blue shirt"}']},
    }}
    document["mapping"]["answer"]["parent"] = "tool"
    return document


class CompletedImageTextTests(unittest.TestCase):
    def test_missing_input_image_returns_the_actual_final_answer(self):
        self.assertIn("请上传", api.OpenAIBackendAPI.completed_image_text_response(text_document()))

    def test_uploaded_reference_is_not_mistaken_for_generated_output(self):
        doc = text_document()
        doc["mapping"]["user"]["message"]["content"] = {
            "content_type": "multimodal_text", "parts": [
                {"content_type": "image_asset_pointer", "asset_pointer": "file-service://input"}]}
        self.assertTrue(api.OpenAIBackendAPI.completed_image_text_response(doc))

    def test_unfinished_or_tool_directed_messages_keep_waiting(self):
        for field, value in [("status", "in_progress"), ("end_turn", False),
                             ("end_turn", None), ("recipient", "image_gen.text2im"), ("channel", "analysis")]:
            with self.subTest(field=field, value=value):
                doc = text_document()
                doc["mapping"]["answer"]["message"][field] = value
                self.assertEqual(api.OpenAIBackendAPI.completed_image_text_response(doc), "")

    def test_invoked_tool_is_allowed_to_finish_asynchronously(self):
        self.assertEqual(api.OpenAIBackendAPI.completed_image_text_response(with_tool(text_document())), "")

    def test_async_tool_metadata_is_pending_even_without_result_ids(self):
        doc = text_document()
        doc["mapping"]["answer"]["message"]["metadata"] = {"async_task_type": "image_gen"}
        self.assertEqual(api.OpenAIBackendAPI.completed_image_text_response(doc), "")

    def test_tool_parameters_in_a_text_message_still_allow_async_images(self):
        for text in ['{"prompt":"blue shirt","size":"1024x1024"}',
                     '{"referenced_image_ids":["input"]}']:
            with self.subTest(text=text):
                self.assertEqual(api.OpenAIBackendAPI.completed_image_text_response(text_document(text)), "")

    def test_previous_turn_and_abandoned_branch_do_not_mask_missing_input(self):
        doc = text_document()
        doc["mapping"]["old-tool"] = {"parent": None, "message": {
            "author": {"role": "tool", "name": "image_gen"},
            "metadata": {"async_task_type": "image_gen"}}}
        doc["mapping"]["user"]["parent"] = "old-tool"
        self.assertTrue(api.OpenAIBackendAPI.completed_image_text_response(doc))

    def test_unknown_or_cyclic_active_branch_does_not_loop(self):
        doc = text_document()
        doc["current_node"] = "unknown"
        self.assertEqual(api.OpenAIBackendAPI.completed_image_text_response(doc), "")
        doc["current_node"] = "answer"
        doc["mapping"]["answer"]["parent"] = "answer"
        self.assertEqual(api.OpenAIBackendAPI.completed_image_text_response(doc), "")

    def test_snapshot_without_current_node_is_supported(self):
        doc = text_document()
        del doc["current_node"]
        self.assertTrue(api.OpenAIBackendAPI.completed_image_text_response(doc))


class ImageTextPollingTests(unittest.TestCase):
    def setUp(self):
        self.backend = object.__new__(api.OpenAIBackendAPI)
        self.backend._query_backend_tasks = Mock(return_value=[])
        self.backend._get_conversation = Mock(return_value=text_document())
        self.backend._resolve_image_urls = Mock()
        self.now = 0.0
        settings = SimpleNamespace(image_poll_timeout_secs=600, image_poll_interval_secs=10,
            image_poll_initial_wait_secs=0, image_settle_enabled=False, image_check_before_hit_enabled=False)
        for patcher in [patch.object(api, "config", settings),
                        patch.object(api.time, "time", side_effect=lambda: self.now),
                        patch.object(api.time, "sleep", side_effect=self.advance)]:
            patcher.start()
            self.addCleanup(patcher.stop)

    def advance(self, seconds):
        self.now += seconds

    def test_completed_text_stops_on_first_document_instead_of_waiting_600_seconds(self):
        with self.assertRaises(api.ImageTextResponseError) as raised:
            self.backend.resolve_conversation_image_urls("original", [], [])
        self.assertEqual(raised.exception.conversation_id, "original")
        self.assertIn("请上传", str(raised.exception))
        self.assertNotIn("超时", str(raised.exception))
        self.assertEqual(self.now, 0)
        self.backend._get_conversation.assert_called_once_with("original")
        self.backend._resolve_image_urls.assert_not_called()

    def test_active_task_with_text_waits_for_the_later_generated_image(self):
        ready = with_tool(text_document())
        ready["mapping"]["image"] = {"message": {"author": {"role": "tool"},
            "metadata": {"async_task_type": "image_gen"},
            "content": {"content_type": "multimodal_text", "parts": [
                {"content_type": "image_asset_pointer", "asset_pointer": "file-service://generated"}]}}}
        self.backend._query_backend_tasks.return_value = [{"status": "pending"}]
        self.backend._get_conversation.side_effect = [text_document(), ready]
        self.assertEqual(self.backend._poll_image_results("original"), (["generated"], []))
        self.assertEqual(self.now, 10)

    def test_completed_text_preserves_400_code_and_does_not_submit_again(self):
        self.backend.close = Mock()
        with patch.object(protocol, "OpenAIBackendAPI", return_value=self.backend), \
             patch.object(protocol, "account_service") as pool, \
             patch.object(protocol, "conversation_events", return_value=iter([
                 {"type": "conversation.completed", "conversation_id": "original"}])), \
             patch.object(protocol, "_get_detailed_error_from_tasks", return_value=""):
            pool.get_available_access_token.return_value = "fixture-token"
            pool.get_account.return_value = {"email": "owner@example.test"}
            with self.assertRaises(protocol.ImageGenerationError) as raised:
                protocol._generate_single_image(protocol.ConversationRequest(model="gpt-image-2"), 1, 1)
            self.assertEqual(raised.exception.status_code, 400)
            self.assertEqual(raised.exception.code, "image_generation_text_response")
            self.assertEqual(raised.exception.conversation_id, "original")
            pool.get_available_access_token.assert_called_once()
            pool.mark_image_result.assert_called_once_with("fixture-token", False)
        self.backend.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
