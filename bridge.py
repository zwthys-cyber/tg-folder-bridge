"""Telegram user receives files and forwards to the bot; bot stores file_ids.
Never calls download_media, download_file, getFile or uploads media bytes.
"""
import asyncio
import contextlib
import os
import re
import uuid
from pathlib import Path
from telegram.error import BadRequest, RetryAfter
from telegram.ext import ApplicationHandlerStop, CommandHandler, MessageHandler, filters
from telethon import TelegramClient, events
from core import Store, media_count, parse_link


class Bridge:
    def __init__(self):
        self.owners = {int(x) for x in os.environ['OWNER_IDS'].split(',') if x.strip()}
        self.relay_id = int(os.environ['RELAY_USER_ID'])
        self.allowed = {x.strip().lower() for x in os.getenv(
            'ALLOWED_SOURCE_BOTS', 'epan,SPXM111_bot').split(',') if x.strip()}
        self.labels = [x.strip() for x in os.getenv(
            'BUTTON_LABELS', '获取文件,下载全部,全部下载,下一页,获取全部,下载文件').split(',')]
        self.idle = float(os.getenv('IDLE_SECONDS', '45'))
        self.timeout = float(os.getenv('JOB_TIMEOUT_SECONDS', '3600'))
        if not self.owners or self.idle <= 0 or self.timeout <= 0:
            raise ValueError('OWNER_IDS 和超时配置无效')
        root = Path(os.getenv('DATA_DIR', 'data'))
        root.mkdir(parents=True, exist_ok=True)
        self.store = Store(root / 'index.sqlite3')
        self.client = TelegramClient(str(root / 'relay'), int(os.environ['API_ID']),
                                     os.environ['API_HASH'])
        self.jobs = asyncio.Queue(maxsize=20)
        self.active = None
        self.pending = None
        self.blocked = False
        self.worker = None

    def install(self, app):
        # Negative group runs before ordinary existing bot handlers. Reserve -100.
        app.add_handler(MessageHandler(filters.User(self.relay_id) & filters.ATTACHMENT,
                                       self.receive_file), group=-100)
        app.add_handler(CommandHandler('fetch', self.fetch), group=-100)
        app.add_handler(CommandHandler('folder', self.folder), group=-100)
        app.add_handler(CommandHandler('folders', self.folders), group=-100)
        app.add_handler(MessageHandler(filters.Regex(r'^https://t\.me/\w+\?start='),
                                       self.fetch), group=-100)

    async def start(self, app):
        self.app = app
        await self.client.connect()
        if not await self.client.is_user_authorized():
            await self.client.disconnect()
            raise RuntimeError('请先在部署服务器执行 python login.py')
        me = await self.client.get_me()
        if me.bot or me.id != self.relay_id:
            await self.client.disconnect()
            raise RuntimeError('登录账号与 RELAY_USER_ID 不一致')
        self.destination = (await app.bot.get_me()).username
        self.client.add_event_handler(self.source_message, events.NewMessage(incoming=True))
        self.client.add_event_handler(self.source_message, events.MessageEdited(incoming=True))
        self.worker = asyncio.create_task(self.work())

    async def stop(self, app):
        if self.worker:
            self.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.worker
        if self.active:
            self.store.update(self.active['id'], 'interrupted', self.active['expected'])
        await self.client.disconnect()
        self.store.db.close()

    async def fetch(self, update, context):
        if update.effective_chat.type != 'private' or update.effective_user.id not in self.owners:
            raise ApplicationHandlerStop
        if self.blocked:
            await update.message.reply_text('收件确认超时，已停止新任务。请检查部署日志后重启服务。')
            raise ApplicationHandlerStop
        text = update.message.text.strip()
        link = text.split(maxsplit=1)[1] if text.startswith('/fetch') and ' ' in text else text
        try:
            parse_link(link, self.allowed)
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            raise ApplicationHandlerStop from exc
        if self.jobs.full():
            await update.message.reply_text('队列已满，请稍后重试。')
            raise ApplicationHandlerStop
        job = uuid.uuid4().hex[:16]
        self.store.create(job, update.effective_user.id, link)
        self.jobs.put_nowait(job)
        await update.message.reply_text(f'已排队：{job}\n查看或取回文件：/folder {job}')
        raise ApplicationHandlerStop

    async def folder(self, update, context):
        if update.effective_chat.type != 'private' or update.effective_user.id not in self.owners:
            raise ApplicationHandlerStop
        args = context.args
        job = self.store.get(args[0]) if args else None
        if not job or job['owner'] != update.effective_user.id:
            await update.message.reply_text('用法：/folder 任务编号 [页码]；每页 10 个文件。')
            raise ApplicationHandlerStop
        try:
            page = int(args[1]) if len(args) > 1 else 1
            if page < 1:
                raise ValueError
        except ValueError:
            await update.message.reply_text('页码必须是正整数。')
            raise ApplicationHandlerStop
        files = self.store.files(job['id'])
        await update.message.reply_text(
            f"任务：{job['id']}\n状态：{job['state']}\n"
            f"已保存 {len(files)} / 预期 {job['expected'] if job['expected'] is not None else '未知'} 个文件\n"
            f"{job['detail']}\n第 {page} 页，共 {max(1, (len(files)+9)//10)} 页")
        for item in files[(page-1)*10:page*10]:
            try:
                await self.send_stored(context.bot, update.effective_chat.id, item)
            except BadRequest:
                await update.message.reply_text('一个文件无法通过 Telegram 重新发送，其余文件继续处理。')
            await asyncio.sleep(1)
        raise ApplicationHandlerStop

    async def folders(self, update, context):
        if update.effective_chat.type != 'private' or update.effective_user.id not in self.owners:
            raise ApplicationHandlerStop
        jobs = self.store.list_jobs(update.effective_user.id)
        text = '\n\n'.join(f"{x['state']}\n/folder {x['id']}" for x in jobs)
        await update.message.reply_text(text or '还没有文件夹。发送分享链接即可导入。')
        raise ApplicationHandlerStop

    async def send_stored(self, bot, chat_id, item):
        # Use this bot's own file_id: retrieval survives deletion of the inbox message.
        kind = item['kind']
        if kind not in {'photo', 'video', 'document', 'animation', 'audio', 'voice', 'video_note'}:
            raise ValueError('Unsupported stored media type')
        kwargs = dict(chat_id=chat_id, **{kind: item['file_id']})
        if kind != 'video_note':
            kwargs['caption'] = item.get('caption', '')[:1024]
            kwargs['parse_mode'] = None
        method = getattr(bot, f'send_{kind}')
        try:
            return await method(**kwargs)
        except RetryAfter as exc:
            delay = exc.retry_after
            await asyncio.sleep(delay.total_seconds() + 1 if hasattr(delay, 'total_seconds') else delay + 1)
            return await method(**kwargs)

    async def receive_file(self, update, context):
        message = update.effective_message
        pending = self.pending
        if (update.effective_chat.type != 'private' or not message.forward_origin
                or not pending or pending['future'].done()):
            # Consume relay messages so existing handlers don't answer each file.
            raise ApplicationHandlerStop
        for kind in ('photo', 'video', 'document', 'animation', 'audio', 'voice', 'video_note'):
            media = getattr(message, kind, None)
            if media:
                if kind == 'photo':
                    media = media[-1]
                self.store.add_file(self.active['id'], pending['source'], message, media.file_id, kind)
                pending['future'].set_result(True)
                break
        raise ApplicationHandlerStop

    async def source_message(self, event):
        job = self.active
        if not job or event.chat_id not in job['chats']:
            return
        if event.id <= job['baselines'][event.chat_id]:
            return
        try:
            job['events'].put_nowait(event.message)
        except asyncio.QueueFull:
            job['overflow'] = True

    async def work(self):
        while True:
            job_id = await self.jobs.get()
            if self.blocked:
                self.store.update(job_id, 'blocked', detail='前一任务收件未确认，请重启后重新提交。')
                self.jobs.task_done()
                continue
            row = self.store.get(job_id)
            job = dict(id=job_id, expected=None, chats=set(), baselines={},
                       events=asyncio.Queue(maxsize=5000), overflow=False)
            self.active = job
            self.store.update(job_id, 'running')
            try:
                await asyncio.wait_for(self.collect(row['link'], job), self.timeout)
                n = len(self.store.files(job_id))
                complete = job['expected'] is not None and n == job['expected']
                self.store.update(job_id, 'complete' if complete else 'needs_review', job['expected'],
                                  '' if complete else '发送已静默，无法确认全部文件已收到。')
            except asyncio.CancelledError:
                self.store.update(job_id, 'interrupted', job['expected'])
                raise
            except Exception as exc:
                # Do not log links, session secrets, or raw Telegram response contents.
                self.store.update(job_id, 'partial' if self.store.files(job_id) else 'failed',
                                  job['expected'], type(exc).__name__)
            finally:
                self.pending = None
                self.active = None
                self.jobs.task_done()
            with contextlib.suppress(Exception):
                await self.app.bot.send_message(row['owner'],
                    f"任务 {job_id}：{self.store.get(job_id)['state']}\n/folder {job_id}")

    async def collect(self, link, job):
        visited = set()
        clicked = set()
        seen = set()
        actions = 0

        async def follow(url):
            nonlocal actions
            name, param = parse_link(url, self.allowed)
            if (name, param) in visited:
                return
            actions += 1
            if actions > 100:
                raise RuntimeError('按钮跳转次数超限')
            entity = await self.client.get_entity(name)
            if not getattr(entity, 'bot', False):
                raise ValueError('跳转目标不是机器人')
            if entity.id not in job['chats']:
                last = await self.client.get_messages(entity, limit=1)
                job['baselines'][entity.id] = last[0].id if last else 0
                job['chats'].add(entity.id)
            visited.add((name, param))
            await self.client.send_message(entity, f'/start {param}')

        await follow(link)
        while True:
            if job['overflow']:
                raise RuntimeError('收件队列超限')
            try:
                message = await asyncio.wait_for(job['events'].get(), self.idle)
            except asyncio.TimeoutError:
                return
            if job['expected'] is None:
                job['expected'] = media_count(message.raw_text or '')
                self.store.update(job['id'], 'running', job['expected'])
            key = (message.chat_id, message.id)
            if (message.photo or message.document) and key not in seen:
                seen.add(key)
                future = asyncio.get_running_loop().create_future()
                self.pending = dict(source=key, future=future)
                # Cloud forwarding only; protected content errors surface as task failure.
                try:
                    await self.client.forward_messages(self.destination, message)
                    await asyncio.wait_for(future, 60)
                except BaseException:
                    # Prevent delayed forwards from being attributed to the next task.
                    self.blocked = True
                    raise
                finally:
                    self.pending = None
                await asyncio.sleep(1)
            for row in message.buttons or []:
                for button in row:
                    label = re.sub(r'\s+', '', button.text or '')
                    if not any(re.sub(r'\s+', '', x) in label for x in self.labels if x):
                        continue
                    url = getattr(button, 'url', None)
                    data = getattr(button, 'data', None)
                    signature = (*key, url, data)
                    if signature in clicked:
                        continue
                    clicked.add(signature)
                    if url:
                        await follow(url)
                    elif data is not None:
                        actions += 1
                        if actions > 100:
                            raise RuntimeError('按钮跳转次数超限')
                        await button.click()
                    await asyncio.sleep(1)
