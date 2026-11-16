import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch


class ExistingSougabot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / '.bot-token').write_text('123456:TEST_TOKEN')
        source = Path(__file__).resolve().parents[1] / 'integrations' / 'sougabot_bot.py'
        target = root / 'bot.py'
        shutil.copyfile(source, target)
        spec = importlib.util.spec_from_file_location('souga_under_test', target)
        self.bot = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.bot)
        self.bot.ADMIN.write_text('7')
        self.api = patch.object(self.bot, 'api', return_value={})
        self.mock_api = self.api.start()

    def tearDown(self):
        self.api.stop()
        self.bot.db.close()
        self.tmp.cleanup()

    def message(self, text, uid=7):
        return {'message': {'chat': {'id': uid, 'type': 'private'},
                            'from': {'id': uid}, 'text': text}}

    def test_existing_add_search_delete_work(self):
        self.bot.handle(self.message('/add 示例资源 | https://t.me/example | 搜索标签'))
        row = self.bot.db.execute('SELECT id,title,link,tags FROM resources').fetchone()
        self.assertEqual(row[1:], ('示例资源', 'https://t.me/example', '搜索标签'))
        self.bot.handle(self.message('搜索标签'))
        self.assertIn('示例资源', self.mock_api.call_args.kwargs['text'])
        self.bot.handle(self.message(f'/delete {row[0]}'))
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM resources').fetchone()[0], 0)

    def test_non_admin_cannot_add(self):
        self.bot.handle(self.message('/add 不允许 | https://t.me/example | 标签', uid=8))
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM resources').fetchone()[0], 0)

    def test_bridge_and_search_share_one_offset_loop(self):
        updates = [{'update_id': 40}, {'update_id': 41}]
        self.mock_api.return_value = updates
        bridge = NS(handle_update=Mock(side_effect=[True, False]))
        with patch.object(self.bot, 'handle') as original:
            self.assertEqual(self.bot.poll_once(40, bridge), 42)
            original.assert_called_once_with(updates[1])
        self.mock_api.assert_called_once_with('getUpdates', offset=40, timeout=25,
                                              allowed_updates=['message', 'callback_query'])

    def test_handler_failure_still_advances_offset_and_processes_next(self):
        updates = [{'update_id': 40}, {'update_id': 41}]
        self.mock_api.return_value = updates
        bridge = NS(handle_update=Mock(side_effect=[RuntimeError('failed'), False]))
        with patch.object(self.bot, 'handle') as original:
            self.assertEqual(self.bot.poll_once(40, bridge), 42)
            original.assert_called_once_with(updates[1])

    def test_callback_pagination_still_works(self):
        for i in range(6):
            self.bot.db.execute('INSERT INTO resources(title,link,tags) VALUES(?,?,?)',
                                (f'条目{i}', f'https://t.me/example{i}', '标签'))
        self.bot.db.commit()
        self.bot.sessions['key'] = (7, '标签')
        self.bot.handle({'callback_query': {
            'id': 'callback1', 'data': 'key:5', 'message': {'chat': {'id': 7}},
            'from': {'id': 7}}})
        self.assertEqual(self.mock_api.call_args_list[0].args, ('answerCallbackQuery',))
        self.assertIn('第 2 页', self.mock_api.call_args.kwargs['text'])

    def test_bridge_unavailable_has_clear_response_and_search_remains(self):
        self.bot.handle(self.message('/fetch https://t.me/epan?start=x'))
        self.assertIn('模块未启动', self.mock_api.call_args.kwargs['text'])
        self.bot.handle(self.message('普通关键词'))
        self.assertIn('没有找到相关资源', self.mock_api.call_args.kwargs['text'])
