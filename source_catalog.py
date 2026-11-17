"""SPXM menu adapter: click list numbers and back only, never content buttons."""
import asyncio
import re
import time
from collector_state import canonical


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
    def __init__(self, client, source, allowed, delay=1.5, timeout=30):
        self.client, self.source, self.allowed = client, source, allowed
        self.delay, self.timeout = delay, timeout

    async def find_panel(self):
        for message in await self.client.get_messages(self.source, limit=200):
            if list_page(message) or any('文件夹列表' in b.text and b.data for b in buttons(message)):
                return message
        raise MenuChangedError('没有找到来源目录入口，请先在来源机器人打开文件夹列表。')

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
        if page and page[0] == wanted:
            return message
        # This exact adapter protocol is observed in this bot's real list/detail keyboards.
        return await self.click(message, f'back_to_folders_list_user/{wanted}'.encode(),
                                lambda m: bool(list_page(m) and list_page(m)[0] == wanted))

    async def scan_page(self, ledger, head=False):
        wanted, _, finished = ledger.page(self.source)
        if finished and not head:
            return False
        if head:
            wanted = 1
        message = await self.page(wanted)
        page, total, numbers = list_page(message)
        # Snapshot callbacks; return to this same page before each subsequent number.
        for number in numbers:
            key = bytes(number.data).hex()
            if ledger.seen(self.source, key):
                continue
            detail = await self.click(message, number.data, lambda m: share_link(m, self.allowed) is not None)
            title, link = share_link(detail, self.allowed)
            ledger.add(self.source, key, title, link)
            print(f'已采集：{title}；目录第 {page}/{total} 页', flush=True)
            backs = [b for b in buttons(detail) if '文件夹列表' in b.text and b.data]
            if len(backs) != 1:
                raise MenuChangedError('详情的返回目录按钮发生变化。')
            message = await self.click(detail, backs[0].data,
                                       lambda m: bool(list_page(m) and list_page(m)[0] == page))
        if not head:
            ledger.advance(self.source, page, total)
        if page == 1:
            ledger.checked_head(self.source)
        print(f'已扫描目录 {page}/{total} 页；累计状态 {ledger.summary()}', flush=True)
        return True
