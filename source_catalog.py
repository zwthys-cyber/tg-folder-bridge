"""SPXM menu adapter: click list numbers and back only, never content buttons."""
import asyncio
import re
import time
from collector_state import canonical
from core import media_count


class MenuChangedError(RuntimeError):
    pass


def buttons(message):
    return [button for row in (message.buttons or []) for button in row]


def list_page(message):
    text = message.raw_text or ''
    match = re.search(r'共\s*(\d+)\s*个文件夹.*?第\s*(\d+)\s*/\s*(\d+)\s*页', text, re.S)
    if not match:
        return None
    count, page, total = map(int, match.groups())
    if not 1 <= page <= total <= 2000 or count < 1:
        return None
    numbers = [b for b in buttons(message) if b.text.strip().isdigit() and b.data]
    return (page, total, numbers) if numbers else None


def share_link(message, allowed):
    text = message.raw_text or ''
    candidates = re.findall(r'https://t\.me/[A-Za-z0-9_]+\?[^\s<>]+', text)
    candidates += [b.url for b in buttons(message) if getattr(b, 'url', None)]
    candidates += [e.url for e in (getattr(message, 'entities', None) or []) if getattr(e, 'url', None)]
    links = set()
    for url in candidates:
        try:
            links.add(canonical(url.rstrip('。），)'), allowed))
        except ValueError:
            pass
    if len(links) != 1 or '分享链接' not in text:
        return None
    title = text.splitlines()[0].strip().lstrip('📁').strip()
    return title, links.pop()


