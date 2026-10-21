from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault('CHATGPT2API_AUTH_KEY', 'test-auth')

import services.image_task_service as tasks
import services.openai_backend_api as backend_module
from services.openai_backend_api import ImagePollTimeoutError, OpenAIBackendAPI
from utils.helper import UpstreamHTTPError

OWNER = {'id': 'resume-owner', 'name': 'Owner', 'role': 'admin'}
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jPRsAAAAASUVORK5CYII=')


class ResumeTaskTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'tasks.json'
        self.path.write_text(json.dumps({'tasks': [{
            'id': 'old-edit', 'owner_id': OWNER['id'], 'status': 'error',
            'mode': 'edit', 'model': 'gpt-image-2', 'size': '1024x1024',
            'quality': 'medium', 'created_at': '2099-01-01 00:00:00',
            'updated_at': '2099-01-01 00:00:00', 'conversation_id': 'original-conversation',
            'account_email': 'original@example.test', 'error': 'ChatGPT 生图超时（120秒）',
        }]}))
        self.handler = Mock(side_effect=AssertionError('must not regenerate'))
        self.service = tasks.ImageTaskService(self.path, generation_handler=self.handler,
            edit_handler=self.handler, retention_days_getter=lambda: 30)
        self.key = OWNER['id'] + ':old-edit'
        self.backend = Mock()
        self.backend._add_unique = OpenAIBackendAPI._add_unique
        self.backend._extract_image_tool_records.return_value = [
            {'file_ids': ['generated-file'], 'sediment_ids': []}]
        self.backend.resolve_conversation_image_urls.return_value = ['https://upstream.invalid/image']
        self.backend.download_image_bytes.return_value = [PNG]
        self.lookup = patch.object(tasks, '_resume_backend', return_value=(self.backend, {'mapping': {}}))
        self.lookup_mock = self.lookup.start()
        self.addCleanup(self.lookup.stop)
        self.log = patch.object(self.service, '_log_call').start()
        self.addCleanup(patch.stopall)

    def run_resume(self):
        self.service._run_resume_poll(self.key, 'original-conversation', 30, OWNER,
            'edit', 'gpt-image-2', 'https://public.example')
        return self.service.list_tasks(OWNER, ['old-edit'])['items'][0]

    def test_restart_retains_conversation_and_private_origin_but_public_task_has_no_account(self):
        public = self.service.list_tasks(OWNER, ['old-edit'])['items'][0]
        self.assertEqual(public['conversation_id'], 'original-conversation')
        self.assertNotIn('account_email', public)
        self.assertEqual(self.service._tasks[self.key]['account_email'], 'original@example.test')

    def test_timeout_saves_origin_account_for_a_later_authenticated_resume(self):
        error = ImagePollTimeoutError('超时')
        error.conversation_id = 'new-conversation'
        error.account_email = 'new@example.test'
        self.handler.side_effect = error
        self.service._run_task(self.key, 'edit', {}, OWNER, 'gpt-image-2')
        reloaded = tasks.ImageTaskService(self.path, retention_days_getter=lambda: 30)
        self.assertEqual(reloaded._tasks[self.key]['conversation_id'], 'new-conversation')
        self.assertEqual(reloaded._tasks[self.key]['account_email'], 'new@example.test')
        self.assertNotIn('access_token', self.path.read_text())

    def test_ready_result_is_downloaded_and_saved_without_polling_or_regenerating(self):
        with patch('services.protocol.conversation.save_image_bytes', return_value='https://public.example/images/recovered.png') as save:
            result = self.run_resume()
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['data'], [{'url': 'https://public.example/images/recovered.png', 'revised_prompt': ''}])
        self.lookup_mock.assert_called_once_with('original-conversation', 'original@example.test')
        self.backend._poll_image_results.assert_not_called()
        self.backend.resolve_conversation_image_urls.assert_called_once_with('original-conversation', ['generated-file'], [], poll=False)
        self.backend.close.assert_called_once()
        self.handler.assert_not_called()
        save.assert_called_once_with(PNG, 'https://public.example')
        persisted = tasks.ImageTaskService(self.path, retention_days_getter=lambda: 30)
        self.assertEqual(persisted.list_tasks(OWNER, ['old-edit'])['items'][0]['status'], 'success')
        self.assertNotIn('b64_json', json.dumps(result))
        with patch.object(tasks.threading, 'Thread') as thread:
            self.assertEqual(self.service.resume_poll(OWNER, 'old-edit')['status'], 'success')
            thread.assert_not_called()

    def test_pending_result_waits_on_the_same_conversation(self):
        self.backend._extract_image_tool_records.return_value = []
        self.backend._poll_image_results.return_value = (['later-file'], [])
        with patch('services.protocol.conversation.save_image_bytes', return_value='/images/later.png'):
            result = self.run_resume()
        self.assertEqual(result['status'], 'success')
        self.backend._poll_image_results.assert_called_once_with('original-conversation', 30)
        self.handler.assert_not_called()

    def test_another_timeout_preserves_original_conversation_and_can_be_resumed_again(self):
        self.backend._extract_image_tool_records.return_value = []
        self.backend._poll_image_results.side_effect = ImagePollTimeoutError('still pending')
        result = self.run_resume()
        self.assertEqual(result['status'], 'error')
        self.assertIn('超时', result['error'])
        self.assertEqual(result['conversation_id'], 'original-conversation')
        self.backend.close.assert_called_once()
        with patch.object(tasks.threading, 'Thread') as thread:
            response = self.service.resume_poll(OWNER, 'old-edit', 30, base_url='https://public.example')
        self.assertEqual(response['status'], 'running')
        thread.return_value.start.assert_called_once()

    def test_terminal_text_on_resume_shows_the_reply_without_another_timeout_button(self):
        self.backend._extract_image_tool_records.return_value = []
        self.backend._poll_image_results.side_effect = backend_module.ImageTextResponseError(
            '请上传需要修改的原图', 'original-conversation')
        result = self.run_resume()
        self.assertEqual(result['status'], 'error')
        self.assertIn('请上传', result['error'])
        self.assertNotIn('超时', result['error'])
        self.backend.close.assert_called_once()
        self.handler.assert_not_called()

    def test_empty_download_is_not_reported_as_success(self):
        self.backend.download_image_bytes.return_value = []
        result = self.run_resume()
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['data'], [])
        self.backend.close.assert_called_once()

    def test_old_proxy_failure_is_recoverable_after_restart(self):
        raw = json.loads(self.path.read_text())
        raw['tasks'][0]['error'] = "'ConfigStore' object has no attribute 'proxy_url'"
        self.path.write_text(json.dumps(raw))
        service = tasks.ImageTaskService(self.path, retention_days_getter=lambda: 30)
        self.assertIn('超时', service.list_tasks(OWNER, ['old-edit'])['items'][0]['error'])
        with patch.object(tasks.threading, 'Thread'):
            response = service.resume_poll(OWNER, 'old-edit')
        self.assertEqual(response['status'], 'running')

    def test_owner_check_and_duplicate_resume_cannot_start_another_worker(self):
        with patch.object(tasks.threading, 'Thread') as thread:
            with self.assertRaisesRegex(ValueError, 'not found'):
                self.service.resume_poll({'id': 'someone-else'}, 'old-edit')
            self.service.resume_poll(OWNER, 'old-edit')
            self.assertEqual(self.service.resume_poll(OWNER, 'old-edit')['status'], 'running')
        self.assertEqual(thread.call_count, 1)


