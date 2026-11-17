"""Durable background delivery of one whole folder, using this bot's file_ids."""
import asyncio
import contextlib
import json
from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard
from telegram import InputMediaPhoto, InputMediaVideo, InputMediaDocument
from telegram.error import RetryAfter, BadRequest, Forbidden
from telegram.ext import ApplicationHandlerStop
from catalog_ui import folder_details, favorite_button
from menu_actions import menu_guard, retire_menu


class DeliveryPaused(Exception):
    pass


def display_order(items):
    """Stable display-only partition; source metadata and existing plans stay intact."""
    from capture_time import epoch
    def key(item):
        rank=0 if item['kind']=='document' else (2 if item['kind']=='video' else 1)
        date=epoch(item)
        return rank,date is None,-date if date is not None else 0
    return sorted(items,key=key)


def batches(items):
    result=[]
    for item in items:
        family=item['kind']
        previous=result[-1][0]['kind'] if result else None
        if (not result or len(result[-1])>=10 or family!=previous
                or family not in {'photo','video','document'}):
            result.append([])
        result[-1].append(item)
    return result


class WholeDelivery:
    def delivery(self,sid,uid=None):
        row=self.store.db.execute('SELECT * FROM deliveries WHERE id=?',(sid,)).fetchone()
        return dict(row) if row and (uid is None or row['owner']==uid) else None

    def delivery_state(self,sid,state,pending=None,error=''):
        with self.store.db:
            if pending is None:
                self.store.db.execute('UPDATE deliveries SET state=?,error=? WHERE id=?',(state,error,sid))
            else:
                self.store.db.execute('UPDATE deliveries SET state=?,pending=?,error=? WHERE id=?',
                                      (state,json.dumps(pending) if pending else '',error,sid))

    def delivery_keys(self,row):
        sid=row['id'];job=self.store.get(row['job'])
        keys=[]
        if row['state']=='paused':
            keys.append([Button('重试发送',callback_data=f'bridge_delivery:{sid}:resume')])
        elif row['state']=='uncertain':
            keys += [[Button('确认上一批已收到',callback_data=f'bridge_delivery:{sid}:received')],
                     [Button('确认未收到，重发上一批',callback_data=f'bridge_delivery:{sid}:retry')]]
        if job:
            keys.append([Button('📁 文件夹详情',callback_data=f"bridge_open:{job['id']}:1"),
                         favorite_button(job,self.store.is_favorite(row['owner'],job['id']))])
        keys.append([Button('📁 文件夹列表',callback_data='bridge_catalog:1')])
        return Keyboard(keys)

    async def delivery_status(self,bot,row):
        plan=json.loads(row['plan']);progress=self.store.view(row['id'],row['owner'])
        received=progress.received_count(plan)
        state={'queued':'已排队','running':'正在连续发送','stopping':'当前一批结束后停止',
               'paused':'已停止发送','uncertain':'上一批发送结果未确认','complete':'内容已发送完成'}.get(row['state'],row['state'])
        job=self.store.get(row['job']) or {}
        text=f"📁 {job.get('title') or row['job']}\n{state}\n已确认发送 {received} / {len(plan)} 个文件"
        if row['state']=='uncertain':
            pending=json.loads(row['pending']) if row['pending'] else {}
            count=len(pending.get('items',[]))
            text+=f'\n请先检查聊天中的上一批（{count} 个媒体文件），确认后选择下方操作。' if count else '\n请检查上一条详情或完成提示是否收到，再选择下方操作。'
        elif row.get('error'):
            text+='\n有内容暂时无法发送，可稍后继续或联系管理员。'
        await bot.send_message(row['owner'],text,reply_markup=self.delivery_keys(row))

    async def queue_whole_folder(self,bot,uid,job,origin):
        if not self.can_view(job,uid):
            await bot.send_message(uid,'文件夹尚未就绪或已移除。')
            return
        if job['state']!='complete' and not job.get('audit_public'):
            await bot.send_message(uid,'这个文件夹还在导入，完成后即可发送全部内容。')
            return
        existing=self.store.db.execute("SELECT * FROM deliveries WHERE owner=? AND job=? AND "
            "(origin=? OR state IN ('queued','running','stopping','paused','uncertain')) ORDER BY rowid DESC LIMIT 1",
            (uid,job['id'],origin)).fetchone()
        if existing:
            row=dict(existing)
            if row['state']=='paused':
                self.delivery_state(row['id'],'queued',pending={})
                row=self.delivery(row['id'],uid)
            await self.delivery_status(bot,row)
            return True
        busy=self.store.db.execute("SELECT * FROM deliveries WHERE owner=? AND state IN ('queued','running','stopping') LIMIT 1",(uid,)).fetchone()
        if busy:
            await bot.send_message(uid,'已有文件夹正在发送，请等待完成后再打开另一个。',
                                   reply_markup=self.delivery_keys(dict(busy)))
            return
        fields=('job','source_chat','source_message','file_id','kind','caption','source_kind','file_size','source_name',
                'captured_at','capture_time_source','capture_timezone_assumed')
        plan=[{key:item.get(key) for key in fields} for item in display_order(self.store.files(job['id']))]
        progress=self.store.start_view(uid,job['id'])
        with self.store.db:
            self.store.db.execute('INSERT INTO deliveries(id,owner,job,origin,state,plan) VALUES(?,?,?,?,?,?)',
                                  (progress.id,uid,job['id'],origin,'queued',json.dumps(plan)))
        ahead=self.store.db.execute("SELECT 1 FROM deliveries WHERE id<>? AND state IN ('queued','running','stopping') LIMIT 1",(progress.id,)).fetchone()
        if ahead:
            await self.delivery_status(bot,self.delivery(progress.id,uid))
        return True

    def recover_deliveries(self):
        with self.store.db:
            self.store.db.execute("UPDATE deliveries SET state='uncertain' WHERE pending<>'' AND state IN ('running','stopping')")
            self.store.db.execute("UPDATE deliveries SET state='paused' WHERE state='stopping' AND pending=''")
            self.store.db.execute("UPDATE deliveries SET state='queued' WHERE state='running' AND pending=''")

    async def delivery_call(self,row,call):
        sid=row['id']
        while True:
            current=self.delivery(sid)
            if current['state']=='stopping':
                self.delivery_state(sid,'paused',pending={})
                raise DeliveryPaused
            try:
                return await call()
            except RetryAfter as error:
                # Explicit rejection: no media was accepted, so this retry is safe.
                self.delivery_state(sid,self.delivery(sid)['state'],pending={})
                delay=error.retry_after
                seconds=delay.total_seconds() if hasattr(delay,'total_seconds') else float(delay)
                await asyncio.sleep(seconds+1)
                if self.delivery(sid)['state']=='stopping':
                    self.delivery_state(sid,'paused',pending={})
                    raise DeliveryPaused
                # Restore intent immediately before the next network attempt.
                self.delivery_state(sid,'running',pending=row['_intent'])

    async def delivery_step(self,row,pending,call,accepted):
        sid=row['id']
        if self.delivery(sid)['state']=='stopping':raise DeliveryPaused
        self.delivery_state(sid,'running',pending=pending)
        row['_intent']=pending
        await self.delivery_call(row,call)
        # Receipt and cleared intent commit together. A crash before this point is uncertain.
        with self.store.db:
            accepted()
            state='paused' if self.delivery(sid)['state']=='stopping' else 'running'
            self.store.db.execute("UPDATE deliveries SET pending='',state=? WHERE id=?",(state,sid))
        if state=='paused':raise DeliveryPaused

    async def send_delivery_batch(self,bot,uid,items):
        if len(items)>1:
            media_types={'photo':InputMediaPhoto,'video':InputMediaVideo,'document':InputMediaDocument}
            return await bot.send_media_group(chat_id=uid,media=[media_types[x['kind']](
                media=x['file_id'],caption=(x.get('caption') or '')[:1024],parse_mode=None) for x in items])
        item=items[0];kind=item['kind']
        kwargs={'chat_id':uid,kind:item['file_id']}
        if kind!='video_note':kwargs.update(caption=(item.get('caption') or '')[:1024],parse_mode=None)
        return await getattr(bot,'send_'+kind)(**kwargs)

    def accepted_delivery_intent(self,row,pending):
        kind=pending['kind']
        # Use SQL here so the caller controls a single transaction with intent clearing.
        if kind=='detail':
            self.store.db.execute('UPDATE view_sessions SET detail_sent=1,sent_messages=sent_messages+1 WHERE id=? AND detail_sent=0',(row['id'],))
        elif kind=='media':
            items=pending['items']
            self.store.db.executemany('INSERT OR IGNORE INTO view_receipts VALUES(?,?,?)',
                [(row['id'],x['source_chat'],x['source_message']) for x in items])
            self.store.db.execute('UPDATE view_sessions SET sent_messages=sent_messages+? WHERE id=?',(len(items),row['id']))

    async def execute_delivery(self,row):
        sid,uid=row['id'],row['owner'];job=self.store.get(row['job']);bot=self.app.bot
        if not self.can_view(job,uid):
            self.delivery_state(sid,'paused',pending={},error='FolderUnavailable')
            return
        plan=json.loads(row['plan']);progress=self.store.view(sid,uid)
        self.delivery_state(sid,'running')
        try:
            if not progress.details_sent:
                pending={'kind':'detail'}
                await self.delivery_step(row,pending,lambda:bot.send_message(uid,folder_details(job,plan)),
                                         lambda:self.accepted_delivery_intent(row,pending))
            received={tuple(r) for r in self.store.db.execute('SELECT source_chat,source_message FROM view_receipts WHERE session=?',(sid,))}
            remaining=[x for x in plan if (x['source_chat'],x['source_message']) not in received]
            for group in batches(remaining):
                pending={'kind':'media','items':group}
                await self.delivery_step(row,pending,lambda:self.send_delivery_batch(bot,uid,group),
                                         lambda:self.accepted_delivery_intent(row,pending))
                await asyncio.sleep(1)
            pending={'kind':'footer'}
            await self.delivery_step(row,pending,lambda:bot.send_message(uid,
                f'✅ 文件夹内容发送完成\n\n共发送 {progress.message_count} 条消息',
                reply_markup=self.delivery_keys(dict(row,state='complete'))),lambda:None)
            self.delivery_state(sid,'complete',pending={})
        except DeliveryPaused:
            self.delivery_state(sid,'paused',pending={})
            await self.delivery_status(bot,self.delivery(sid))
        except asyncio.CancelledError:
            current=self.delivery(sid)
            self.delivery_state(sid,'uncertain' if current['pending'] else ('paused' if current['state']=='stopping' else 'queued'))
            raise
        except (BadRequest,Forbidden) as error:
            self.delivery_state(sid,'paused',pending={},error=type(error).__name__)
            print('连续发送暂停：'+type(error).__name__,flush=True)
            if not isinstance(error,Forbidden):
                await self.delivery_status(bot,self.delivery(sid))
        except Exception as error:
            current=self.delivery(sid)
            self.delivery_state(sid,'uncertain' if current['pending'] else 'paused',error=type(error).__name__)
            print('连续发送暂停：'+type(error).__name__,flush=True)
            with contextlib.suppress(Exception):
                await self.delivery_status(bot,self.delivery(sid))

    async def delivery_loop(self):
        self.recover_deliveries()
        while True:
            row=self.store.db.execute("SELECT * FROM deliveries WHERE state='queued' ORDER BY rowid LIMIT 1").fetchone()
            if row:
                try:
                    await self.execute_delivery(dict(row))
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    current=self.delivery(row['id'])
                    self.delivery_state(row['id'],'uncertain' if current['pending'] else 'paused',error=type(error).__name__)
                    print('连续发送状态通知失败：'+type(error).__name__,flush=True)
                    await asyncio.sleep(1)
            else:
                await asyncio.sleep(.5)

    @menu_guard
    async def delivery_callback(self,update,context):
        query=update.callback_query
        parts=query.data.split(':')
        row=self.delivery(parts[1],update.effective_user.id) if len(parts)==3 else None
        if update.effective_chat.type!='private' or not row:
            await query.answer('此发送任务不可用',show_alert=True)
            raise ApplicationHandlerStop
        action=parts[2]
        if action=='stop':
            if row['state']=='queued':self.delivery_state(row['id'],'paused',pending={})
            elif row['state']=='running':self.delivery_state(row['id'],'stopping')
        elif action=='resume' and row['state']=='paused':
            self.delivery_state(row['id'],'queued',pending={})
        elif action in {'received','retry'} and row['state']=='uncertain':
            pending=json.loads(row['pending']) if row['pending'] else {}
            with self.store.db:
                if action=='received' and pending:self.accepted_delivery_intent(row,pending)
                state='complete' if action=='received' and pending.get('kind')=='footer' else 'queued'
                self.store.db.execute("UPDATE deliveries SET state=?,pending='' WHERE id=?",(state,row['id']))
        await query.answer()
        await self.delivery_status(context.bot,self.delivery(row['id']))
        await retire_menu(self,context.bot,update.effective_chat.id,query.message)
        raise ApplicationHandlerStop
