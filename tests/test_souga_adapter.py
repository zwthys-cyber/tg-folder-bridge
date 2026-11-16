import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch
from telegram import Bot
from souga_adapter import SougaBridge, is_bridge_update


def raw_message(text='', user=7, **media):
    return {'update_id': 12, 'message': {
        'message_id': 3, 'date': 1700000000,
        'chat': {'id': user, 'type': 'private'},
        'from': {'id': user, 'is_bot': False, 'first_name': 'Test'},
        'text': text, **media}}


class Routing(unittest.TestCase):
    def test_commands_and_share_link_are_routed(self):
        for text in ('/start', '/catalog', '/help', '/speed 32 32', '/transport 2', '/diagnostics', '/fetch https://t.me/epan?start=x', '/folders', '/folder id 2', '/status id', '/resume id',
                     'https://t.me/epan?start=x'):
            self.assertTrue(is_bridge_update(raw_message(text), 7))

    def test_existing_bot_messages_remain_with_existing_handler(self):
        for text in ('/search example', '普通搜索内容', '/folder_other'):
            self.assertFalse(is_bridge_update(raw_message(text), 7))
        self.assertFalse(is_bridge_update({'callback_query': {'id': 'abc'}}, 7))

    def test_original_callback_is_routed_but_search_pagination_is_not(self):
        query = {'callback_query': {'data': 'bridge_original:job:1:2',
                                    'message': {'chat': {'type': 'private'}}}}
        self.assertTrue(is_bridge_update(query, 7))
        query['callback_query']['data'] = 'bridge_view:job:1'
        self.assertTrue(is_bridge_update(query, 7))
        query['callback_query']['data'] = 'searchkey:5'
        self.assertFalse(is_bridge_update(query, 7))

    def test_only_relay_media_is_intercepted(self):
        self.assertTrue(is_bridge_update(raw_message(user=7, video={}), 7))
        self.assertFalse(is_bridge_update(raw_message(user=8, video={}), 7))
        group = raw_message('/folders')
        group['message']['chat']['type'] = 'group'
        self.assertFalse(is_bridge_update(group, 7))


class Lifecycle(unittest.TestCase):
    def test_feed_raw_update_without_starting_an_updater(self):
        bridge = NS(relay_id=7, install=Mock(), start=AsyncMock(), stop=AsyncMock(),
                    search_text=AsyncMock(return_value=False))
        app = NS(bot=Bot('123456:TEST_TOKEN'), initialize=AsyncMock(),
                 start=AsyncMock(), stop=AsyncMock(), shutdown=AsyncMock(),
                 process_update=AsyncMock(), running=True)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ):
            config = Path(tmp) / '.env'
            config.write_text(f'DATA_DIR={tmp}/data\n')
            with patch('souga_adapter.Bridge', return_value=bridge), \
                    patch('souga_adapter.Application') as application:
                builder = application.builder.return_value
                builder.token.return_value.updater.return_value.build.return_value = app
                adapter = SougaBridge('123456:TEST_TOKEN', config)
                try:
                    adapter.start()
                    self.assertFalse(adapter.handle_update(raw_message('/search example')))
                    self.assertFalse(adapter.handle_update(raw_message('普通关键词')))
                    bridge.search_text.assert_awaited_once_with(7, 7, '普通关键词')
                    bridge.search_text.return_value = True
                    self.assertTrue(adapter.handle_update(raw_message('文件夹名称')))
                    app.process_update.assert_not_awaited()
                    self.assertTrue(adapter.handle_update(raw_message('/folders')))
                    app.process_update.assert_awaited_once()
                    self.assertEqual(app.process_update.call_args.args[0].update_id, 12)
                    builder.token.return_value.updater.assert_called_once_with(None)
                    bridge.start.assert_awaited_once_with(app)
                finally:
                    adapter.close()
                bridge.stop.assert_awaited_once_with(app)
                app.shutdown.assert_awaited_once()
                self.assertIsNone(adapter.thread)

    def test_missing_config_does_not_start_a_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = SougaBridge('123456:TEST_TOKEN', Path(tmp) / 'absent.env')
            with self.assertRaises(FileNotFoundError):
                adapter.start()
            self.assertIsNone(adapter.thread)