class ResumeAccountTests(unittest.TestCase):
    def test_authenticates_the_original_account_and_does_not_acquire_a_generation_slot(self):
        accounts = [
            {'access_token': 'wrong-token', 'email': 'other@example.test'},
            {'access_token': 'original-token', 'email': 'original@example.test', 'status': '限流', 'quota': 0},
        ]
        backend = Mock()
        backend._get_conversation.return_value = {'mapping': {}}
        with patch.object(tasks, 'account_service') as pool, patch.object(backend_module, 'OpenAIBackendAPI', return_value=backend) as constructor:
            pool.list_accounts.return_value = accounts
            pool.refresh_access_token.return_value = 'refreshed-original-token'
            result, document = tasks._resume_backend('existing-conversation', 'original@example.test')
        self.assertIs(result, backend)
        constructor.assert_called_once_with('refreshed-original-token')
        pool.refresh_access_token.assert_called_once_with('original-token', event='image_resume_poll')
        pool.get_available_access_token.assert_not_called()
        backend._get_conversation.assert_called_once_with('existing-conversation')

    def test_legacy_task_locates_owner_by_reading_conversation_and_closes_wrong_account(self):
        wrong, right = Mock(), Mock()
        wrong._get_conversation.side_effect = UpstreamHTTPError('conversation', 404, {})
        right._get_conversation.return_value = {'mapping': {}}
        with patch.object(tasks, 'account_service') as pool, patch.object(backend_module, 'OpenAIBackendAPI', side_effect=[wrong, right]):
            pool.list_accounts.return_value = [{'access_token': 'first'}, {'access_token': 'second'}]
            pool.refresh_access_token.side_effect = lambda token, **kwargs: token
            result, _ = tasks._resume_backend('existing-conversation', '')
        self.assertIs(result, right)
        wrong.close.assert_called_once()
        right.close.assert_not_called()

    def test_missing_original_account_does_not_use_someone_elses_account(self):
        with patch.object(tasks, 'account_service') as pool, patch.object(backend_module, 'OpenAIBackendAPI') as constructor:
            pool.list_accounts.return_value = [{'access_token': 'other', 'email': 'other@example.test'}]
            with self.assertRaisesRegex(RuntimeError, '原账号'):
                tasks._resume_backend('existing-conversation', 'original@example.test')
        constructor.assert_not_called()


