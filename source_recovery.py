"""Recover an empty import through observed source directory buttons."""
import asyncio
import contextlib
import sqlite3
import time
from pathlib import Path

from collector_state import canonical
from core import folder_metadata, folder_title, media_count
from source_catalog import Catalog, MenuChangedError, buttons, list_page, share_link


async def open_menu_content(client, store, job, link, allowed, ledger):
    ledger = Path(ledger)
    if not ledger.exists() or job.get('dedup_audit'):
        return False
    with contextlib.closing(sqlite3.connect(ledger.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        rows = db.execute('SELECT source,item_key FROM items WHERE job=? OR link=?',
                          (job['id'], canonical(link, allowed))).fetchall()
    if len(rows) != 1:
        return False
    source, key = rows[0]
    try:
        action, folder, page, origin = bytes.fromhex(key).decode().split('/')
    except (ValueError, UnicodeError):
        return False
    if (source.lower() not in allowed or action != 'folder_preview_list'
            or not folder.isdigit() or not page.isdigit() or origin != 'list'):
        return False
    wanted = int(page)
    if not 1 <= wanted <= 2000:
        return False
    peer = await client.get_entity(source)
    if not getattr(peer, 'bot', False):
        raise MenuChangedError('来源目录目标不是机器人')
    latest = await client.get_messages(peer, limit=1)
    job['chats'].add(peer.id)
    job['baselines'][peer.id] = latest[0].id if latest else 0
    catalog = Catalog(client, source, allowed, delay=1.5, timeout=15)
    # Old directory keyboards may be stale. Obtain a fresh home menu first.
    await client.send_message(peer, '/start')
    deadline = time.monotonic() + 15
    home = None
    while time.monotonic() < deadline:
        for message in await client.get_messages(peer, limit=5):
            entries = [b for b in buttons(message) if '文件夹列表' in b.text and b.data]
            if message.id > job['baselines'][peer.id] and len(entries) == 1:
                home = message, entries[0]
                break
        if home:
            break
        await asyncio.sleep(0.5)
    if not home:
        raise MenuChangedError('来源未返回新的目录入口')
    panel = await catalog.click(home[0], home[1].data, lambda m: list_page(m) is not None)
    marker = list_page(panel)
    if wanted > marker[1]:
        raise MenuChangedError('来源目录页数已经变化')
    if marker[0] != wanted:
        jumps = [b for b in buttons(panel) if '跳转' in b.text and b.data]
        if len(jumps) != 1:
            raise MenuChangedError('来源目录缺少跳转入口')
        prompt = await catalog.click(panel, jumps[0].data,
                                     lambda m: '请输入要跳转的页码' in (m.raw_text or ''))
        sent = await client.send_message(peer, str(wanted), reply_to=prompt.id)
        deadline = time.monotonic() + 15
        found = None
        while time.monotonic() < deadline:
            candidates = [await client.get_messages(peer, ids=prompt.id)]
            candidates += list(await client.get_messages(peer, limit=5))
            found = next((m for m in candidates if m
                          and (m.id in {prompt.id,panel.id} or m.id > sent.id)
                          and list_page(m)
                          and list_page(m)[0] == wanted), None)
            if found:
                break
            await asyncio.sleep(0.5)
        if found is None:
            raise MenuChangedError('来源页码跳转未确认')
        panel = found
    candidates = [b for b in buttons(panel) if b.data
                  and b.data.decode(errors='replace').split('/')[:2] == [action, folder]]
    # Head insertions move existing folders across page boundaries. Walk only
    # buttons actually present in the live keyboard, then verify the share link.
    if not candidates:
        total = list_page(panel)[1]
        for nearby in (wanted+1, wanted-1):
            if not 1 <= nearby <= total:
                continue
            while list_page(panel)[0] != nearby:
                current = list_page(panel)[0]
                following = current + (1 if nearby > current else -1)
                navigation = [b for b in buttons(panel)
                              if b.data == f'folders_list_page/{following}'.encode()]
                if len(navigation) != 1:
                    raise MenuChangedError('来源目录缺少相邻页入口')
                panel = await catalog.click(panel, navigation[0].data,
                    lambda m: bool(list_page(m) and list_page(m)[0] == following))
            candidates = [b for b in buttons(panel) if b.data
                          and b.data.decode(errors='replace').split('/')[:2] == [action, folder]]
            if candidates:
                break
    if len(candidates) != 1:
        raise MenuChangedError('目录已更新，原文件夹不在记录页码')
    target = canonical(link, allowed)
    def matching_detail(message):
        parsed = share_link(message, allowed)
        return bool(parsed and parsed[1] == target)
    detail = await catalog.click(panel, candidates[0].data, matching_detail)
    views = [b for b in buttons(detail) if '查看内容' in b.text and b.data]
    if len(views) != 1:
        raise MenuChangedError('来源详情缺少查看内容入口')
    count = media_count(detail.raw_text or '')
    if count is not None:
        job['expected'] = count
        job['expected_refreshed'] = True
    title = folder_title(detail.raw_text or '')
    if title:
        store.set_title(job['id'], title)
    store.set_folder_metadata(job['id'], folder_metadata(detail.raw_text or ''))
    latest = await client.get_messages(peer, limit=1)
    # Exclude menu previews and cover photos, including already queued updates.
    job['baselines'][peer.id] = latest[0].id if latest else detail.id
    job['menu_recovery'] = True
    job['source_announced'] = True
    try:
        await asyncio.wait_for(views[0].click(), timeout=15)
    except Exception as error:
        if type(error).__name__ not in {'TimeoutError', 'BotResponseTimeoutError'}:
            raise
    return True
