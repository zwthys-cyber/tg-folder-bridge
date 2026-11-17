"""Private-chat directory page jumps without treating ordinary search numbers as pages."""
import time
from telegram import ForceReply
from telegram.ext import ApplicationHandlerStop
from catalog_ui import catalog_page


class BrowseNavigation:
    def jump_rows(self, prefix, uid):
        if prefix == 'bridge_catalog':
            return self.store.catalog(), False
        if prefix == 'bridge_favorites':
            return self.favorite_rows(uid), True
        if prefix.startswith('bridge_search:'):
            session = self.search_sessions.get(prefix.split(':',1)[1])
            if session and session[0] == uid:
                return self.store.search_catalog(session[1],uid,owned=uid in self.owners), False
        raise ValueError('列表已过期，请重新打开。')

    async def jump_to(self, update, context, prefix, page):
        try:
            rows,favorites=self.jump_rows(prefix,update.effective_user.id)
            pages=max(1,(len(rows)+9)//10)
            if not 1<=page<=pages:
                raise ValueError(f'请输入 1–{pages} 之间的页码。')
        except ValueError as error:
            await update.message.reply_text(str(error))
            return False
        text,keys=catalog_page(rows,page,prefix,favorites=favorites)
        sent=await context.bot.send_message(update.effective_chat.id,text,reply_markup=keys)
        if prefix=='bridge_catalog':
            self.store.register_catalog_panel(update.effective_chat.id,sent.message_id)
        return True

    async def page_command(self,update,context):
        if update.effective_chat.type=='private':
            try:
                if len(context.args)!=1:raise ValueError
                page=int(context.args[0])
            except ValueError:
                await update.message.reply_text('用法：/page 页码，例如 /page 2')
            else:
                await self.jump_to(update,context,'bridge_catalog',page)
        raise ApplicationHandlerStop

    async def jump_callback(self,update,context):
        query=update.callback_query
        if update.effective_chat.type!='private':
            await query.answer()
            raise ApplicationHandlerStop
        prefix=query.data.split(':',1)[1]
        try:
            rows,_=self.jump_rows(prefix,update.effective_user.id)
        except ValueError as error:
            await query.answer(str(error),show_alert=True)
            raise ApplicationHandlerStop
        await query.answer()
        pages=max(1,(len(rows)+9)//10)
        prompt=await context.bot.send_message(update.effective_chat.id,
            f'请回复要跳转的页码（1–{pages}），发送 /cancel 取消。',reply_markup=ForceReply(selective=False))
        if len(self.pending_jumps)>=1000:
            self.pending_jumps.pop(next(iter(self.pending_jumps)))
        self.pending_jumps[(update.effective_chat.id,update.effective_user.id)] = (prefix,prompt.message_id,time.time()+300)
        raise ApplicationHandlerStop

    async def handle_jump_reply(self,update,context,text):
        key=(update.effective_chat.id,update.effective_user.id)
        pending=self.pending_jumps.get(key)
        if not pending or update.effective_chat.type!='private':return False
        if pending[2]<time.time():
            self.pending_jumps.pop(key,None)
            return False
        replied=getattr(update.message,'reply_to_message',None)
        if not replied or replied.message_id!=pending[1]:return False
        if not text.isdigit() or len(text)>6:
            await update.message.reply_text('请输入有效页码，或 /cancel 取消。')
            return True
        if await self.jump_to(update,context,pending[0],int(text)):
            self.pending_jumps.pop(key,None)
        return True

    async def cancel_jump(self,update,context):
        if update.effective_chat.type=='private':
            self.pending_jumps.pop((update.effective_chat.id,update.effective_user.id),None)
            await update.message.reply_text('已取消页码输入。')
        raise ApplicationHandlerStop
