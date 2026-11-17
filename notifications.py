"""Opt-in folder notifications with a durable outbox and no blind network replay."""
import asyncio
import time
from telegram import InlineKeyboardButton as Button,InlineKeyboardMarkup as Keyboard
from telegram.error import RetryAfter,Forbidden,BadRequest
from telegram.ext import ApplicationHandlerStop
from menu_actions import menu_guard,retire_menu


class Notifications:
    def subscribed(self,uid):
        row=self.store.db.execute('SELECT enabled FROM subscribers WHERE owner=?',(uid,)).fetchone()
        return bool(row and row[0])

    def set_subscription(self,uid,enabled):
        # Record every already-complete folder before activating a new subscriber.
        # This also closes the completion-vs-subscribe boundary between loop ticks.
        if enabled:self.publish_ready()
        with self.store.db:
            self.store.db.execute('INSERT INTO subscribers VALUES(?,?) ON CONFLICT(owner) DO UPDATE SET enabled=excluded.enabled',
                                  (uid,int(enabled)))
            if not enabled:
                self.store.db.execute("UPDATE notice_queue SET state='cancelled' WHERE owner=? AND state='queued'",(uid,))

    async def notification_settings(self,bot,uid):
        enabled=self.subscribed(uid)
        text=('🔔 新文件夹通知：已开启' if enabled else '🔕 新文件夹通知：未开启')
        text+='\n仅通知订阅后新完成的文件夹，不补发历史更新。'
        keys=Keyboard([[Button('取消订阅' if enabled else '订阅新文件夹通知',
                               callback_data='bridge_notice:'+('off' if enabled else 'on'))]])
        await bot.send_message(uid,text,reply_markup=keys)

    async def notifications_command(self,update,context):
        if update.effective_chat.type=='private':
            command=(update.message.text or '').split()[0].split('@')[0].lower()
            if command in {'/subscribe','/unsubscribe'}:
                self.set_subscription(update.effective_user.id,command=='/subscribe')
            await self.notification_settings(context.bot,update.effective_user.id)
        raise ApplicationHandlerStop

    @menu_guard
    async def notice_callback(self,update,context):
        query=update.callback_query
        if update.effective_chat.type!='private':raise ApplicationHandlerStop
        action=query.data.split(':')[-1]
        if action not in {'on','off'}:
            await query.answer('通知设置已过期，请发送 /notifications。')
            raise ApplicationHandlerStop
        self.set_subscription(update.effective_user.id,action=='on')
        await query.answer('已订阅' if action=='on' else '已取消订阅')
        await self.notification_settings(context.bot,update.effective_user.id)
        await retire_menu(self,context.bot,update.effective_chat.id,query.message)
        raise ApplicationHandlerStop

    def publish_ready(self):
        jobs=self.store.db.execute("SELECT id FROM jobs WHERE state='complete' AND id NOT IN (SELECT job FROM notice_events)").fetchall()
        with self.store.db:
            for (job,) in jobs:
                self.store.db.execute('INSERT INTO notice_events VALUES(?,?)',(job,time.time()))
                self.store.db.execute("INSERT OR IGNORE INTO notice_queue(job,owner,state) SELECT ?,owner,'queued' "
                                      'FROM subscribers WHERE enabled=1',(job,))

    def recover_notices(self):
        with self.store.db:
            self.store.db.execute("UPDATE notice_queue SET state='unknown',error='InterruptedSend' WHERE state='sending'")

    async def send_notice(self,row):
        current=self.store.db.execute('SELECT state,attempts FROM notice_queue WHERE job=? AND owner=?',
                                      (row['job'],row['owner'])).fetchone()
        if not current or current['state']!='queued':return
        row=dict(row,attempts=current['attempts'])
        job=self.store.get(row['job'])
        if not self.subscribed(row['owner']) or not job or (job['state']!='complete' and not job.get('audit_public')):
            with self.store.db:
                self.store.db.execute("UPDATE notice_queue SET state='cancelled' WHERE job=? AND owner=?",(row['job'],row['owner']))
            return
        count=self.store.db.execute('SELECT count(*) FROM files WHERE job=?',(row['job'],)).fetchone()[0]
        text=f"📁 新文件夹已上架\n\n{(job['title'] or '新文件夹')[:160]}\n共 {count} 个文件"
        keys=Keyboard([[Button('查看文件夹',callback_data=f"bridge_open:{job['id']}:1")],
                       [Button('取消更新通知',callback_data='bridge_notice:off')]])
        state,error,next_at='sent','',0
        with self.store.db:
            claimed=self.store.db.execute("UPDATE notice_queue SET state='sending',attempts=attempts+1 "
                                          "WHERE job=? AND owner=? AND state='queued'",(row['job'],row['owner'])).rowcount
        if not claimed:return
        try:
            await self.app.bot.send_message(row['owner'],text,reply_markup=keys)
        except asyncio.CancelledError:
            with self.store.db:
                self.store.db.execute("UPDATE notice_queue SET state='unknown',error='InterruptedSend' WHERE job=? AND owner=?",
                                      (row['job'],row['owner']))
            raise
        except RetryAfter as exc:
            delay=exc.retry_after.total_seconds() if hasattr(exc.retry_after,'total_seconds') else float(exc.retry_after)
            state='queued' if row['attempts']<3 else 'failed'
            error='RetryAfter';next_at=time.time()+delay+1
        except Forbidden:
            self.set_subscription(row['owner'],False)
            state,error='blocked','Forbidden'
        except BadRequest:
            state,error='failed','BadRequest'
        except Exception as exc:
            state,error='unknown',type(exc).__name__
        with self.store.db:
            self.store.db.execute('UPDATE notice_queue SET state=?,error=?,next_at=? WHERE job=? AND owner=?',
                                  (state,error,next_at,row['job'],row['owner']))

    async def notice_loop(self):
        self.recover_notices()
        while True:
            try:
                self.publish_ready()
                row=self.store.db.execute("SELECT * FROM notice_queue WHERE state='queued' AND next_at<=? "
                                          'ORDER BY rowid LIMIT 1',(time.time(),)).fetchone()
                if row:await self.send_notice(dict(row))
            except asyncio.CancelledError:raise
            except Exception as exc:
                print('更新通知暂不可用：'+type(exc).__name__,flush=True)
            await asyncio.sleep(1)
