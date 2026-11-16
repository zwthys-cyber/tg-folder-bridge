"""Transfer received media to the bot, which stores its own file_ids.
Forward-only by default; opt-in download/upload uses a bounded, cleaned spool.
"""
import asyncio
import contextlib
import os
import re
import time
import uuid
from pathlib import Path
from telegram import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, RetryAfter
from telegram.ext import ApplicationHandlerStop, CallbackQueryHandler, CommandHandler, MessageHandler, filters
from telethon import TelegramClient, events, types
from core import Store, folder_title, media_count, media_key, pagination_marker, parse_link
from media_spool import MediaSpool
from previews import document_thumbnail, make_jpeg_preview
from fast_upload import upload_file
from catalog_ui import catalog_page, send_file_page


class Bridge:
    def __init__(self):
        self.owners = {int(x) for x in os.environ['OWNER_IDS'].split(',') if x.strip()}
        self.relay_id = int(os.environ['RELAY_USER_ID'])
        self.allowed = {x.strip().lower() for x in os.getenv(
            'ALLOWED_SOURCE_BOTS', 'epan,SPXM111_bot').split(',') if x.strip()}
        self.labels = [x.strip() for x in os.getenv(
            'BUTTON_LABELS', '获取文件,下载全部,全部下载,下一页,获取全部,下载文件').split(',')]
        # Existing deployments already have BUTTON_LABELS set; keep them working
        # without requiring a manual .env edit for this source's batch button.
        if '下一组' not in self.labels:
            self.labels.append('下一组')
        self.idle = float(os.getenv('IDLE_SECONDS', '45'))
        self.timeout = float(os.getenv('JOB_TIMEOUT_SECONDS', '3600'))
        self.transfer_pause = float(os.getenv('TRANSFER_PAUSE_SECONDS', '0.2'))
        self.heic_previews = os.getenv('HEIC_PREVIEWS', '1') == '1'
        self.document_groups = os.getenv('DISPLAY_MODE', 'document_group') == 'document_group'
        self.upload_workers = int(os.getenv('UPLOAD_WORKERS', '3'))
        if not 1 <= self.upload_workers <= 4 or self.transfer_pause < 0:
            raise ValueError('UPLOAD_WORKERS must be 1..4; transfer pause must be nonnegative')
        if not self.owners or self.idle <= 0 or self.timeout <= 0:
            raise ValueError('OWNER_IDS 和超时配置无效')
        root = Path(os.getenv('DATA_DIR', 'data'))
        root.mkdir(parents=True, exist_ok=True)
        self.transfer_mode = os.getenv('TRANSFER_MODE', 'forward')
        if self.transfer_mode not in {'forward', 'download_upload'}:
            raise ValueError('TRANSFER_MODE must be forward or download_upload')
        self.spool = None
        if self.transfer_mode == 'download_upload':
            self.spool = MediaSpool(
                root / 'spool',
                int(float(os.getenv('MAX_MEDIA_FILE_GB', '4')) * 1024**3),
                int(float(os.getenv('MIN_FREE_DISK_GB', '10')) * 1024**3))
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
        app.add_handler(CommandHandler('start', self.start_command), group=-100)
        app.add_handler(CommandHandler('catalog', self.start_command), group=-100)
        app.add_handler(CommandHandler('help', self.help_command), group=-100)
        app.add_handler(CommandHandler('folder', self.folder), group=-100)
        app.add_handler(CommandHandler('folders', self.folders), group=-100)
        app.add_handler(CommandHandler('status', self.status), group=-100)
        app.add_handler(CommandHandler('resume', self.resume), group=-100)
        app.add_handler(CallbackQueryHandler(self.original, pattern=r'^bridge_original:'), group=-100)
        app.add_handler(CallbackQueryHandler(self.catalog_callback, pattern=r'^bridge_(catalog|open):'), group=-100)
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
        print('文件转存模式：' + self.transfer_mode, flush=True)
        print('中转账号 Premium：' + ('已开通' if getattr(me, 'premium', False) else '未开通'), flush=True)
        await self.update_menu(app.bot)
        self.client.add_event_handler(self.source_message, events.NewMessage(incoming=True))
        self.client.add_event_handler(self.source_message, events.MessageEdited(incoming=True))
        self.worker = asyncio.create_task(self.work())

    async def update_menu(self, bot):
        public = [BotCommand('start', '查看文件夹列表'), BotCommand('catalog', '浏览文件夹'),
                  BotCommand('help', '使用帮助')]
        await bot.set_my_commands(public)
        await bot.set_my_commands(public, scope=BotCommandScopeAllPrivateChats())
        for owner in self.owners:
            try:
                await bot.set_my_commands(public + [BotCommand('fetch', '导入分享文件夹'),
                    BotCommand('folders', '查看导入任务'), BotCommand('status', '查看传输进度'),
                    BotCommand('resume', '继续导入任务'), BotCommand('add', '收录搜索资源'),
                    BotCommand('delete', '删除搜索资源')], scope=BotCommandScopeChat(owner))
            except BadRequest:
                pass  # This owner may not have opened the bot yet.

    def can_view(self, job, uid):
        return bool(job and ((uid in self.owners and job['owner'] == uid) or job['state'] == 'complete'))

    async def start_command(self, update, context):
        if update.effective_chat.type == 'private':
            text, markup = catalog_page(self.store.catalog())
            await context.bot.send_message(update.effective_chat.id, text, reply_markup=markup)
        raise ApplicationHandlerStop

    async def help_command(self, update, context):
        if update.effective_chat.type != 'private':
            raise ApplicationHandlerStop
        text = '发送 /start 查看文件夹列表，点击编号打开文件夹。\n文件按组显示缩略图、文件名和大小。\n直接发送关键词仍可搜索原收录资源。'
        if update.effective_user.id in self.owners:
            text += '\n\n管理员：\n/fetch 分享链接\n/folders 导入任务\n/status 任务编号\n/resume 任务编号\n/add 标题 | 链接 | 关键词\n/delete 编号'
        await update.message.reply_text(text)
        raise ApplicationHandlerStop

    async def catalog_callback(self, update, context):
        query = update.callback_query
        if update.effective_chat.type != 'private':
            raise ApplicationHandlerStop
        try:
            parts = query.data.split(':')
            if parts[0] == 'bridge_catalog':
                page = int(parts[1])
                await query.answer()
                text, markup = catalog_page(self.store.catalog(), page)
                await query.edit_message_text(text, reply_markup=markup)
            else:
                job = self.store.get(parts[1])
                if not self.can_view(job, update.effective_user.id):
                    await query.answer('文件夹尚未就绪或已移除', show_alert=True)
                    raise ApplicationHandlerStop
                page = max(1, int(parts[2]))
                await query.answer()
                await send_file_page(context.bot, update.effective_chat.id, job,
                                     self.store.files(job['id']), page, self.send_stored)
        except (ValueError, IndexError):
            await query.answer('列表已更新，请重新发送 /start', show_alert=True)
        raise ApplicationHandlerStop

    async def stop(self, app):
        if self.worker:
            self.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.worker
        if self.active:
            self.store.update(self.active['id'], 'interrupted', self.active['expected'])
        await self.client.disconnect()
        self.store.db.close()
        if self.spool:
            self.spool.close()

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
        if update.effective_chat.type != 'private':
            raise ApplicationHandlerStop
        args = context.args
        job = self.store.get(args[0]) if args else None
        if not self.can_view(job, update.effective_user.id):
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
        if self.document_groups:
            await send_file_page(context.bot, update.effective_chat.id, job, files, page, self.send_stored)
            raise ApplicationHandlerStop
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

    async def status(self, update, context):
        if update.effective_chat.type != 'private' or update.effective_user.id not in self.owners:
            raise ApplicationHandlerStop
        job = self.store.get(context.args[0]) if context.args else None
        if not job or job['owner'] != update.effective_user.id:
            await update.message.reply_text('用法：/status 任务编号')
            raise ApplicationHandlerStop
        n = len(self.store.files(job['id']))
        await update.message.reply_text(
            f"任务：{job['id']}\n状态：{job['state']}\n"
            f"已保存 {n} / {job['expected'] if job['expected'] is not None else '未知'} 个原文件\n"
            f"{job['detail']}")
        raise ApplicationHandlerStop

    async def resume(self, update, context):
        if update.effective_chat.type != 'private' or update.effective_user.id not in self.owners:
            raise ApplicationHandlerStop
        job = self.store.get(context.args[0]) if context.args else None
        if not job or job['owner'] != update.effective_user.id:
            await update.message.reply_text('用法：/resume 原任务编号')
        elif self.blocked or self.jobs.full():
            await update.message.reply_text('服务暂不能接收续传任务；收件未确认时请先重启。')
        elif job['state'] in {'queued', 'running'}:
            await update.message.reply_text('此任务仍在排队或运行，请用 /status 查看。')
        else:
            self.store.update(job['id'], 'queued', job['expected'])
            self.jobs.put_nowait(job['id'])
            await update.message.reply_text(
                f"已排队续传：{job['id']}\n识别并跳过已保存原文件，HEIC 可补充预览。\n/status {job['id']}")
        raise ApplicationHandlerStop

    async def original(self, update, context):
        query = update.callback_query
        if update.effective_user.id not in self.owners or update.effective_chat.type != 'private':
            await query.answer('没有权限', show_alert=True)
            raise ApplicationHandlerStop
        try:
            _, job_id, chat, message = query.data.split(':')
            job = self.store.get(job_id)
            if not job or job['owner'] != update.effective_user.id:
                raise ValueError
            item = next(x for x in self.store.files(job_id)
                        if x['source_chat'] == int(chat) and x['source_message'] == int(message))
        except (ValueError, StopIteration):
            await query.answer('文件不存在或无权查看', show_alert=True)
            raise ApplicationHandlerStop
        await query.answer()
        await self.send_stored(context.bot, update.effective_chat.id, item, use_preview=False)
        raise ApplicationHandlerStop

    async def send_stored(self, bot, chat_id, item, use_preview=True):
        # Use this bot's own file_id: retrieval survives deletion of the inbox message.
        if use_preview and item.get('preview_file_id'):
            markup = InlineKeyboardMarkup([[InlineKeyboardButton(
                '下载 HEIC 原图', callback_data=(f"bridge_original:{item['job']}:"
                                             f"{item['source_chat']}:{item['source_message']}"))]])
            kwargs = dict(chat_id=chat_id, photo=item['preview_file_id'],
                          caption=item.get('caption', '')[:1024], parse_mode=None, reply_markup=markup)
            method = bot.send_photo
        else:
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
        if (update.effective_chat.type != 'private'
                or not pending or pending['future'].done()):
            # Consume relay messages so existing handlers don't answer each file.
            raise ApplicationHandlerStop
        marker = pending.get('marker')
        if marker:
            if getattr(message, 'caption', None) != marker:
                raise ApplicationHandlerStop
        elif not message.forward_origin:
            raise ApplicationHandlerStop
        for kind in ('photo', 'video', 'document', 'animation', 'audio', 'voice', 'video_note'):
            media = getattr(message, kind, None)
            if media:
                if kind == 'photo':
                    media = media[-1]
                if pending.get('preview'):
                    if kind != 'photo':
                        raise ApplicationHandlerStop
                    self.store.set_preview(self.active['id'], pending['source'], media.file_id)
                else:
                    self.store.add_file(self.active['id'], pending['source'], message, media.file_id,
                                        kind, caption=pending.get('caption'),
                                        source_media_key=pending.get('media_key', ''),
                                        has_thumbnail=bool(getattr(media, 'thumbnail', None)) or kind == 'photo',
                                        replace=pending.get('replace_original', False))
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
            return
        next_groups = job.get('handle_next_groups')
        if next_groups:
            try:
                await next_groups(event.message, next_groups_only=True)
            except Exception as exc:
                job['button_error'] = exc

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
                detail = '' if complete else '发送已静默，无法确认全部文件已收到。'
                if job.get('preview_warnings'):
                    detail += ' 部分 HEIC 预览未生成，原文件已保留。'
                if job.get('resume_lookup_warnings'):
                    detail += ' 部分旧媒体标识无法恢复，可能有重新上传的文件。'
                if job.get('callback_timeout') and not complete:
                    detail += ' 源机器人未确认某次翻组点击，可稍后续传重试。'
                self.store.update(job_id, 'complete' if complete else 'needs_review', job['expected'], detail)
            except asyncio.CancelledError:
                self.store.update(job_id, 'interrupted', job['expected'])
                raise
            except Exception as exc:
                # Do not log links, session secrets, or raw Telegram response contents.
                explanations = {
                    'InsufficientDiskError': '磁盘剩余空间不足，已停止并清理临时文件。',
                    'MediaTooLargeError': '单文件超过配置上限，已停止并清理临时文件。',
                    'IncompleteMediaError': '媒体下载不完整，已清理临时文件。',
                    'ChatForwardsRestrictedError': '源内容禁止转发；如需下载重传，请启用 download_upload 模式。',
                }
                detail = type(exc).__name__
                if detail in explanations:
                    detail += '：' + explanations[detail]
                self.store.update(job_id, 'partial' if self.store.files(job_id) else 'failed',
                                  job['expected'], detail)
            finally:
                self.pending = None
                self.active = None
                self.jobs.task_done()
            with contextlib.suppress(Exception):
                status = self.store.get(job_id)
                await self.app.bot.send_message(row['owner'],
                    f"任务 {job_id}：{status['state']}\n{status['detail']}\n/folder {job_id}")

    def progress(self, job, label):
        started = time.monotonic()
        last_update = -float('inf')

        def callback(current, total):
            nonlocal last_update
            now = time.monotonic()
            if now - last_update < 3 and (not total or current < total):
                return
            last_update = now
            elapsed = max(now - started, 0.01)
            rate = current / elapsed
            total_text = f'{total / 1024**2:.1f} MiB' if total else '未知'
            percent = f'{current / total:.1%}' if total else ''
            eta = f'{max(0, total-current) / rate / 60:.1f} 分钟' if total and rate > 0 else '估算中'
            detail = (f'{label}：{current / 1024**2:.1f} / {total_text} {percent}\n'
                      f'平均速度 {rate / 1024**2:.2f} MiB/s；此阶段剩余约 {eta}')
            self.store.update(job['id'], 'running', job['expected'], detail)
        return callback

    async def transfer_media(self, message, key, job, preview_only=False, replace_original=False):
        if self.transfer_mode == 'download_upload':
            self.store.update(job['id'], 'running', job['expected'], '正在下载一个文件到临时目录。')
            async with self.spool.stage(self.client, message, progress=self.progress(job, '下载')) as path:
                preview = None
                thumbnail = None
                if self.heic_previews:
                    try:
                        preview = make_jpeg_preview(path, self.spool)
                    except Exception as exc:
                        if type(exc).__name__ == 'InsufficientDiskError':
                            raise
                        job['preview_warnings'] = True
                if self.document_groups:
                    try:
                        thumbnail = await document_thumbnail(path, self.spool, preview)
                    except Exception as exc:
                        if type(exc).__name__ == 'InsufficientDiskError':
                            raise
                        job['preview_warnings'] = True
                marker = f"bridge:{job['id']}:{uuid.uuid4().hex}"
                self.store.update(job['id'], 'running', job['expected'], '正在上传文件，确认收件后立即删除临时文件。')
                # Keep filename/ordinary video/audio metadata. Round videos and
                # stickers are sent as ordinary attachments so a receipt caption
                # is available. The marker is not saved as the user's caption.
                attrs = [a for a in getattr(message.document, 'attributes', [])
                         if isinstance(a, (types.DocumentAttributeFilename,
                                           types.DocumentAttributeVideo,
                                           types.DocumentAttributeImageSize,
                                           types.DocumentAttributeAudio))
                         and not getattr(a, 'round_message', False)]
                if not any(isinstance(a, types.DocumentAttributeFilename) for a in attrs):
                    attrs.append(types.DocumentAttributeFilename(f'photo_{message.id}{path.suffix}'))
                if not preview_only:
                    await self.deliver(message, key, marker=marker, upload_path=path, attrs=attrs,
                                       thumbnail=thumbnail, replace_original=replace_original)
                if preview and not self.document_groups:
                    await self.deliver(message, key, marker=f'preview:{uuid.uuid4().hex}',
                                       upload_path=preview, preview=True)
        else:
            await self.deliver(message, key)

    async def deliver(self, message, key, marker=None, upload_path=None, attrs=None, preview=False, thumbnail=None, replace_original=False):
        future = asyncio.get_running_loop().create_future()
        self.pending = dict(source=key, future=future, media_key=media_key(message), replace_original=replace_original)
        if marker:
            self.pending.update(marker=marker, caption=message.raw_text or '', preview=preview)
        try:
            if upload_path is None:
                await self.client.forward_messages(self.destination, message)
            else:
                uploaded = await upload_file(
                    self.client, upload_path,
                    progress=self.progress(self.active, '上传预览' if preview else '上传'),
                    workers=self.upload_workers)
                self.store.update(self.active['id'], 'running', self.active['expected'], '上传完成，等待机器人确认收件。')
                await self.client.send_file(
                    self.destination, uploaded, caption=marker, parse_mode=None,
                    force_document=not preview and (self.document_groups or not bool(message.photo)), attributes=attrs or None,
                    thumb=str(thumbnail) if thumbnail else None,
                    supports_streaming=any(isinstance(a, types.DocumentAttributeVideo)
                                           for a in attrs or []))
            await asyncio.wait_for(future, 60)
        except BaseException:
            # Do not attribute a late send/forward receipt to another task.
            self.blocked = True
            raise
        finally:
            self.pending = None

    async def collect(self, link, job):
        visited = set()
        clicked = set()
        seen = set()
        actions = 0
        remaining = {(x['source_chat'], x['source_message']): x for x in self.store.files(job['id'])}
        # Older versions did not store a stable media ID. Recover it from the
        # old source messages; never identify duplicates by filename alone.
        for source, row in remaining.items():
            if not row.get('media_key'):
                try:
                    old = await self.client.get_messages(source[0], ids=source[1])
                    row['media_key'] = media_key(old)
                    if row['media_key']:
                        self.store.set_media_key(job['id'], source, row['media_key'])
                    else:
                        job['resume_lookup_warnings'] = True
                except Exception:
                    job['resume_lookup_warnings'] = True

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

        async def handle_buttons(message, next_groups_only=False):
            nonlocal actions
            key = (message.chat_id, message.id)
            text = message.raw_text or ''
            rows = message.buttons or []
            page = pagination_marker(text, [button.text or '' for row in rows for button in row])
            for row in rows:
                for button in row:
                    label = re.sub(r'\s+', '', button.text or '')
                    is_group = '下一组' in label
                    if next_groups_only and not is_group:
                        continue
                    if not any(re.sub(r'\s+', '', x) in label for x in self.labels if x):
                        continue
                    if is_group:
                        if any(x in text for x in ('正在发送', '发送中', '请稍候')):
                            continue
                        sent = re.search(r'已发送\s*[：:]\s*(\d+)\s*/\s*(\d+)', text)
                        if sent and int(sent[1]) >= int(sent[2]):
                            continue
                    url = getattr(button, 'url', None)
                    data = getattr(button, 'data', None)
                    signature = (*key, url, data, page if is_group else None)
                    if signature in clicked or self.active is not job:
                        continue
                    clicked.add(signature)
                    job['pending_group_clicks'] = job.get('pending_group_clicks', 0) + 1
                    try:
                        if url:
                            await follow(url)
                        elif data is not None:
                            actions += 1
                            if actions > 100:
                                raise RuntimeError('按钮跳转次数超限')
                            try:
                                await button.click()
                            except Exception as exc:
                                # Some bots send the next batch but never answer
                                # the callback itself. Keep collecting its files.
                                if type(exc).__name__ != 'BotResponseTimeoutError':
                                    raise
                                job['callback_timeout'] = True
                    finally:
                        job['pending_group_clicks'] -= 1
                    await asyncio.sleep(1)

        job['handle_next_groups'] = handle_buttons

        await follow(link)
        while True:
            if job.get('button_error'):
                raise job['button_error']
            if job['overflow']:
                raise RuntimeError('收件队列超限')
            try:
                message = await asyncio.wait_for(job['events'].get(), self.idle)
            except asyncio.TimeoutError:
                if job.get('pending_group_clicks'):
                    continue
                return
            if job['expected'] is None:
                job['expected'] = media_count(message.raw_text or '')
                self.store.update(job['id'], 'running', job['expected'])
            if not self.store.get(job['id']).get('title'):
                title = folder_title(message.raw_text or '')
                if title:
                    self.store.set_title(job['id'], title)
            key = (message.chat_id, message.id)
            if (message.photo or message.document) and key not in seen:
                seen.add(key)
                previous_key = key if key in remaining else next(
                    (source for source, row in remaining.items()
                     if media_key(message) and row.get('media_key') == media_key(message)), None)
                previous = remaining.pop(previous_key) if previous_key is not None else None
                if previous:
                    ext = getattr(getattr(message, 'file', None), 'ext', '') or ''
                    if self.transfer_mode == 'download_upload' and self.heic_previews and ext.lower() in {'.heic', '.heif'}:
                        if self.document_groups and not previous.get('has_thumbnail'):
                            await self.transfer_media(message, previous_key, job, replace_original=True)
                        elif not self.document_groups and not previous.get('preview_file_id'):
                            await self.transfer_media(message, previous_key, job, preview_only=True)
                else:
                    await self.transfer_media(message, key, job)
                await asyncio.sleep(self.transfer_pause)
            await handle_buttons(message)
