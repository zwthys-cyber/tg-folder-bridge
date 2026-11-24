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
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import ApplicationHandlerStop, CallbackQueryHandler, CommandHandler, MessageHandler, filters
from telethon import TelegramClient, events, types
from core import Store, folder_title, folder_metadata, source_entry, media_count, media_key, pagination_marker, parse_link
from media_spool import MediaSpool
from download_pipeline import DownloadPipeline
from previews import document_thumbnail, make_jpeg_preview
from fast_upload import upload_file
from catalog_ui import catalog_page, send_file_page, send_folder_card, favorite_button, resource_summary
from folder_cover import image_cover
import transfer_settings
from transport_recovery import Cooldown, error_detail, rate_wait
from adaptive_speed import AdaptiveSpeed
from upload_transport import UploadTransport
from bot_api_config import endpoint, LOCAL
from local_upload import eligible, send_local, DeliveryUnknownError, LocalBotUploadRejected
from menu_actions import menu_guard, retire_menu
from content_identity import analyze, preference
from source_lock import source_lease
from source_recovery import open_menu_content
from source_catalog import MenuChangedError
from source_probe import probe_original_tail
from reference_repair import repair_reference
from remote_identity import recover_identical_copy
from operations import Operations
from browse_navigation import BrowseNavigation
from delivery import WholeDelivery
from notifications import Notifications
import automation_control

VERSION = '0.13.0'


