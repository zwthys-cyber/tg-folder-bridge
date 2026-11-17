"""Retire used inline menus without removing media, and reject queued double clicks."""
import functools
from telegram.error import TelegramError
from telegram.ext import ApplicationHandlerStop


async def retire_menu(runtime,bot,chat,message,clear=True):
    mid=getattr(message,'message_id',None)
    if not isinstance(mid,int) or not isinstance(chat,int):return
    with runtime.store.db:
        runtime.store.db.execute('INSERT OR IGNORE INTO retired_menus VALUES(?,?)',(chat,mid))
    jumps=getattr(runtime,'pending_jumps',{})
    for key,pending in list(jumps.items()):
        if key[0]==chat and len(pending)>3 and getattr(pending[3],'message_id',None)==mid:
            jumps.pop(key,None)
            delete=getattr(bot,'delete_message',None)
            if delete:
                try:await delete(chat_id=chat,message_id=pending[1])
                except TelegramError:pass
    method=getattr(bot,'edit_message_reply_markup',None)
    if method and clear:
        try:await method(chat_id=chat,message_id=mid,reply_markup=None)
        except TelegramError as error:
            print('旧菜单已失效，按钮清理暂不可用：'+type(error).__name__,flush=True)


def menu_guard(method):
    @functools.wraps(method)
    async def guarded(self,update,context):
        query=update.callback_query
        chat=getattr(update.effective_chat,'id',None)
        mid=getattr(getattr(query,'message',None),'message_id',None)
        if update.effective_chat.type!='private' or not isinstance(chat,int) or not isinstance(mid,int):
            return await method(self,update,context)
        if self.store.db.execute('SELECT 1 FROM retired_menus WHERE chat=? AND message=?',(chat,mid)).fetchone():
            await query.answer('这个菜单已使用，请使用最新菜单或发送 /start。')
            await retire_menu(self,context.bot,chat,query.message)
            raise ApplicationHandlerStop
        busy=getattr(self,'_busy_menus',None)
        if busy is None:self._busy_menus=busy=set()
        key=(chat,mid)
        if key in busy:
            await query.answer('正在处理，请稍候。')
            raise ApplicationHandlerStop
        busy.add(key)
        try:return await method(self,update,context)
        finally:busy.discard(key)
    return guarded
