"""Owner-only operations panel, persistent retry scheduler and metadata backup."""
import asyncio
import contextlib
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard
from telegram.ext import ApplicationHandlerStop
from core import parse_link
import automation_control as control
from menu_actions import menu_guard, retire_menu
from transport_recovery import rate_wait

FAILURES = {'failed', 'partial', 'needs_review', 'blocked', 'interrupted'}
TRANSIENT = {'ConnectionError', 'TimeoutError', 'OSError', 'URLError', 'ServerError', 'TimedOut', 'NetworkError'}


class Operations:
    def controls_root(self):
        return self.settings_file.parent

    def completed_duplicate(self, row):
        try:
            result=self.store.find_link(row['owner'],row['link'],getattr(self,'allowed',{'epan','spxm111_bot'}))
        except ValueError:
            return None
        return result if result and result['state']=='complete' and result['id']!=row['id'] else None

    def failed_rows(self, uid):
        allowed=getattr(self,'allowed',{'epan','spxm111_bot'})
        completed=set()
        for row in self.store.db.execute("SELECT link FROM jobs WHERE owner=? AND state='complete'",(uid,)):
            try:completed.add(parse_link(row[0],allowed))
            except ValueError:pass
        result=[]
        for row in self.store.db.execute("SELECT * FROM jobs WHERE owner=? AND state IN ('failed','partial','needs_review','blocked','interrupted') ORDER BY rowid DESC",(uid,)):
            try:
                if parse_link(row['link'],allowed) in completed and not self.store.unknown_local_send(row['id']):continue
            except ValueError:pass
            result.append(dict(row))
        return result

    def owner_update(self, update):
        return update.effective_chat.type == 'private' and update.effective_user.id in self.owners

    def panel(self, uid):
        root = self.controls_root()
        states = dict(self.store.db.execute('SELECT state,count(*) FROM jobs WHERE owner=? GROUP BY state', (uid,)))
        disk = shutil.disk_usage(root)
        lines = ['🛠 管理员控制面板',
                 '采集：' + ('已暂停' if control.paused(root, 'collector') else '已启用'),
                 '新转存任务：' + ('已暂停' if control.paused(root, 'transfers') else '已启用'),
                 f"任务：完成 {states.get('complete',0)}，排队 {states.get('queued',0)}，运行 {states.get('running',0)}",
                 f"待处理失败：{len(self.failed_rows(uid))}",
                 f'磁盘可用：{disk.free/1024**3:.1f} / {disk.total/1024**3:.1f} GiB',
                 f'并发上限：下载 {self.download_workers}，上传 {self.upload_workers}']
        active = self.active
        if active:
            row = self.store.get(active['id'])
            if row and row['owner'] == uid:
                lines.append(f"当前：{row['title'] or row['id']}\n{row['detail'][:600]}")
        deliveries=dict(self.store.db.execute('SELECT state,count(*) FROM deliveries GROUP BY state'))
        lines.append(f"用户内容发送：排队 {deliveries.get('queued',0)}，运行 {deliveries.get('running',0)}，"
                     f"停止 {deliveries.get('paused',0)}，待确认 {deliveries.get('uncertain',0)}")
        subscribers=self.store.db.execute('SELECT count(*) FROM subscribers WHERE enabled=1').fetchone()[0]
        notices=dict(self.store.db.execute('SELECT state,count(*) FROM notice_queue GROUP BY state'))
        lines.append(f"更新通知：订阅 {subscribers} 人，待发送 {notices.get('queued',0)}，已发送 {notices.get('sent',0)}，"
                     f"待确认 {notices.get('unknown',0)}，失败 {notices.get('failed',0)}")
        collector = root / 'collector.sqlite3'
        heartbeat = root / 'collector-heartbeat.json'
        if heartbeat.exists():
            age=max(0,int(time.time()-json.loads(heartbeat.read_text())['at']))
            lines.append(f'采集器最近心跳：{age} 秒前' + ('（请检查服务）' if age>300 else ''))
        else:
            lines.append('采集器心跳：尚未记录')
        if collector.exists():
            with contextlib.closing(sqlite3.connect(collector.resolve().as_uri()+'?mode=ro',uri=True)) as db:
                scans = db.execute('SELECT page,total,finished FROM cursor').fetchall()
                counts = dict(db.execute('SELECT state,count(*) FROM items GROUP BY state'))
            for page,total,finished in scans:
                lines.append(f'目录采集：已完成 {total if finished else max(0,page-1)}/{total} 页')
            lines.append(f"链接：已完成 {counts.get('complete',0)}，待提交 {counts.get('ready',0)}，需检查 {counts.get('review',0)+counts.get('dispatching',0)}")
        latest = Path(os.getenv('BACKUP_DIR', '/root/sougabot-backups/scheduled'))
        try:
            backups = sorted(latest.glob('metadata-*/manifest.json')) if latest.exists() else []
            backup_status = backups[-1].parent.name.removeprefix('metadata-') if backups else '尚无备份'
        except OSError:
            backup_status = '目录暂不可读，请检查 BACKUP_DIR 权限'
        lines.append('最近备份：' + backup_status)
        keyboard = Keyboard([
            [Button('🔄 刷新',callback_data='bridge_admin:panel')],
            [Button('继续采集' if control.paused(root,'collector') else '暂停采集',callback_data='bridge_admin:collector:'+('resume' if control.paused(root,'collector') else 'pause')),
             Button('继续新转存' if control.paused(root,'transfers') else '暂停新转存',callback_data='bridge_admin:transfers:'+('resume' if control.paused(root,'transfers') else 'pause'))],
            [Button('⚠️ 失败任务',callback_data='bridge_admin:failures:1'),
             Button('💾 立即备份',callback_data='bridge_admin:backup')]])
        return '\n\n'.join(lines), keyboard

    def failures_panel(self, uid, page):
        rows = self.failed_rows(uid)
        pages = max(1,(len(rows)+4)//5)
        page = max(1,min(page,pages))
        lines = [f'⚠️ 待处理任务：{len(rows)} 个（第 {page}/{pages} 页）']
        keys = []
        for row in rows[(page-1)*5:page*5]:
            unknown = self.store.unknown_local_send(row['id'])
            retry = self.store.db.execute('SELECT next_at FROM retry_schedule WHERE job=?',(row['id'],)).fetchone()
            note = f'；约 {max(0,int(retry[0]-time.time()))} 秒后可重试' if retry and retry[0] else ''
            lines.append(f"📁 {row['title'] or row['id']}\n{row['state']}{note}\n{row['detail'][:300]}\n/status {row['id']}")
            if unknown:
                lines.append(f"上传结果未知，请先检查聊天；确认未收到后：\n/resolve {row['id']} retry")
            else:
                keys.append([Button('续传：'+(row['title'] or row['id'])[:35],callback_data='bridge_admin:retry:'+row['id'])])
        navigation = []
        if page>1:navigation.append(Button('上一页',callback_data=f'bridge_admin:failures:{page-1}'))
        if page<pages:navigation.append(Button('下一页',callback_data=f'bridge_admin:failures:{page+1}'))
        if navigation:keys.append(navigation)
        keys.append([Button('返回控制面板',callback_data='bridge_admin:panel')])
        return '\n\n'.join(lines), Keyboard(keys)

    def queue_retry(self, uid, job_id):
        row = self.store.get(job_id)
        if not row or row['owner'] != uid:
            return '没有这个任务。'
        if self.store.unknown_local_send(job_id):
            return '上传结果未知，请先检查聊天并使用 /resolve，不能直接重试。'
        duplicate=self.completed_duplicate(row)
        if duplicate:
            with self.store.db:
                self.store.db.execute('UPDATE retry_schedule SET next_at=0 WHERE job=?',(job_id,))
            return f"该分享已由另一任务完成，无需重复续传。\n/folder {duplicate['id']}"
        if row['state'] not in FAILURES:
            return '此任务已完成、排队或正在运行，无需重复提交。'
        if self.blocked or self.jobs.full():
            return '服务暂不能接受续传，请稍后重试。'
        self.store.update(job_id,'queued',row['expected'],'管理员已安排续传。')
        with self.store.db:
            self.store.db.execute('UPDATE retry_schedule SET next_at=0 WHERE job=?',(job_id,))
        self.jobs.put_nowait(job_id)
        return f'已排队续传：{job_id}；已保存文件会跳过。'

    def schedule_retry(self, job_id, error):
        if (self.transfer_mode != 'download_upload'
                or (type(error).__name__ not in TRANSIENT and rate_wait(error,0) is None)
                or self.store.unknown_local_send(job_id)):
            return
        return self.schedule_attempt(job_id)

    def schedule_incomplete(self, job):
        """Retry a known short source delivery, never an ambiguous upload or audit."""
        job_id = job['id']
        expected = job.get('expected')
        if (self.transfer_mode != 'download_upload' or self.blocked
                or not isinstance(expected, int) or expected <= 0
                or job.get('dedup_audit') or job.get('audit_unmatched') or job.get('overflow')
                or self.store.unknown_local_send(job_id)):
            return
        row = self.store.get(job_id)
        if (not row or row['owner'] not in self.owners or row['state'] != 'needs_review'
                or self.completed_duplicate(row)
                or self.store.processed_count(job_id) >= expected):
            return
        return self.schedule_attempt(job_id)

    def schedule_attempt(self, job_id):
        # Network failures and short source deliveries share one persistent budget.
        row = self.store.db.execute('SELECT attempts FROM retry_schedule WHERE job=?',(job_id,)).fetchone()
        attempts = min(4, (row[0] if row else 0) + 1)
        next_at = time.time() + 60 * 2**(attempts-1) if attempts <= 3 else 0
        if (self.store.get(job_id) or {}).get('partial_notice'):
            next_at = time.time() + 6 * 3600
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO retry_schedule VALUES(?,?,?)',(job_id,attempts,next_at))
        return int(next_at-time.time())+1 if next_at else None

    async def retry_loop(self):
        while True:
            await asyncio.sleep(5)
            if (self.active or not self.jobs.empty() or self.blocked or self.cooldown.remaining()
                    or control.paused(self.controls_root(),'transfers')):
                continue
            rows = self.store.db.execute('SELECT job FROM retry_schedule WHERE next_at>0 AND next_at<=? ORDER BY next_at',
                                         (time.time(),)).fetchall()
            for (job_id,) in rows:
                row = self.store.get(job_id)
                if not row or row['state'] == 'complete' or self.store.unknown_local_send(job_id):
                    with self.store.db:
                        self.store.db.execute('UPDATE retry_schedule SET next_at=0 WHERE job=?',(job_id,))
                    continue
                if row['owner'] in self.owners:
                    self.queue_retry(row['owner'],job_id)
                    break

    async def run_backup(self):
        if getattr(self,'backup_busy',False):
            return '备份正在进行，请稍后查看。'
        self.backup_busy = True
        try:
            project = Path(__file__).resolve().parent
            command = [sys.executable,str(project/'backup_metadata.py'), '--project',str(project),
                       '--destination',os.getenv('BACKUP_DIR','/root/sougabot-backups/scheduled')]
            if os.getenv('SOUGABOT_DIR'):
                command += ['--bot-dir',os.environ['SOUGABOT_DIR']]
            process = await asyncio.create_subprocess_exec(*command,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
            try:
                await asyncio.wait_for(process.communicate(),120)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                return '备份超时，请检查服务日志。'
            return '✅ 备份完成（只含索引和配置，不含媒体）。' if process.returncode==0 else '备份未完成，请检查备份目录权限和服务日志。'
        finally:
            self.backup_busy = False

    async def admin_command(self,update,context):
        if self.owner_update(update):
            text,keys = self.panel(update.effective_user.id)
            await update.message.reply_text(text,reply_markup=keys)
        raise ApplicationHandlerStop

    async def failures_command(self,update,context):
        if self.owner_update(update):
            text,keys = self.failures_panel(update.effective_user.id,1)
            await update.message.reply_text(text,reply_markup=keys)
        raise ApplicationHandlerStop

    @menu_guard
    async def admin_callback(self,update,context):
        query = update.callback_query
        if not self.owner_update(update):
            await query.answer('仅管理员可用',show_alert=True)
            raise ApplicationHandlerStop
        parts=query.data.split(':')
        action=parts[1]
        if action=='backup':
            await query.answer('正在创建备份')
            result=await self.run_backup()
            await context.bot.send_message(update.effective_chat.id,result)
            text,keys=self.panel(update.effective_user.id)
            await context.bot.send_message(update.effective_chat.id,text,reply_markup=keys)
            await retire_menu(self,context.bot,update.effective_chat.id,query.message)
            raise ApplicationHandlerStop
        if action=='retry' and len(parts)==3:
            await query.answer(self.queue_retry(update.effective_user.id,parts[2]),show_alert=True)
        else:
            if action in {'collector','transfers'}:
                if len(parts)!=3 or parts[2] not in {'pause','resume'}:
                    await query.answer('旧控制按钮已过期，请使用更新后的面板。')
                else:
                    control.set_paused(self.controls_root(),action,parts[2]=='pause')
                    await query.answer()
            else:
                await query.answer()
        if action=='failures':
            text,keys=self.failures_panel(update.effective_user.id,int(parts[2]))
        else:
            text,keys=self.panel(update.effective_user.id)
        # A refresh within the same second may have identical content.
        from telegram.error import BadRequest
        try:
            await query.edit_message_text(text,reply_markup=keys)
        except BadRequest as error:
            if 'message is not modified' not in str(error).lower():raise
        raise ApplicationHandlerStop