class LongImageWaitTests(unittest.TestCase):
    def test_timeout_does_not_submit_another_generation_when_original_conversation_exists(self):
        import services.protocol.conversation as conversation
        error = ImagePollTimeoutError('超时')
        error.conversation_id = 'still-generating'
        backend = Mock()
        with patch.object(conversation, 'account_service') as pool, patch.object(conversation, 'OpenAIBackendAPI', return_value=backend), patch.object(conversation, 'stream_image_outputs', side_effect=error):
            pool.get_available_access_token.return_value = 'test-token'
            pool.get_account.return_value = {'email': 'original@example.test'}
            with self.assertRaises(ImagePollTimeoutError) as raised:
                conversation._generate_single_image(conversation.ConversationRequest(model='gpt-image-2'), 0, 1)
        self.assertEqual(raised.exception.conversation_id, 'still-generating')
        pool.get_available_access_token.assert_called_once()
        pool.mark_image_result.assert_called_once_with('test-token', False)
        backend.close.assert_called_once()

    def test_image_completed_after_five_minutes_survives_the_ten_minute_budget(self):
        backend = object.__new__(OpenAIBackendAPI)
        backend._query_backend_tasks = Mock(return_value=[])
        now = [0.0]
        backend._get_conversation = Mock(side_effect=lambda _: {'ready': now[0] >= 300})
        backend._extract_image_tool_records = lambda document: [{'file_ids': ['late-file'], 'sediment_ids': []}] if document['ready'] else []
        backend._find_content_policy_error_in_conversation = lambda _: ''
        backend._resolve_image_urls = Mock(return_value=['https://example.test/late.png'])
        settings = SimpleNamespace(image_poll_timeout_secs=600, image_poll_interval_secs=10,
            image_poll_initial_wait_secs=0, image_settle_enabled=False, image_check_before_hit_enabled=False)
        with patch.object(backend_module, 'config', settings), patch.object(backend_module.time, 'time', side_effect=lambda: now[0]), patch.object(backend_module.time, 'sleep', side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)):
            urls = backend.resolve_conversation_image_urls('slow-conversation', [], [])
        self.assertEqual(urls, ['https://example.test/late.png'])
        self.assertEqual(now[0], 300)
        self.assertGreater(backend._get_conversation.call_count, 12)


if __name__ == '__main__':
    unittest.main()