class Catalog:
    def __init__(self, client, source, allowed, delay=1.5, timeout=30, should_pause=None):
        self.client, self.source, self.allowed = client, source, allowed
        self.delay, self.timeout = delay, timeout
        self.should_pause = should_pause

    async def find_panel(self):
        for message in await self.client.get_messages(self.source, limit=200):
            if self.is_panel(message):
                return message
        sent = await self.client.send_message(self.source, '/start')
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            for message in await self.client.get_messages(self.source, limit=5):
                if message.id > sent.id and self.is_panel(message):
                    return message
            await asyncio.sleep(0.5)
        raise MenuChangedError('来源未回复可识别的文件夹目录入口。')

    @staticmethod
    def is_panel(message):
        return bool(list_page(message) or any('文件夹列表' in b.text and b.data for b in buttons(message)))

    async def click(self, message, data, predicate):
        await asyncio.sleep(self.delay)
        latest = await self.client.get_messages(self.source, limit=1)
        baseline = latest[0].id if latest else message.id
        try:
            # Answer timeout can still mean an edit succeeded; poll before retrying anything.
            await asyncio.wait_for(message.click(data=data), timeout=self.timeout)
        except Exception as error:
            if type(error).__name__ not in {'BotResponseTimeoutError', 'TimeoutError'}:
                raise
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            candidates = [await self.client.get_messages(self.source, ids=message.id)]
            candidates += list(await self.client.get_messages(self.source, limit=5))
            for current in candidates:
                if current and (current.id == message.id or current.id > baseline) and predicate(current):
                    return current
            await asyncio.sleep(0.5)
        raise MenuChangedError('目录点击后未确认预期页面；已保存进度，停止本轮。')

    async def page(self, wanted):
        message = await self.find_panel()
        page = list_page(message)
        if not page:
            backs = [b for b in buttons(message) if '文件夹列表' in b.text and b.data]
            if len(backs) != 1:
                raise MenuChangedError('没有找到详情的返回目录按钮。')
            message = await self.click(message, backs[0].data, lambda m: list_page(m) is not None)
            page = list_page(message)
        if not 1<=wanted<=page[1]:
            raise MenuChangedError('目录页码已失效。')
        jumps = [b for b in buttons(message) if '跳转' in b.text and b.data]
        if abs(wanted-page[0]) >= 3 and len(jumps)==1:
            return await self.jump_page(message,jumps[0].data,wanted)
        visited=set()
        while page[0]!=wanted:
            if page[0] in visited:raise MenuChangedError('来源翻页没有前进。')
            visited.add(page[0])
            direction=1 if wanted>page[0] else -1
            target=page[0]+direction
            # Telegram rejects data absent from the current keyboard. Walk real
            # previous/next buttons; never invent a direct jump callback.
            navigation=[b.data for b in buttons(message)
                        if b.data==f'folders_list_page/{target}'.encode()]
            if len(navigation)!=1:raise MenuChangedError('来源没有预期的上一页或下一页按钮。')
            message=await self.click(message,navigation[0],
                lambda m:bool(list_page(m) and list_page(m)[0]==target))
            if target==wanted:return message
            page=list_page(message)
        return message

    async def jump_page(self,panel,data,wanted):
        prompt = await self.click(panel,data,
            lambda m:'请输入要跳转的页码' in (m.raw_text or ''))
        sent = await self.client.send_message(self.source,str(wanted),reply_to=prompt.id)
        deadline = time.monotonic()+self.timeout
        while time.monotonic() < deadline:
            candidates = [await self.client.get_messages(self.source,ids=prompt.id)]
            candidates += list(await self.client.get_messages(self.source,limit=5))
            for current in candidates:
                if (current and (current.id in {prompt.id,panel.id} or current.id>sent.id)
                        and list_page(current) and list_page(current)[0]==wanted):
                    return current
            await asyncio.sleep(0.5)
        raise MenuChangedError('来源页码跳转未确认，保留采集断点。')

    async def scan_page(self, ledger, head=False, head_page=1):
        wanted, _, finished = ledger.page(self.source)
        if finished and not head:
            return False
        if head:
            ledger.begin_head(self.source)
            wanted = head_page
        result = await self.inspect_page(ledger, wanted, head=head)
        if result is None:
            return False
        page, self.head_total, self.head_new = result
        if not head:
            ledger.advance(self.source, page, self.head_total)
            if page == 1:
                ledger.checked_head(self.source)
        return True

    async def inspect_page(self, ledger, wanted, *, head=False):
        message = await self.page(wanted)
        page, total, numbers = list_page(message)
        new = 0
        # Snapshot callbacks; return to this same page before each subsequent number.
        for number in numbers:
            if self.should_pause and self.should_pause():
                return None
            # A numbered callback identifies a menu position, not a folder.
            # Read details again and deduplicate by the canonical share link.
            detail = await self.click(message, number.data, lambda m: share_link(m, self.allowed) is not None)
            title, link = share_link(detail, self.allowed)
            if head and not ledger.known_before_head(self.source, link):
                new += 1
            ledger.add(self.source, 'share:' + link, title, link, media_count(detail.raw_text or ''))
            print(f'已采集：{title}；目录第 {page}/{total} 页', flush=True)
            backs = [b for b in buttons(detail) if '文件夹列表' in b.text and b.data]
            if len(backs) != 1:
                raise MenuChangedError('详情的返回目录按钮发生变化。')
            message = await self.click(detail, backs[0].data,
                                       lambda m: bool(list_page(m) and list_page(m)[0] == page))
        print(f'已扫描目录 {page}/{total} 页；累计状态 {ledger.summary()}', flush=True)
        return page, total, new

    async def scan_head(self, ledger, max_pages=3):
        """Bounded metadata-only sweep; resume after restarts without losing new links."""
        ledger.begin_head(self.source)
        page, clean = ledger.head_position(self.source)
        for _ in range(max_pages):
            if not await self.scan_page(ledger, head=True, head_page=page):
                return False
            clean = clean + 1 if self.head_new == 0 else 0
            if page >= self.head_total or clean >= 2:
                ledger.checked_head(self.source)
                return True
            page += 1
            ledger.head_checkpoint(self.source, page, clean)
        return False

    async def revisit(self, ledger):
        if self.head_total < 3 or not ledger.page(self.source)[2]:
            return
        result = await self.inspect_page(ledger, ledger.revisit_page(self.source, self.head_total))
        if result is not None:
            page, total, _ = result
            ledger.checked_revisit(self.source, page, total)
