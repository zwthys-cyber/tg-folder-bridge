"""Feed raw getUpdates dictionaries from an existing synchronous Python bot.

This adapter has no Updater and never calls getUpdates or changes a webhook.
The existing bot remains responsible for fetching updates and advancing offsets.
"""
import asyncio
import re
import threading
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application
from bridge import Bridge
from bot_api_config import configure


def is_bridge_update(raw, relay_id):
    callback = raw.get('callback_query', {})
    if (isinstance(callback.get('data'), str)
            and callback['data'].startswith(('bridge_original:', 'bridge_catalog:', 'bridge_open:', 'bridge_search:'))
            and callback.get('message', {}).get('chat', {}).get('type') == 'private'):
        return True
    message = raw.get('message')
    if not isinstance(message, dict):
        return False
    if message.get('chat', {}).get('type') != 'private':
        return False
    text = message.get('text', '').strip()
    if re.match(r'^/(start|help|catalog|fetch|folder|folders|status|resume|speed|transport|diagnostics|resolve|dedup)(?:@\w+)?(?:\s|$)', text):
        return True
    if re.match(r'^https://t\.me/\w+\?start=', text):
        return True
    return (message.get('from', {}).get('id') == relay_id and
            any(key in message for key in (
                'photo', 'video', 'document', 'animation', 'audio', 'voice', 'video_note')))


class SougaBridge:
    def __init__(self, bot_token, env_file, timeout=120):
        self.token = bot_token
        self.env_file = Path(env_file).resolve()
        self.timeout = timeout
        self.loop = None
        self.thread = None
        self.bridge = None
        self.app = None

    def start(self):
        if self.thread:
            raise RuntimeError('SougaBridge 已启动，不能重复启动')
        if not self.env_file.is_file():
            raise FileNotFoundError('请先配置转存模块的 .env 文件')
        self.loop = asyncio.new_event_loop()

        def run():
            asyncio.set_event_loop(self.loop)
            self.loop.run_forever()
            self.loop.close()

        self.thread = threading.Thread(target=run, name='tg-folder-bridge', daemon=True)
        self.thread.start()
        try:
            self._submit(self._start())
        except BaseException:
            self.close()
            raise
        return self

    def _submit(self, coroutine):
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return future.result(self.timeout)
        except FutureTimeout:
            future.cancel()
            raise TimeoutError('转存模块响应超时，请检查部署服务器日志') from None

    async def _start(self):
        # DATA_DIR must be absolute, so the existing bot's working directory has
        # no effect and its resources.db is never opened by this module.
        load_dotenv(self.env_file, override=False)
        import os
        configured = Path(os.getenv('DATA_DIR', 'data'))
        if not configured.is_absolute():
            os.environ['DATA_DIR'] = str(self.env_file.parent / configured)
        self.bridge = Bridge()
        self.app = configure(Application.builder().token(self.token).updater(None)).build()
        self.bridge.install(self.app)
        await self.app.initialize()
        await self.app.start()
        await self.bridge.start(self.app)

    def handle_update(self, raw):
        """True: consumed by bridge. False: continue the existing bot handler.

        Invoke once for each raw update, in the same order as getUpdates returns.
        Don't call from this adapter's asyncio thread.
        """
        if not self.bridge or not self.app:
            raise RuntimeError('请先调用 SougaBridge.start()')
        if not is_bridge_update(raw, self.bridge.relay_id):
            message = raw.get('message', {})
            text = message.get('text', '').strip()
            if (message.get('chat', {}).get('type') == 'private' and 0 < len(text) <= 100
                    and not text.startswith('/')):
                return self._submit(self.bridge.search_text(message['chat']['id'], message['from']['id'], text))
            return False

        async def process():
            update = Update.de_json(raw, self.app.bot)
            await self.app.process_update(update)

        self._submit(process())
        return True

    async def _stop(self):
        if self.bridge:
            await self.bridge.stop(self.app)
        if self.app:
            if self.app.running:
                await self.app.stop()
            await self.app.shutdown()

    def close(self):
        if not self.thread:
            return
        try:
            self._submit(self._stop())
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(self.timeout)
            self.thread = None