class Bridge(Operations, BrowseNavigation, WholeDelivery, Notifications):
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
        self.reference_repair = os.getenv('REFERENCE_REPAIR', '1') == '1'
        # Explicit operator request, consumed once per task in this process.
        # Refreshing a source manifest still skips all confirmed media IDs.
        self.source_refresh_requests = {value.strip() for value in
            os.getenv('SOURCE_REFRESH_ONCE', '').split(',') if value.strip()}
        self.document_groups = os.getenv('DISPLAY_MODE', 'document_group') == 'document_group'
        self.upload_workers = int(os.getenv('UPLOAD_WORKERS', '8'))
        self.download_workers = int(os.getenv('DOWNLOAD_WORKERS', '8'))
        if self.transfer_pause < 0:
            raise ValueError('Transfer pause must be nonnegative')
        if not self.owners or self.idle <= 0 or self.timeout <= 0:
            raise ValueError('OWNER_IDS 和超时配置无效')
        root = Path(os.getenv('DATA_DIR', 'data'))
        root.mkdir(parents=True, exist_ok=True)
        self.source_lock = root / 'source-control.lock'
        self.settings_file = root / 'performance.json'
        self.download_workers, self.upload_workers = transfer_settings.load(
            self.settings_file, self.download_workers, self.upload_workers)
        self.automatic_speed = transfer_settings.automatic(self.settings_file)
        self.upload_connections = transfer_settings.connections(self.settings_file)
        self.upload_scheduler = transfer_settings.scheduler(self.settings_file)
        self.upload_transport = None
        self.bot_api_base = endpoint()
        self.upload_backend = os.getenv('UPLOAD_BACKEND', 'relay')
        if self.upload_backend not in {'relay', 'hybrid'}:
            raise ValueError('UPLOAD_BACKEND must be relay or hybrid')
        if self.upload_backend == 'hybrid' and self.bot_api_base != LOCAL:
            raise ValueError('Hybrid uploads require the local endpoint for polling and delivery')
        self.auto_resume = os.getenv('AUTO_RESUME_INTERRUPTED', '1') == '1'
        self.tuners = dict(下载=AdaptiveSpeed(self.download_workers),
                          上传=AdaptiveSpeed(self.upload_workers))
        self.cooldown = Cooldown(root / 'rate-limit.json')
        self.reconnect_required = False
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
                                     os.environ['API_HASH'], flood_sleep_threshold=0,
                                     raise_last_call_error=True)
        self.jobs = asyncio.Queue(maxsize=20)
        self.active = None
        self.pending = None
        self.blocked = False
        self.worker = None
        self.recovery_worker = None
        self.search_sessions = {}
        self.pending_jumps = {}
        self.retry_worker = None
        self.delivery_worker = None
        self.notice_worker = None

    def install(self, app):
        # Negative group runs before ordinary existing bot handlers. Reserve -100.
        app.add_handler(MessageHandler(filters.User(self.relay_id) & filters.ATTACHMENT,
                                       self.receive_file), group=-100)
        app.add_handler(CommandHandler('fetch', self.fetch), group=-100)
        app.add_handler(CommandHandler('start', self.start_command), group=-100)
        app.add_handler(CommandHandler('favorites', self.favorites_command), group=-100)
        for command in ('notifications','subscribe','unsubscribe'):
            app.add_handler(CommandHandler(command,self.notifications_command),group=-100)
        app.add_handler(CallbackQueryHandler(self.notice_callback,pattern=r'^bridge_notice:'),group=-100)
        app.add_handler(CommandHandler('help', self.help_command), group=-100)
        app.add_handler(CommandHandler('admin', self.admin_command), group=-100)
        app.add_handler(CommandHandler('failures', self.failures_command), group=-100)
        app.add_handler(CommandHandler('page', self.page_command), group=-100)
        app.add_handler(CommandHandler('cancel', self.cancel_jump), group=-100)
        app.add_handler(CommandHandler('folder', self.folder), group=-100)
        app.add_handler(CommandHandler('folders', self.folders), group=-100)
        app.add_handler(CommandHandler('status', self.status), group=-100)
        app.add_handler(CommandHandler('resume', self.resume), group=-100)
        app.add_handler(CommandHandler('speed', self.speed), group=-100)
        app.add_handler(CommandHandler('transport', self.transport_command), group=-100)
        app.add_handler(CommandHandler('diagnostics', self.diagnostics), group=-100)
        app.add_handler(CommandHandler('resolve', self.resolve_delivery), group=-100)
        app.add_handler(CommandHandler('dedup', self.dedup), group=-100)
        app.add_handler(CallbackQueryHandler(self.original, pattern=r'^bridge_original:'), group=-100)
        app.add_handler(CallbackQueryHandler(self.admin_callback, pattern=r'^bridge_admin:'), group=-100)
        app.add_handler(CallbackQueryHandler(self.jump_callback, pattern=r'^bridge_jump:'), group=-100)
        app.add_handler(CallbackQueryHandler(self.delivery_callback, pattern=r'^bridge_delivery:'), group=-100)
        app.add_handler(CallbackQueryHandler(self.catalog_callback, pattern=r'^bridge_(catalog|open|view|page|search|fav|unfav|favorites):'), group=-100)
        app.add_handler(MessageHandler(filters.Regex(r'^https://t\.me/\w+\?start='),
                                       self.fetch), group=-100)
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.search_command), group=-100)

    async def start(self, app):
        self.app = app
        if self.cooldown.remaining():
            self.reconnect_required = True
            print('用户连接正在限流冷却，冷却后再恢复。', flush=True)
        else:
            try:
                await self.connect_user()
            except Exception as exc:
                if not self.defer_transport(exc, 0):
                    raise
                await self.client.disconnect()
                self.reconnect_required = True
                print('用户连接暂不可用，已进入冷却；管理命令仍可使用。', flush=True)
        self.destination = (await app.bot.get_me()).username
        print('文件转存模式：' + self.transfer_mode, flush=True)
        print(f'Bridge {VERSION}；下载并发上限：{self.download_workers}；上传并发上限：{self.upload_workers}；'
              f'上传连接：{self.upload_connections}；调度：{self.upload_scheduler}', flush=True)
        if os.getenv('EDGE_MODE') != '1':
            await self.update_menu(app.bot)
        self.client.add_event_handler(self.source_message, events.NewMessage(incoming=True))
        self.client.add_event_handler(self.source_message, events.MessageEdited(incoming=True))
        self.worker = asyncio.create_task(self.work())
        self.resume_after_upgrade()
        self.recovery_worker = asyncio.create_task(self.restore_jobs())
        self.retry_worker = asyncio.create_task(self.retry_loop())
        if os.getenv('EDGE_MODE') != '1':
            self.delivery_worker = asyncio.create_task(self.delivery_loop())
            self.notice_worker = asyncio.create_task(self.notice_loop())

    async def restore_jobs(self):
        if not self.auto_resume or self.transfer_mode != 'download_upload':
            return
        for job_id in self.store.recoverable_jobs:
            row = self.store.get(job_id)
            if row and row['owner'] in self.owners and row['state'] == 'interrupted':
                if self.store.unknown_local_send(job_id):
                    self.store.update(job_id, 'needs_review', row['expected'],
                        '本地上传结果未知，请先检查聊天；确认未收到后使用 /resolve 任务编号 retry。')
                    continue
                self.store.update(job_id, 'queued', row['expected'], '服务恢复后自动续传。')
                await self.jobs.put(job_id)
                print(f'自动恢复任务：{job_id}', flush=True)

    def resume_after_upgrade(self):
        # Deployment can request one-time continuation without a second poller
        # or logging into the relay session from another running process.
        import json
        request_file = self.settings_file.parent / 'resume-once.json'
        if request_file.exists():
            try:
                requests = json.loads(request_file.read_text())
                if (not isinstance(requests, list) or len(requests) > 20
                        or any(not isinstance(job, str) for job in requests)):
                    raise ValueError
            except (ValueError, OSError):
                print('升级续传请求无效，请检查 resume-once.json；服务继续运行。', flush=True)
                return
            if self.transfer_mode != 'download_upload':
                print('仅下载上传模式支持升级后自动续传。', flush=True)
                return
            for job_id in requests:
                row = self.store.get(job_id)
                if (row and row['owner'] in self.owners and row['state'] == 'interrupted'
                        and not self.jobs.full()):
                    if self.store.unknown_local_send(job_id):
                        continue
                    self.store.update(job_id, 'queued', row['expected'], '升级后继续任务。')
                    self.jobs.put_nowait(job_id)
            request_file.unlink()

    async def connect_user(self):
        await self.client.connect()
        if not await self.client.is_user_authorized():
            await self.client.disconnect()
            raise RuntimeError('请先在部署服务器执行 python login.py')
        me = await self.client.get_me()
        if me.bot or me.id != self.relay_id:
            await self.client.disconnect()
            raise RuntimeError('登录账号与 RELAY_USER_ID 不一致')
        print('中转账号 Premium：' + ('已开通' if getattr(me, 'premium', False) else '未开通'), flush=True)
        self.reconnect_required = False

    async def update_menu(self, bot):
        public = [BotCommand('start', '资源统计与文件夹列表'),
                  BotCommand('page', '跳到指定目录页'),
                  BotCommand('favorites', '我的收藏'),
                  BotCommand('notifications','更新通知'),
                  BotCommand('help', '使用帮助')]
        await bot.set_my_commands(public)
        await bot.set_my_commands(public, scope=BotCommandScopeAllPrivateChats())
        for owner in self.owners:
            try:
                await bot.set_my_commands(public + [BotCommand('fetch', '导入分享文件夹'),
                    BotCommand('admin', '管理员控制面板'), BotCommand('failures', '失败任务与续传'),
                    BotCommand('folders', '查看导入任务'), BotCommand('status', '查看传输进度'),
                    BotCommand('resume', '继续导入任务'), BotCommand('add', '收录搜索资源'),
                    BotCommand('speed', '调整上传下载并发'),
                    BotCommand('transport', '设置上传连接数'),
                    BotCommand('diagnostics', '查看实测传输记录'),
                    BotCommand('resolve', '确认未知上传后允许重试'),
                    BotCommand('dedup', '核验内容并合并重复索引'),
                    BotCommand('delete', '删除搜索资源')], scope=BotCommandScopeChat(owner))
            except BadRequest:
                pass  # This owner may not have opened the bot yet.

    def can_view(self, job, uid):
        return bool(job and ((uid in self.owners and job['owner'] == uid)
                            or job['state'] == 'complete' or job.get('audit_public') or job.get('partial_notice')))

    async def start_command(self, update, context):
        if update.effective_chat.type == 'private':
            rows=self.store.catalog()
            stats=self.store.resource_stats()
            summary=await context.bot.send_message(update.effective_chat.id, resource_summary(stats))
            text,markup=catalog_page(rows)
            listing=await context.bot.send_message(update.effective_chat.id,text,reply_markup=markup)
            self.store.register_catalog_panel(update.effective_chat.id,listing.message_id,summary.message_id)
        raise ApplicationHandlerStop

    async def help_command(self, update, context):
        if update.effective_chat.type != 'private':
            raise ApplicationHandlerStop
        text = '发送 /start 查看文件夹列表，点击编号打开详情。\n点击“查看内容”会连续发送整个文件夹，不需点击下一组。\n/page 页码 跳转目录；也可点击列表中的跳转按钮。\n发送 /favorites 查看我的收藏。\n直接发送关键词搜索文件夹名称，也可搜索原收录资源。'
        text+='\n/notifications 更新通知，进入后点击开启或关闭。默认关闭。'
        if update.effective_user.id in self.owners:
            text += '\n\n管理员：\n/admin 控制面板\n/failures 失败任务\n/fetch 分享链接\n/folders 导入任务\n/status 任务编号\n/resume 任务编号\n/speed 下载并发 上传并发\n/add 标题 | 链接 | 关键词\n/delete 编号'
        await update.message.reply_text(text)
        raise ApplicationHandlerStop

    def favorite_rows(self, uid):
        return [r for r in self.store.favorite_catalog(uid) if self.can_view(r,uid)]

    async def favorites_command(self, update, context):
        if update.effective_chat.type == 'private':
            text,markup=catalog_page(self.favorite_rows(update.effective_user.id),
                page_callback_prefix='bridge_favorites',favorites=True)
            await context.bot.send_message(update.effective_chat.id,text,reply_markup=markup)
        raise ApplicationHandlerStop

    async def search_text(self, chat, uid, query):
        rows = self.store.search_catalog(query, uid, owned=uid in self.owners)
        if not rows:
            return False
        if len(self.search_sessions) >= 1000:
            self.search_sessions.pop(next(iter(self.search_sessions)))
        token = uuid.uuid4().hex[:16]
        self.search_sessions[token] = (uid, query)
        text, markup = catalog_page(rows, page_callback_prefix=f'bridge_search:{token}')
        await self.app.bot.send_message(chat, f'搜索：{query}\n\n{text}', reply_markup=markup)
        return True

    async def search_command(self, update, context):
        text = (update.message.text or '').strip()
        if await self.handle_jump_reply(update, context, text):
            raise ApplicationHandlerStop
        if update.effective_chat.type == 'private' and 0 < len(text) <= 100:
            if await self.search_text(update.effective_chat.id, update.effective_user.id, text):
                raise ApplicationHandlerStop

    @menu_guard
    async def catalog_callback(self, update, context):
        query = update.callback_query
        if update.effective_chat.type != 'private':
            raise ApplicationHandlerStop
        try:
            parts = query.data.split(':')
            if parts[0]=='bridge_view' and len(parts)==3 and parts[2]=='1':
                await query.answer()
                accepted=await self.queue_whole_folder(context.bot,update.effective_user.id,
                    self.store.get(parts[1]),query.message.message_id)
                if accepted:await retire_menu(self,context.bot,update.effective_chat.id,query.message)
                raise ApplicationHandlerStop
            if parts[0] in {'bridge_catalog','bridge_favorites'}:
                page = int(parts[1])
                await query.answer()
                favorites = parts[0] == 'bridge_favorites'
                rows = self.favorite_rows(update.effective_user.id) if favorites else self.store.catalog()
                text, markup = catalog_page(rows, page, parts[0], favorites=favorites)
                old_text=getattr(query.message,'text',None) or ''
                if getattr(query.message, 'photo', None) or not old_text.startswith(('📁 文件夹列表','⭐ 我的收藏','搜索：')):
                    listing=await context.bot.send_message(update.effective_chat.id, text, reply_markup=markup)
                    if not favorites:
                        self.store.register_catalog_panel(update.effective_chat.id,listing.message_id)
                    await retire_menu(self,context.bot,update.effective_chat.id,query.message)
                else:
                    await query.edit_message_text(text, reply_markup=markup)
            elif parts[0] == 'bridge_search':
                session = self.search_sessions.get(parts[1])
                if not session or session[0] != update.effective_user.id:
                    await query.answer('搜索已过期，请重新输入关键词', show_alert=True)
                    raise ApplicationHandlerStop
                rows = self.store.search_catalog(session[1], session[0], owned=session[0] in self.owners)
                text, markup = catalog_page(rows, int(parts[2]), f'bridge_search:{parts[1]}')
                await query.answer()
                await query.edit_message_text(f'搜索：{session[1]}\n\n{text}', reply_markup=markup)
            elif parts[0] in {'bridge_fav','bridge_unfav'}:
                job=self.store.get(parts[1])
                if not self.can_view(job,update.effective_user.id):
                    await query.answer('文件夹尚未就绪或已移除',show_alert=True)
                    raise ApplicationHandlerStop
                saved=parts[0]=='bridge_fav'
                self.store.favorite(update.effective_user.id,job['id'],saved)
                await query.answer('已收藏，可在“我的收藏”中查看。' if saved else '已取消收藏。')
                markup=getattr(query.message,'reply_markup',None)
                if markup:
                    rows=[[favorite_button(job,saved) if b.callback_data in
                           {f"bridge_fav:{job['id']}",f"bridge_unfav:{job['id']}"} else b for b in row]
                          for row in markup.inline_keyboard]
                    updated=InlineKeyboardMarkup(rows)
                    if updated != markup:
                        await query.edit_message_reply_markup(reply_markup=updated)
            else:
                progress=None
                if parts[0]=='bridge_page':
                    progress=self.store.view(parts[1],update.effective_user.id)
                    if progress is None:
                        await query.answer('此浏览会话已过期，请重新打开文件夹。',show_alert=True)
                        raise ApplicationHandlerStop
                job = self.store.get(progress.job if progress else parts[1])
                if not self.can_view(job, update.effective_user.id):
                    await query.answer('文件夹尚未就绪或已移除', show_alert=True)
                    raise ApplicationHandlerStop
                page = max(1, int(parts[2]))
                await query.answer()
                items = self.store.files(job['id'])
                if parts[0] == 'bridge_open' and page == 1:
                    await self.show_folder_card(context.bot, update.effective_chat.id, job, items)
                    deleted=await self.dismiss_catalog(context.bot,update.effective_chat.id,query.message)
                    await retire_menu(self,context.bot,update.effective_chat.id,query.message,clear=not deleted)
                else:
                    if progress is None:
                        old_text=getattr(query.message,'text',None) or ''
                        legacy_navigation=(parts[0]=='bridge_view' and
                            (page>1 or ('共 ' in old_text and ' 组）' in old_text)))
                        progress=self.store.start_view(update.effective_user.id,job['id'],details_sent=legacy_navigation)
                    await send_file_page(context.bot, update.effective_chat.id, job,
                                         items, page, self.send_stored,progress=progress,
                                         favorite=self.store.is_favorite(update.effective_user.id,job['id']))
                    await retire_menu(self,context.bot,update.effective_chat.id,query.message)
        except (ValueError, IndexError):
            await query.answer('列表已更新，请重新发送 /start', show_alert=True)
        raise ApplicationHandlerStop

    async def dismiss_catalog(self, bot, chat, message):
        text=getattr(message,'text',None) or ''
        message_id=getattr(message,'message_id',None)
        if not text.startswith('📁 文件夹列表') or not isinstance(message_id,int):
            return
        stats_id=self.store.catalog_panel_stats(chat,message_id)
        ids=[stats_id,message_id] if stats_id is not None else [message_id]
        removed=True
        for mid in ids:
            try:
                await bot.delete_message(chat_id=chat,message_id=mid)
            except TelegramError as exc:
                removed=False
                print('菜单消息清理暂不可用：'+type(exc).__name__,flush=True)
        if removed:
            self.store.forget_catalog_panel(chat,message_id)
        return removed

    async def show_folder_card(self, bot, chat, job, items):
        photo = job.get('cover_file_id')
        if not photo:
            try:
                photo = await image_cover(bot, items)
            except Exception as exc:
                print('文件夹封面暂不可用：' + type(exc).__name__, flush=True)
        reply = await send_folder_card(bot, chat, job, items, photo,
                                       favorite=self.store.is_favorite(chat,job['id']))
        if photo and getattr(reply, 'photo', None):
            self.store.set_cover(job['id'], reply.photo[-1].file_id)

    async def stop(self, app):
        if self.notice_worker:
            self.notice_worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.notice_worker
        if self.delivery_worker:
            self.delivery_worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.delivery_worker
        if self.retry_worker:
            self.retry_worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.retry_worker
        if self.recovery_worker:
            self.recovery_worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.recovery_worker
        if self.worker:
            self.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.worker
        if self.active:
            self.store.update(self.active['id'], 'interrupted', self.active['expected'])
        await self.close_upload_transport()
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
        existing = self.store.find_link(update.effective_user.id, link, self.allowed)
        if existing:
            await update.message.reply_text(
                f"该分享已有任务：{existing['state']}\n/folder {existing['id']}\n"
                f"未完成时续传：/resume {existing['id']}")
            raise ApplicationHandlerStop
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
            if page == 1:
                await self.show_folder_card(context.bot, update.effective_chat.id, job, files)
            else:
                progress=self.store.start_view(update.effective_user.id,job['id'])
                await send_file_page(context.bot, update.effective_chat.id, job, files, page, self.send_stored,
                                     progress=progress,favorite=self.store.is_favorite(update.effective_user.id,job['id']))
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
        processed = self.store.processed_count(job['id'])
        counts = (f"已处理 {processed} / {job['expected'] if job['expected'] is not None else '未知'} 个来源条目\n"
                  f"唯一资源 {n}，合并重复 {processed-n} 个\n") if processed != n else (
                  f"已保存 {n} / {job['expected'] if job['expected'] is not None else '未知'} 个原文件\n")
        await update.message.reply_text(
            f"任务：{job['id']}\n状态：{job['state']}\n"
            f"{counts}"
            f"{job['detail']}")
        raise ApplicationHandlerStop

    async def speed(self, update, context):
        if update.effective_chat.type != 'private' or update.effective_user.id not in self.owners:
            raise ApplicationHandlerStop
        if context.args:
            try:
                automatic = context.args[0].lower() == 'auto'
                args = context.args[1:] if automatic else context.args
                if automatic and not args:
                    args = ['32', '32']
                if len(args) != 2:
                    raise ValueError
                download, upload = transfer_settings.validate(*map(int, args))
                transfer_settings.save(self.settings_file, download, upload, automatic)
                self.download_workers, self.upload_workers = download, upload
                self.automatic_speed = automatic
                self.tuners['下载'].configure(download)
                self.tuners['上传'].configure(upload)
            except ValueError:
                await update.message.reply_text('自动调速：/speed auto 32 32\n固定并发：/speed 8 8\n下载、上传上限均为 1–32。')
                raise ApplicationHandlerStop
        await update.message.reply_text(
            ('自动调速：' if self.automatic_speed else '固定并发：') +
            f'下载上限 {self.download_workers} 路；上传上限 {self.upload_workers} 路。\n' +
            ('设置已保存。' if context.args else '调整：/speed 下载并发 上传并发（1–32）。') +
            ('从当前安全并发分别采样，吞吐提升才保留更高并发，收益不足自动回退。\n' if self.automatic_speed else '') +
            ('自动探测最高 16 路；设置更高上限也不会自动试到 20–32 路。\n' if self.automatic_speed else '') +
            '进行中的并发传输会在后续分块采用新上限，已发出的请求正常完成。\n'
            '小文件和兼容回退仍使用单路；并发越高不保证越快。')
        if self.cooldown.remaining():
            await update.message.reply_text(f'当前连接仍需冷却约 {self.cooldown.remaining():.0f} 秒，修改并发不会提前解除冷却。')
        raise ApplicationHandlerStop

    async def transport_command(self, update, context):
        if update.effective_chat.type != 'private' or update.effective_user.id not in self.owners:
            raise ApplicationHandlerStop
        if context.args:
            try:
                if not 1 <= len(context.args) <= 2:
                    raise ValueError
                if context.args == ['auto']:
                    count, scheduler = 2, 'auto'
                else:
                    count = int(context.args[0])
                    scheduler = context.args[1] if len(context.args) == 2 else self.upload_scheduler
                    if scheduler == 'auto':
                        scheduler = 'balanced'
                if not 1 <= count <= 4:
                    raise ValueError
                transfer_settings.save(self.settings_file, self.download_workers,
                    self.upload_workers, self.automatic_speed, count, scheduler)
                self.upload_connections = count
                self.upload_scheduler = scheduler
            except ValueError:
                await update.message.reply_text('用法：/transport auto 或 /transport 2 [balanced 或 round_robin]。支持 1–4。')
                raise ApplicationHandlerStop
        await update.message.reply_text(
            f'大文件上传连接：{self.upload_connections}；调度：{self.upload_scheduler}。\n'
            'auto 通过同一文件比较原通道和两条媒体连接，设置从下一个文件生效。\n'
            '1 为原通道，2–4 为独立媒体连接。\n'
            '分块总并发仍由 /speed 控制；/diagnostics 查看实测记录。')
        raise ApplicationHandlerStop

    async def diagnostics(self, update, context):
        if update.effective_chat.type != 'private' or update.effective_user.id not in self.owners:
            raise ApplicationHandlerStop
        lines = [f'Bridge {VERSION}；上传连接：{self.upload_connections}；调度：{self.upload_scheduler}。\n'
                 f'重启自动续传：{"开启" if self.auto_resume else "关闭"}（仅下载上传模式）。\n'
                 f'上传路由：{self.upload_backend}（hybrid：2000 MB 以内本地 API，超出走中转）。']
        for row in self.store.transfer_history(8):
            m = row['payload']
            cooldown = (f"\n限流响应 {m['rate_waits']} 次，合计等待 {m.get('cooldown_seconds', 0):.1f} 秒"
                        if m.get('rate_waits') else '')
            routes = (f"\n原通道 {m.get('control_mib', 0):.2f} MiB/s，媒体连接 "
                      f"{m.get('dedicated_mib', 0):.2f} MiB/s，选择 {m['route_choice']}"
                      if m.get('route_choice') else '')
            verified = (f"\n已核对全部 {m['hash_ranges']} 个哈希范围，确认同一原件 {m['verified_bytes']/1024**2:.1f} MiB"
                        if m.get('verified_bytes') else '')
            lines.append(f"{row['at']} UTC · {row['phase']} · {row['job']}\n"
                f"{m.get('bytes', 0)/1024**2:.1f} MiB / {m.get('seconds', 0):.1f} 秒 · "
                f"{m.get('average_mib', 0):.2f} MiB/s\n"
                f"{m.get('transport', '?')} · {m.get('connections', 1)} 连接 · "
                f"p95 {m.get('p95_ms', 0):.0f} ms · 分块续传 {m.get('retries', 0)} 次 · {m.get('error') or '阶段完成'}{cooldown}{routes}{verified}")
        if len(lines) == 1:
            lines.append('还没有新版本的传输记录，当前文件完成后可查看。')
        await update.message.reply_text('\n\n'.join(lines))
        raise ApplicationHandlerStop

    async def close_upload_transport(self):
        if self.upload_transport:
            await self.upload_transport.close()
            self.upload_transport = None

    async def get_upload_transport(self):
        scheduler = 'balanced' if self.upload_scheduler == 'auto' else self.upload_scheduler
        if self.upload_transport and (self.upload_transport.count != self.upload_connections
                or self.upload_transport.scheduler != scheduler):
            await self.close_upload_transport()
        if self.upload_connections == 1:
            return None
        if self.upload_transport is None:
            self.upload_transport = UploadTransport(self.client, self.upload_connections, scheduler)
        return self.upload_transport

    async def resume(self, update, context):
        if update.effective_chat.type != 'private' or update.effective_user.id not in self.owners:
            raise ApplicationHandlerStop
        job = self.store.get(context.args[0]) if context.args else None
        if not job or job['owner'] != update.effective_user.id:
            await update.message.reply_text('用法：/resume 原任务编号')
        elif self.store.unknown_local_send(job['id']):
            await update.message.reply_text('上次本地上传结果未知。先检查聊天，确认未收到后发送：\n/resolve '+job['id']+' retry')
        elif self.completed_duplicate(job):
            completed=self.completed_duplicate(job)
            await update.message.reply_text(f"该分享已有完成任务，无需重复续传。\n/folder {completed['id']}")
        elif self.blocked or self.jobs.full():
            await update.message.reply_text('服务暂不能接收续传任务；收件未确认时请先重启。')
        elif job['state'] in {'queued', 'running', 'cooldown'}:
            await update.message.reply_text('此任务正在排队、运行或冷却，无需重复续传，请用 /status 查看。')
        else:
            self.store.update(job['id'], 'queued', job['expected'])
            self.jobs.put_nowait(job['id'])
            await update.message.reply_text(
                f"已排队续传：{job['id']}\n识别并跳过已保存原文件，HEIC 可补充预览。\n/status {job['id']}")
        raise ApplicationHandlerStop

    async def resolve_delivery(self, update, context):
        if update.effective_chat.type != 'private' or update.effective_user.id not in self.owners:
            raise ApplicationHandlerStop
        job = self.store.get(context.args[0]) if context.args else None
        if (len(context.args) != 2 or context.args[1] not in ('retry','received') or not job
                or job['owner'] != update.effective_user.id):
            await update.message.reply_text('确认聊天里未收到该文件后，使用 /resolve 任务编号 retry。')
        elif self.active or self.pending:
            await update.message.reply_text('请等待当前任务结束后再确认未知上传。')
        elif context.args[1] == 'received':
            reply = update.message.reply_to_message
            kind = 'video' if reply and reply.video else 'document'
            media = getattr(reply, kind, None)
            rows = self.store.db.execute("""
                SELECT i.id,i.source_chat,i.source_message,s.name,s.size
                FROM local_send_intents i JOIN source_entries s ON s.job=i.job
                 AND s.source_chat=i.source_chat AND s.source_message=i.source_message
                WHERE i.job=? AND i.state='pending' AND i.marker LIKE 'bridge:%'
            """, (job['id'],)).fetchall()
            if (len(rows) != 1 or not media or reply.chat_id != job['owner']
                    or not reply.from_user or reply.from_user.id != context.bot.id
                    or media.file_name != rows[0]['name'] or media.file_size != rows[0]['size']
                    or self.store.db.execute("SELECT 1 FROM files WHERE (chat=? AND message=?) OR "
                        "(job=? AND source_chat=? AND source_message=?)",
                        (reply.chat_id, reply.message_id, job['id'], rows[0]['source_chat'],
                         rows[0]['source_message'])).fetchone()):
                await update.message.reply_text('请回复本机器人发送的、与待确认原件匹配的文件。')
            else:
                intent = rows[0]
                self.store.confirm_local_send(intent['id'], lambda: self.store.add_file(
                    job['id'], (intent['source_chat'], intent['source_message']), reply,
                    media.file_id, kind, has_thumbnail=bool(media.thumbnail)))
                self.blocked = False
                await update.message.reply_text(self.queue_retry(job['owner'], job['id']))
        else:
            self.store.authorize_local_retry(job['id'])
            self.blocked = False
            await update.message.reply_text('已记录你确认未收到。发送 /resume '+job['id']+' 继续。')
        raise ApplicationHandlerStop

    async def dedup(self, update, context):
        if update.effective_chat.type != 'private' or update.effective_user.id not in self.owners:
            raise ApplicationHandlerStop
        row = self.store.get(context.args[0]) if context.args else None
        if not row or row['owner'] != update.effective_user.id:
            await update.message.reply_text('用法：/dedup 任务编号。只合并已核验的重复索引，旧聊天媒体不删除。')
        elif row['state'] in {'queued', 'running', 'cooldown'} or self.jobs.full() or self.blocked:
            await update.message.reply_text('请等待当前任务结束或未知发送确认后再核验。')
        else:
            self.store.request_audit(row['id'])
            self.store.update(row['id'], 'queued', row['expected'], '内容核验已排队；旧媒体不删除。')
            self.jobs.put_nowait(row['id'])
            await update.message.reply_text('内容核验已排队。确认相同内容后优先保留原始信息更完整的一份；未确认的版本保留。')
        raise ApplicationHandlerStop

    @menu_guard
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
        await retire_menu(self,context.bot,update.effective_chat.id,query.message)
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
                if (pending.get('expected_size') is not None
                        and (kind == 'photo' or getattr(media, 'file_size', None) != pending['expected_size'])):
                    pending['future'].set_exception(DeliveryUnknownError('补收回执的文件大小不匹配。'))
                    raise ApplicationHandlerStop
                if pending.get('preview'):
                    if kind != 'photo':
                        raise ApplicationHandlerStop
                    self.store.set_preview(self.active['id'], pending['source'], media.file_id)
                else:
                    def save():
                        self.store.add_file(self.active['id'], pending['source'], message, media.file_id,
                                            kind, caption=pending.get('caption'),
                                            source_media_key=pending.get('media_key', ''),
                                            has_thumbnail=bool(getattr(media, 'thumbnail', None)) or kind == 'photo',
                                            replace=pending.get('replace_original', False))
                    if pending.get('intent') is not None:
                        self.store.confirm_local_send(pending['intent'], save)
                    else:
                        save()
                pending['future'].set_result(True)
                break
        raise ApplicationHandlerStop

    async def source_message(self, event):
        job = self.active
        if not job or job.get('source_owned') is False or event.chat_id not in job['chats']:
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

    def defer_transport(self, error, attempt):
        wait = rate_wait(error, attempt)
        connection = isinstance(error, ConnectionError)
        if wait is None and not connection:
            return False
        if wait is not None:
            if type(error).__name__ == 'LocalBotRateLimitError':
                self.cooldown.defer(wait, error_detail(error))
                return True
            phase = (self.active or {}).get('phase', '')
            # A source-list FloodWait is unrelated to media throughput. Back off
            # the active window, never overwrite the user's persistent limits.
            if phase in self.tuners:
                tuner = self.tuners[phase]
                tuner.current = max(1, min(8, tuner.current // 2))
            self.cooldown.defer(wait, error_detail(error))
        elif not self.cooldown.remaining():
            self.cooldown.defer(5 * 2**attempt, error_detail(error))
        return True

    async def await_cooldown(self, job):
        while self.cooldown.remaining() > 0:
            seconds = self.cooldown.remaining()
            self.store.update(job['id'], 'cooldown', job['expected'],
                f'{self.cooldown.reason}\n自动冷却，还需约 {seconds:.0f} 秒。\n'
                f'下载并发 {self.download_workers}；上传并发 {self.upload_workers}。冷却结束后自动继续。')
            await asyncio.sleep(min(10, seconds))

    async def collect_with_recovery(self, link, job):
        for attempt in range(3):
            try:
                await self.await_cooldown(job)
                if self.reconnect_required:
                    await self.connect_user()
                self.store.update(job['id'], 'running', job['expected'])
                job['phase'] = '获取文件列表'
                return await self.collect(link, job)
            except Exception as exc:
                button_error = job.get('button_error')
                if button_error and rate_wait(button_error, attempt) is not None:
                    exc = button_error
                    job['phase'] = '源机器人翻组'
                if job.get('phase') == '下载' and rate_wait(exc, attempt) is not None:
                    raise
                if not self.defer_transport(exc, attempt):
                    raise exc
                detail = error_detail(exc, job.get('phase', ''))
                print(f"任务 {job['id']}：{detail}", flush=True)
                await self.close_upload_transport()
                await self.client.disconnect()
                self.reconnect_required = True
                if self.transfer_mode != 'download_upload' and self.blocked:
                    raise  # Unknown late forwards cannot be safely attributed.
                self.pending = None
                self.blocked = False  # Reuploaded messages use unique receipt tokens.
                if attempt == 2:
                    raise exc
                job_id, expected = job['id'], job['expected']
                refresh_source = (job.get('refresh_source') or
                                  (job.get('phase') == '下载' and type(exc).__name__ in
                                   {'FloodWaitError', 'FloodPremiumWaitError'}))
                job.clear()
                job.update(refresh_source=refresh_source, id=job_id, expected=expected, chats=set(), baselines={},
                           events=asyncio.Queue(maxsize=5000), overflow=False,
                           dedup_audit=bool(self.store.get(job_id).get('audit_requested')))

    async def work(self):
        while True:
            job_id = await self.jobs.get()
            while automation_control.paused(self.controls_root(),'transfers'):
                await asyncio.sleep(1)
            if self.blocked:
                self.store.update(job_id, 'blocked', detail='前一任务收件未确认，请重启后重新提交。')
                self.jobs.task_done()
                continue
            row = self.store.get(job_id)
            refresh_source = job_id in self.source_refresh_requests
            self.source_refresh_requests.discard(job_id)
            job = dict(id=job_id, expected=row['expected'], chats=set(), baselines={},
                       events=asyncio.Queue(maxsize=5000), overflow=False,
                       refresh_source=refresh_source,
                       repair_partial=bool(self.store.files(job_id)) and not row.get('audit_requested'),
                       dedup_audit=bool(row.get('audit_requested')))
            self.active = job
            self.store.update(job_id, 'running', job['expected'])
            try:
                job['source_owned'] = False
                async with source_lease(self.source_lock):
                    job['source_owned'] = True
                    try:
                        await asyncio.wait_for(self.collect_with_recovery(row['link'], job), self.timeout)
                    finally:
                        job['source_owned'] = False
                n = self.store.processed_count(job_id)
                complete = job['expected'] is not None and n == job['expected'] and not job.get('audit_unmatched')
                detail = '' if complete else '发送已静默，无法确认全部文件已收到。'
                if not complete and job.get('source_announced') and job.get('audit_total') == 0:
                    detail = '来源已回复开始发送，但等待并核对聊天历史后仍无媒体，已暂缓自动补收。'
                if not complete and job.get('download_limited'):
                    detail = (f"{job['download_limited']} 个来源文件暂时无法完整下载；"
                              f"已保存 {n}/{job['expected']}，缺件保留等待来源恢复，本轮继续后续文件夹。")
                if job.get('preview_warnings'):
                    detail += ' 部分 HEIC 预览未生成，原文件已保留。'
                if job.get('resume_lookup_warnings'):
                    detail += ' 部分旧媒体标识无法恢复，可能有重新上传的文件。'
                if job.get('audit_unmatched'):
                    detail += ' 部分来源无法匹配已有索引，未重新上传；需要检查来源变化。'
                if job.get('callback_timeout') and not complete:
                    detail += ' 源机器人未确认某次翻组点击，可稍后续传重试。'
                self.store.update(job_id, 'complete' if complete else 'needs_review', job['expected'], detail)
                if complete:
                    self.store.finish_audit(job_id)
                    with self.store.db:
                        self.store.db.execute('UPDATE retry_schedule SET next_at=0 WHERE job=?',(job_id,))
                else:
                    retry_wait = self.schedule_incomplete(job)
                    if retry_wait:
                        detail += (f' 已处理 {n}/{job["expected"]}；约 {retry_wait} 秒后自动排队补收，'
                                   '已保存文件会跳过。')
                    self.store.update(job_id, 'needs_review', job['expected'], detail)
            except asyncio.CancelledError:
                self.store.update(job_id, 'interrupted', job['expected'])
                raise
            except Exception as exc:
                # Do not log links, session secrets, or raw Telegram response contents.
                explanations = {
                    'InsufficientDiskError': '磁盘剩余空间不足，已停止并清理临时文件。',
                    'MediaTooLargeError': '单文件超过配置上限，已停止并清理临时文件。',
                    'IncompleteMediaError': '媒体下载不完整，已清理临时文件。',
                    'DownloadCoverageError': '下载分块不完整，已停止并清理临时文件。',
                    'CdnIntegrityError': 'CDN 内容未通过完整性校验，未写入文件夹，请检查诊断记录。',
                    'ChatForwardsRestrictedError': '源内容禁止转发；如需下载重传，请启用 download_upload 模式。',
                }
                name = type(exc).__name__
                detail = error_detail(exc, job.get('phase', ''))
                if job.get('menu_recovery_failed'):
                    detail += '；来源目录恢复取件失败，已暂缓此任务。'
                    if job.get('menu_recovery_reason'):
                        detail += ' ' + job['menu_recovery_reason']
                if job.get('history_timeout'):
                    detail += '；来源历史核对超过30秒，已暂缓此任务并释放队列。'
                if isinstance(exc, LocalBotUploadRejected):
                    detail += f'；{exc.method}：{exc.reason}'
                if name in explanations:
                    detail += '：' + explanations[name]
                if self.cooldown.remaining():
                    detail += f'；连接冷却还剩约 {self.cooldown.remaining():.0f} 秒，续传会等待冷却。'
                self.store.update(job_id, 'partial' if self.store.files(job_id) else 'failed',
                                  job['expected'], detail)
                retry_wait = self.schedule_retry(job_id, exc)
                if retry_wait:
                    self.store.update(job_id, 'partial' if self.store.files(job_id) else 'failed',
                                      job['expected'], detail + f'；约 {retry_wait} 秒后自动排队重试。')
            finally:
                self.pending = None
                self.active = None
                self.jobs.task_done()
            with contextlib.suppress(Exception):
                status = self.store.get(job_id)
                await self.app.bot.send_message(row['owner'],
                    f"任务 {job_id}：{status['state']}\n{status['detail']}\n/folder {job_id}")

    def worker_limit(self, direction):
        ceiling = self.download_workers if direction == '下载' else self.upload_workers
        return min(ceiling, self.tuners[direction].current) if self.automatic_speed else ceiling

    @contextlib.asynccontextmanager
    async def staged_download(self, message, job):
        async with contextlib.AsyncExitStack() as stack:
            for attempt in range(2):
                pipeline = job.get('download_pipeline')
                if pipeline and pipeline.contains(message):
                    stage = pipeline.stage(message,
                        lambda stats: job.__setitem__('download_stats', stats), self.progress(job, '下载'))
                else:
                    limit = job.get('download_recovery_workers', self.download_workers)
                    stage = self.spool.stage(self.client, message, progress=self.progress(job, '下载'),
                        workers=limit, stats=job['download_stats'],
                        worker_limit=lambda: min(limit, self.worker_limit('下载')), checkpoint_scope=job['id'],
                        cdn=bool(attempt and getattr(message, 'document', None)))
                try:
                    path = await stack.enter_async_context(stage)
                    break
                except BaseException as exc:
                    self.store.record_transfer(job['id'], '下载', job['download_stats'], error_detail(exc, '下载'))
                    if attempt == 1 or type(exc).__name__ not in {'FloodWaitError', 'FloodPremiumWaitError'}:
                        raise
                    if pipeline:
                        await pipeline.close()
                        job['paused_download_pipeline'] = job.pop('download_pipeline')
                    job['download_recovery_workers'] = 1
                    self.cooldown.defer(max(rate_wait(exc, attempt), 10 * 2**attempt), error_detail(exc, '下载'))
                    await self.await_cooldown(job)
                    # Refresh a stale reference only from this exact source
                    # position, and never substitute different media in place.
                    if getattr(message, 'document', None) is not None:
                        try:
                            refreshed = await asyncio.wait_for(self.client.get_messages(
                                message.chat_id, ids=message.id), timeout=15)
                        except Exception as refresh_error:
                            wait = rate_wait(refresh_error, 0)
                            if wait is not None:
                                self.cooldown.defer(wait, error_detail(refresh_error, '刷新来源'))
                                raise refresh_error from exc
                        else:
                            if (refreshed and media_key(refreshed) == media_key(message)
                                    and refreshed.chat_id == message.chat_id and refreshed.id == message.id):
                                message = refreshed
                    self.store.update(job['id'], 'running', job['expected'], '限流等待结束，单路续传当前文件。')
                    job['download_stats'] = {}
            phase = '复用已下载' if job['download_stats'].get('transport') == 'disk_checkpoint' else '下载'
            self.store.record_transfer(job['id'], phase, job['download_stats'])
            yield path
            if phase == '下载' and 'download_recovery_workers' in job:
                limit = job['download_recovery_workers'] * 2
                if limit >= self.worker_limit('下载'):
                    job.pop('download_recovery_workers')
                    pipeline = job.pop('paused_download_pipeline', None)
                    if pipeline is not None:
                        job['download_pipeline'] = pipeline
                else:
                    job['download_recovery_workers'] = limit

    def progress(self, job, label):
        started = time.monotonic()
        last_update = -float('inf')

        def callback(current, total):
            nonlocal last_update
            direction = '下载' if label == '下载' else '上传'
            metrics = job.get('download_stats' if direction == '下载' else 'upload_stats', {})
            if (self.automatic_speed and metrics.get('parts') and not metrics.get('pipeline')
                    and not metrics.get('route_probing')):
                self.tuners[direction].observe(current, total, metrics['parts'])
            now = time.monotonic()
            if now - last_update < 3 and (not total or current < total):
                return
            last_update = now
            elapsed = max(now - started, metrics.get('seconds', 0), 0.01)
            resumed = metrics.get('resumed_bytes', 0)
            rate = max(0, current - resumed) / elapsed
            total_text = f'{total / 1024**2:.1f} MiB' if total else '未知'
            percent = f'{current / total:.1%}' if total else ''
            eta = f'{max(0, total-current) / rate / 60:.1f} 分钟' if total and rate > 0 else '估算中'
            detail = (f'{label}：{current / 1024**2:.1f} / {total_text} {percent}\n'
                      f'平均速度 {rate / 1024**2:.2f} MiB/s；此阶段剩余约 {eta}')
            if label == '下载':
                if resumed:
                    detail += f'\n已复用本地分块 {resumed / 1024**2:.1f} MiB'
                metrics = job.get('download_stats', {})
                detail += f"\n下载并发 {metrics.get('workers', 1)} 路"
                if metrics.get('parts'):
                    detail += f"；分块平均等待 {1000 * metrics['wait_seconds'] / metrics['parts']:.0f} ms"
                if metrics.get('mode') == 'sdk_fallback':
                    detail += '（CDN/文件引用兼容回退）'
            elif label.startswith('上传'):
                metrics = job.get('upload_stats', {})
                detail += f"\n上传并发 {metrics.get('workers', 1)} 路"
                if metrics.get('parts'):
                    detail += f"；分块平均等待 {1000 * metrics['wait_seconds'] / metrics['parts']:.0f} ms"
            if metrics.get('route_probing'):
                detail += '\n正在以相同并发比较两种上传通道。'
            elif self.automatic_speed and metrics.get('parts') and not metrics.get('pipeline'):
                detail += f'\n自动调速：{self.tuners[direction].note}；下一批并发 {self.worker_limit(direction)} 路'
            if metrics.get('recent_seconds', 0) > 0:
                detail += (f"\n近期 {metrics['recent_seconds']:.1f} 秒：{metrics.get('recent_mib', 0):.2f} MiB/s"
                           f"；{metrics.get('connections', 1)} 条连接（{metrics.get('transport', 'sdk')}）")
            if metrics.get('retries'):
                detail += f"\n分块已重试 {metrics['retries']} 次，保留已完成部分。"
            if job.get('audit_total'):
                detail += f"\n内容核验 {job.get('audit_index', 0)} / {job['audit_total']}；确认重复后只合并索引。"
            self.store.update(job['id'], 'running', job['expected'], detail)
        return callback

    async def transfer_media(self, message, key, job, preview_only=False, replace_original=False, existing=None):
        if self.transfer_mode == 'download_upload':
            original = job.get('reusable_documents', {}).get(key)
            if original and not (preview_only or replace_original or existing):
                import json
                analysis = dict(content_sha256=original['content_sha256'],
                                stream_sha256=original['stream_sha256'],
                                quality=json.loads(original['quality_json']))
                duplicate = self.store.duplicate(job['id'], key, analysis)
                if duplicate and preference(analysis['quality']) <= preference(json.loads(duplicate['quality_json'])):
                    self.store.merge_alias(job['id'], key,
                        (duplicate['source_chat'], duplicate['source_message']), media_key(message))
                else:
                    self.store.reuse_document(job['id'], key, original, message.raw_text or '')
                    if duplicate:
                        self.store.merge_alias(job['id'], (duplicate['source_chat'], duplicate['source_message']),
                                               key, duplicate['media_key'])
                job['phase'] = '复用已保存文件'
                self.store.update(job['id'], 'running', job['expected'], '已复用机器人确认保存的同一原文件。')
                self.store.record_transfer(job['id'], '复用', dict(bytes=message.file.size, transport='saved_file_id'))
                return
            if job.get('repair_partial') and not (preview_only or replace_original or existing):
                job['phase']='下载'
                self.store.update(job['id'],'running',job['expected'],
                    '先检查缺件尾部能否读取，避免反复下载整文件。')
                probe_stats={}
                probe_error=''
                try:
                    await self.await_cooldown(job)
                    await probe_original_tail(self.client,message,probe_stats)
                except BaseException as exc:
                    probe_error=error_detail(exc,'来源尾部探测')
                    raise
                finally:
                    self.store.record_transfer(job['id'],'来源探测',probe_stats,probe_error)
                if await recover_identical_copy(self,message,key,job):
                    return
                await self.await_cooldown(job)
            job['phase'] = '下载'
            self.store.update(job['id'], 'running', job['expected'], '正在下载一个文件到临时目录。')
            job['download_stats'] = {}
            self.tuners['下载'].begin()
            async with self.staged_download(message, job) as path:
                analysis = None
                duplicate = None
                if not preview_only:
                    import json
                    job['phase'] = '内容核验'
                    self.store.update(job['id'], 'running', job['expected'],
                        f"核验原始内容和视频参数，不转码、不剥离拍摄信息。\n内容核验 {job.get('audit_index', 0)} / {job.get('audit_total', 0)}")
                    analysis = await analyze(path, native=bool(getattr(getattr(message, 'media', None), 'video', False)))
                    if existing:
                        self.store.set_analysis(job['id'], key, analysis)
                    duplicate = self.store.duplicate(job['id'], key, analysis)
                    if duplicate and preference(analysis['quality']) <= preference(json.loads(duplicate['quality_json'])):
                        self.store.merge_alias(job['id'], key,
                            (duplicate['source_chat'], duplicate['source_message']), media_key(message))
                        return
                    if existing:
                        if duplicate:
                            self.store.merge_alias(job['id'], (duplicate['source_chat'], duplicate['source_message']),
                                                   key, duplicate.get('media_key', ''))
                        return
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
                job['phase'] = '上传'
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
                    self.store.set_analysis(job['id'], key, analysis)
                    if duplicate:
                        self.store.merge_alias(job['id'], (duplicate['source_chat'], duplicate['source_message']),
                                               key, duplicate.get('media_key', ''))
                if preview and not self.document_groups:
                    await self.deliver(message, key, marker=f'preview:{uuid.uuid4().hex}',
                                       upload_path=preview, preview=True)
        else:
            await self.deliver(message, key)

    async def deliver(self, message, key, marker=None, upload_path=None, attrs=None, preview=False, thumbnail=None, replace_original=False):
        if (upload_path is not None and self.upload_backend == 'hybrid'
                and eligible(Path(upload_path).stat().st_size)):
            try:
                return await self.deliver_local(message, key, marker, upload_path, attrs,
                                                preview, thumbnail, replace_original)
            except LocalBotUploadRejected as exc:
                if exc.reason not in {'local_file', 'file_reference', 'file_parts', 'file_size',
                                      'message_rejected', 'unclassified'}:
                    raise
                print(f"本地上传已拒绝，改用中转通道：{self.active['id']} {exc.reason}", flush=True)
        future = asyncio.get_running_loop().create_future()
        self.pending = dict(source=key, future=future, media_key=media_key(message), replace_original=replace_original)
        if marker:
            self.pending.update(marker=marker, caption=message.raw_text or '', preview=preview)
        try:
            if upload_path is None:
                await self.client.forward_messages(self.destination, message)
            else:
                self.active['upload_stats'] = {}
                self.tuners['上传'].begin()
                failure = ''
                try:
                    uploaded = await upload_file(
                        self.client, upload_path,
                        progress=self.progress(self.active, '上传预览' if preview else '上传'),
                        workers=self.upload_workers, worker_limit=lambda: self.worker_limit('上传'),
                        stats=self.active['upload_stats'], transport=await self.get_upload_transport(),
                        route_probe=self.upload_scheduler == 'auto')
                except BaseException as exc:
                    failure = error_detail(exc, '上传')
                    await self.close_upload_transport()
                    raise
                finally:
                    self.store.record_transfer(self.active['id'], '上传',
                        self.active['upload_stats'], failure)
                self.store.update(self.active['id'], 'running', self.active['expected'], '上传完成，等待机器人确认收件。')
                self.active['phase'] = '发送媒体消息'
                await self.client.send_file(
                    self.destination, uploaded, caption=marker, parse_mode=None,
                    force_document=not preview and not bool(getattr(getattr(message, 'media', None), 'video', False))
                        and (self.document_groups or not bool(message.photo)), attributes=attrs or None,
                    thumb=str(thumbnail) if thumbnail else None,
                    supports_streaming=any(isinstance(a, types.DocumentAttributeVideo)
                                           for a in attrs or []))
            self.active['phase'] = '确认机器人收件'
            await asyncio.wait_for(future, 60)
        except BaseException:
            # Do not attribute a late send/forward receipt to another task.
            self.blocked = True
            raise
        finally:
            self.pending = None

    async def deliver_local(self, message, key, marker, path, attrs, preview, thumbnail, replace):
        job = self.active
        self.store.update(job['id'], 'running', job['expected'],
            '本地 Bot API 正在上传（2000 MB 以内）。此通道没有可靠的远端字节进度，确认发送后计入文件夹。')
        filename = next((a.file_name for a in attrs or [] if isinstance(a, types.DocumentAttributeFilename)), Path(path).name)
        photo = preview or (not self.document_groups and bool(message.photo))
        video = None
        if not photo and bool(getattr(getattr(message, 'media', None), 'video', False)):
            attribute = next((a for a in attrs or [] if isinstance(a, types.DocumentAttributeVideo)), None)
            if attribute:
                video = dict(width=attribute.w, height=attribute.h, duration=int(attribute.duration),
                             supports_streaming=bool(attribute.supports_streaming))
        def save(reply, kind, media):
            if preview:
                self.store.set_preview(job['id'], key, media.file_id)
            else:
                self.store.add_file(job['id'], key, reply, media.file_id, kind,
                    caption=message.raw_text or '', source_media_key=media_key(message),
                    has_thumbnail=bool(getattr(media, 'thumbnail', None)) or kind == 'photo', replace=replace)
        async def attempt(video_options, thumb):
            stats, failure = {}, ''
            try:
                return await send_local(self.app.bot, self.store, job['id'], key,
                    self.store.get(job['id'])['owner'], path, marker, filename,
                    photo=photo, thumbnail=thumb, stats=stats, save=save,
                    caption=message.raw_text or '', video=video_options)
            except LocalBotUploadRejected as exc:
                failure = f'{exc.method}: {exc.reason}'
                print(f"本地 Bot API 拒绝上传：{job['id']} {failure}", flush=True)
                raise
            except BaseException as exc:
                failure = type(exc).__name__
                if isinstance(exc, DeliveryUnknownError):
                    self.blocked = True
                raise
            finally:
                self.store.record_transfer(job['id'], '上传', stats, failure)

        try:
            return await attempt(video, thumbnail)
        except LocalBotUploadRejected as exc:
            if thumbnail and not photo and exc.reason == 'thumbnail':
                return await attempt(video, None)
            if video and exc.reason in {'video_format', 'media_dimensions', 'media_invalid',
                                        'message_rejected', 'unclassified'}:
                return await attempt(None, None)
            raise

    async def collect(self, link, job):
        visited = set()
        clicked = set()
        seen = set()
        actions = 0
        remaining = {(x['source_chat'], x['source_message']): x for x in self.store.resume_entries(job['id'])}
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

        plan=[]
        saved = []
        if not job.get('dedup_audit') and not job.get('refresh_source'):
            saved = self.store.saved_source_plan(job['id'], job['expected'])
        if saved:
            peers={}
            for name in self.allowed:
                entity=await self.client.get_entity(name);peers[entity.id]=entity
            loaded={}
            for chat in {x[0] for x in saved}:
                if chat not in peers:break
                messages=await self.client.get_messages(peers[chat],ids=[x[1] for x in saved if x[0]==chat])
                for message in messages:
                    if message and (message.photo or message.document):loaded[(chat,message.id)]=message
            if all(key in loaded for key in saved):
                plan=[loaded[key] for key in saved]
                self.store.update(job['id'],'running',job['expected'],
                                  f'已载入原来源清单 {len(plan)} 条，只补传尚未保存的文件。')
        cached=bool(plan)
        extended_wait = False
        history_checked = False
        menu_used = False
        if not cached and self.store.get(job['id']).get('title'):
            try:
                menu_used = await asyncio.wait_for(open_menu_content(
                    self.client, self.store, job, link, self.allowed,
                    self.settings_file.parent / 'collector.sqlite3'), timeout=120)
            except Exception as error:
                job['menu_recovery_failed'] = True
                job['menu_recovery_reason'] = (str(error) if isinstance(error, MenuChangedError)
                                                else error_detail(error, '来源目录'))
                raise RuntimeError('来源目录恢复取件失败，需要核对来源入口。') from None
        if not cached and not menu_used:
            await follow(link)
        while not cached:
            if job.get('button_error'):
                raise job['button_error']
            if job['overflow']:
                raise RuntimeError('收件队列超限')
            try:
                message = await asyncio.wait_for(job['events'].get(), self.idle)
            except asyncio.TimeoutError:
                if job.get('pending_group_clicks'):
                    continue
                if job.get('source_announced') and not plan and not extended_wait:
                    extended_wait = True
                    continue
                if not history_checked:
                    history_checked = True
                    recovered = []
                    for chat in job['chats']:
                        try:
                            messages = await asyncio.wait_for(self.client.get_messages(
                                chat, min_id=job['baselines'][chat],
                                limit=min(5000, max(100, (job['expected'] or 0) + 20)),
                                reverse=True), timeout=30)
                        except asyncio.TimeoutError:
                            job['history_timeout'] = True
                            raise RuntimeError('来源历史核对超时，已暂缓此任务，继续其他文件夹。') from None
                        recovered.extend(m for m in messages if m and not getattr(m, 'out', False)
                                         and m.id > job['baselines'][chat]
                                         and (m.photo or m.document)
                                         and (m.chat_id, m.id) not in seen)
                    for message in recovered:
                        job['events'].put_nowait(message)
                    if recovered:
                        continue
                if job.get('source_announced') and not plan:
                    job['source_stalled'] = True
                break
            if message.id <= job['baselines'].get(message.chat_id, 0):
                continue
            if '开始发送内容' in (message.raw_text or ''):
                job['source_announced'] = True
            if not job.get('expected_refreshed'):
                expected = media_count(message.raw_text or '')
                if expected is not None:
                    job['expected'] = expected
                    job['expected_refreshed'] = True
                    self.store.update(job['id'], 'running', expected)
            if not self.store.get(job['id']).get('title'):
                title = folder_title(message.raw_text or '')
                if title:
                    self.store.set_title(job['id'], title)
            self.store.set_folder_metadata(job['id'], folder_metadata(message.raw_text or ''))
            key = (message.chat_id, message.id)
            if (message.photo or message.document) and key not in seen:
                seen.add(key)
                if len(plan) >= 5000:
                    raise RuntimeError('文件计划超过安全上限')
                plan.append(message)
                if len(plan)%5==0:
                    self.store.update(job['id'],'running',job['expected'],
                        f"正在收集来源清单：{len(plan)} / {job['expected'] if job['expected'] is not None else '未知'} 个媒体。")
            await handle_buttons(message)
            if job['expected'] is not None and len(plan) >= job['expected'] and not job.get('pending_group_clicks'):
                break
        entries = [source_entry(m, i) for i, m in enumerate(plan, 1)]
        self.store.set_source_entries(job['id'], entries)
        if job['expected'] is not None and len(plan)==job['expected']:
            self.store.save_source_plan(job['id'],plan)
        # Upload in the same source order used for browsing. Native-video
        # preference belongs to duplicate selection, not transfer scheduling.
        job['audit_total'] = len(plan)
        # Prefetch only consecutive unsaved sources. Repairs/audits keep the
        # conservative serial path; Telegram sends and receipt commits stay serial.
        saved_media = {row.get('media_key') for row in remaining.values() if row.get('media_key')}
        allowed = {(m.chat_id, m.id) for m in plan
                   if (m.chat_id, m.id) not in remaining and media_key(m) not in saved_media}
        if (self.spool and self.upload_backend == 'hybrid' and self.document_groups
                and not job.get('dedup_audit')):
            reusable = {}
            for message, entry in zip(plan, entries):
                key = (message.chat_id, message.id)
                if key not in allowed or not entry['size'] or not eligible(entry['size']):
                    continue
                kind = 'video' if getattr(getattr(message, 'media', None), 'video', False) else 'document'
                original = self.store.reusable_document(job['id'], entry, media_key(message), kind)
                needs_thumbnail = self.heic_previews and entry['name'].lower().endswith(('.heic', '.heif'))
                if original and (not needs_thumbnail or original['has_thumbnail']):
                    reusable[key] = original
            job['reusable_documents'] = reusable
            allowed.difference_update(reusable)
        def stage_ahead(message, stats, progress):
            stats['pipeline'] = True
            return self.spool.stage(self.client, message, progress=progress,
                workers=min(8, self.download_workers), stats=stats, checkpoint_scope=job['id'])
        pipeline = DownloadPipeline(plan, allowed, stage_ahead)
        if self.spool and not job.get('dedup_audit') and not job.get('repair_partial'):
            job['download_pipeline'] = pipeline
        try:
            for index, message in enumerate(plan, 1):
                job['audit_index'] = index
                key = (message.chat_id, message.id)
                previous_key = key if key in remaining else next(
                    (source for source, row in remaining.items()
                     if media_key(message) and row.get('media_key') == media_key(message)), None)
                previous = remaining.pop(previous_key) if previous_key is not None else None
                if previous:
                    if previous.get('is_alias'):
                        continue
                    if job.get('dedup_audit'):
                        if not previous.get('content_sha256'):
                            await self.transfer_media(message, previous_key, job, existing=previous)
                        else:
                            # A restart can occur after persisting a fingerprint
                            # but before merging its index row. Finish that merge
                            # without downloading or uploading the original again.
                            import json
                            duplicate = self.store.duplicate(job['id'], previous_key, previous)
                            if duplicate:
                                other = (duplicate['source_chat'], duplicate['source_message'])
                                if preference(json.loads(previous['quality_json'])) > preference(json.loads(duplicate['quality_json'])):
                                    self.store.merge_alias(job['id'], other, previous_key, duplicate.get('media_key', ''))
                                else:
                                    self.store.merge_alias(job['id'], previous_key, other, previous.get('media_key', ''))
                        continue
                    ext = getattr(getattr(message, 'file', None), 'ext', '') or ''
                    if self.transfer_mode == 'download_upload' and self.heic_previews and ext.lower() in {'.heic', '.heif'}:
                        if self.document_groups and not previous.get('has_thumbnail'):
                            await self.transfer_media(message, previous_key, job, replace_original=True)
                        elif not self.document_groups and not previous.get('preview_file_id'):
                            await self.transfer_media(message, previous_key, job, preview_only=True)
                else:
                    if job.get('dedup_audit'):
                        job['audit_unmatched'] = True
                        continue
                    try:
                        await self.transfer_media(message, key, job)
                    except Exception as exc:
                        if (job.get('phase') != '下载' or type(exc).__name__ not in
                                {'FloodWaitError', 'FloodPremiumWaitError'}):
                            raise
                        self.cooldown.defer(rate_wait(exc, 0), error_detail(exc, '下载'))
                        await self.await_cooldown(job)
                        if await recover_identical_copy(self, message, key, job):
                            continue
                        if self.reference_repair and await repair_reference(self, message, key, job):
                            continue
                        job['download_limited'] = job.get('download_limited', 0) + 1
                        await self.await_cooldown(job)
                await asyncio.sleep(self.transfer_pause)
        finally:
            job.pop('download_pipeline', None)
            job.pop('reusable_documents', None)
            await pipeline.close()
        if job.get('dedup_audit') and any(not row.get('content_sha256') for row in self.store.files(job['id'])):
            # Existing saved rows do not prove this audit actually inspected
            # all originals when a source batch stops arriving prematurely.
            job['audit_unmatched'] = True
