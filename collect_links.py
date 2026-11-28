"""Independent, resumable source-menu collector. Never polls the Bot API."""
import argparse
import asyncio
import contextlib
import csv
import json
import os
import sqlite3
import time
from pathlib import Path
from dotenv import dotenv_values
from telethon import TelegramClient
from telethon.crypto import AuthKey
from telethon.sessions import MemorySession
from collector_state import ACTIVE, Ledger, existing_jobs
from source_catalog import Catalog, MenuChangedError
from source_lock import source_lease
import automation_control as control


async def connect(config, root):
    # Copy only authentication into memory. Two processes must not write relay.session.
    with contextlib.closing(sqlite3.connect((root / 'relay.session').resolve().as_uri() + '?mode=ro', uri=True)) as db:
        row = db.execute('SELECT dc_id,server_address,port,auth_key FROM sessions').fetchone()
    if not row or not row[3]:
        raise RuntimeError('先使用 login.py 登录专用中转账号。')
    session = MemorySession()
    session.set_dc(*row[:3])
    session.auth_key = AuthKey(row[3])
    client = TelegramClient(session, int(config['API_ID']), config['API_HASH'],
                            receive_updates=False, flood_sleep_threshold=0)
    await client.connect()
    me = await client.get_me()
    owners = {int(x) for x in config['OWNER_IDS'].split(',') if x.strip()}
    if not me or me.bot or me.id != int(config['RELAY_USER_ID']) or me.id not in owners:
        await client.disconnect()
        raise RuntimeError('采集账号必须是 OWNER_IDS 中的 RELAY_USER_ID。')
    return client, me.id


def safe_to_dispatch(index, rows, root):
    if control.paused(root,'transfers') or any(r['state'] in ACTIVE or r.get('restart_pending') for r in rows):
        return False
    with contextlib.closing(sqlite3.connect(index.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        if db.execute("SELECT 1 FROM local_send_intents WHERE state IN ('pending','unknown') LIMIT 1").fetchone():
            return False
    cooldown = root / 'rate-limit.json'
    if cooldown.exists():
        # The bridge handles actual cooldown when a task is queued.
        if float(json.loads(cooldown.read_text())['until']) > time.time():
            return False
    return True


async def dispatch(client, target, ledger, index, owner, allowed, root):
    rows, jobs = existing_jobs(index, owner, allowed)
    ledger.reconcile(jobs)
    if not safe_to_dispatch(index, rows, root):
        return False
    # Unknown command delivery must be reconciled, never blindly repeated.
    if ledger.db.execute("SELECT 1 FROM items WHERE state='dispatching' LIMIT 1").fetchone():
        return False
    for item in ledger.pending():
        job = jobs.get(item['link'])
        refresh = ledger.refresh_target(item, job)
        if job and job['state'] == 'complete':
            if refresh:
                command = f"/resume {job['id']} refresh {refresh}"
            else:
                continue
        elif item['state'] != 'ready':
            continue
        elif item['attempts'] >= 2:
            ledger.set_state(item, 'review', job['id'] if job else None)
            continue
        else:
            command = '/resume ' + job['id'] if job else '/fetch ' + item['link']
        # Durable before network I/O. A crash here intentionally needs reconciliation.
        ledger.set_state(item, 'dispatching', job['id'] if job else None, attempt=True, refresh_count=refresh)
        await client.send_message(target, command)
        # Wait for real job creation/state change; a sent command alone is not acceptance.
        for _ in range(30):
            await asyncio.sleep(1)
            _, current = existing_jobs(index, owner, allowed)
            accepted = current.get(item['link'])
            if not accepted:
                continue
            if refresh:
                confirmed = (accepted['id'] == job['id'] and
                             (accepted.get('refresh_expected') == refresh or
                              (accepted['state'] == 'complete' and (accepted.get('expected') or 0) >= refresh)))
            else:
                confirmed = not job or accepted['state'] in ACTIVE | {'complete'}
            if confirmed:
                ledger.set_state(item, 'submitted', accepted['id'])
                print(f"已提交：{item['title']}，任务 {accepted['id']}", flush=True)
                return True
        print('提交结果未确认，已停止自动提交；检查 status 和机器人回复。', flush=True)
        return False
    return False


async def operate(args, config, root, ledger):
    allowed = {x.strip().lower() for x in config.get('ALLOWED_SOURCE_BOTS', 'epan,SPXM111_bot').split(',')}
    if args.source.lower() not in allowed:
        raise ValueError('来源机器人不在 ALLOWED_SOURCE_BOTS 中。')
    # A second collector must not run against the same account/ledger concurrently.
    async with source_lease(root / 'collector-process.lock'):
        client, owner = await connect(config, root)
        try:
            catalog = Catalog(client, args.source, allowed,
                              should_pause=lambda: control.paused(root,'collector'))
            if args.command == 'scan':
                for _ in range(args.pages):
                    async with source_lease(root / 'source-control.lock'):
                        if not await catalog.scan_page(ledger):
                            break
                _, jobs = existing_jobs(root / 'index.sqlite3', owner, allowed)
                ledger.reconcile(jobs)
                return
            target = await client.get_entity(args.target_bot)
            if not getattr(target, 'bot', False) or target.username.lower() in allowed:
                raise ValueError('目标必须是自己的转存机器人，不能是来源机器人。')
            while True:
                try:
                    control.write(root, {'at': time.time(), 'pid': os.getpid()}, name='collector-heartbeat.json')
                    rows, jobs = existing_jobs(root / 'index.sqlite3', owner, allowed)
                    ledger.reconcile(jobs)
                    if safe_to_dispatch(root / 'index.sqlite3', rows, root):
                        if not control.paused(root,'collector') and ledger.head_due(args.source):
                            async with source_lease(root / 'source-control.lock'):
                                if await catalog.scan_head(ledger):
                                    await catalog.revisit(ledger)
                            _, jobs = existing_jobs(root / 'index.sqlite3', owner, allowed)
                            ledger.reconcile(jobs)
                        ready = ledger.db.execute("SELECT 1 FROM items WHERE state='ready' LIMIT 1").fetchone()
                        if not ready and not control.paused(root,'collector'):
                            async with source_lease(root / 'source-control.lock'):
                                await catalog.scan_page(ledger)
                            _, jobs = existing_jobs(root / 'index.sqlite3', owner, allowed)
                            ledger.reconcile(jobs)
                        await dispatch(client, target, ledger, root / 'index.sqlite3', owner, allowed, root)
                    await asyncio.sleep(10)
                except Exception as error:
                    if isinstance(error, MenuChangedError):
                        print('来源菜单未确认，保留采集断点，60 秒后再检查。', flush=True)
                        await asyncio.sleep(60)
                        continue
                    if type(error).__name__ == 'FloodWaitError':
                        delay = max(1, int(error.seconds))
                        print(f'来源要求等待 {delay} 秒；保存进度后等待。', flush=True)
                        await asyncio.sleep(delay)
                        continue
                    raise
        finally:
            await client.disconnect()


def main():
    parser = argparse.ArgumentParser(description='逐个点击来源序号，采集详情分享链接，去重后顺序导入。')
    parser.add_argument('command', choices=['scan', 'run', 'status', 'export'])
    parser.add_argument('--env', type=Path, default=Path(__file__).parent / '.env')
    parser.add_argument('--source', default='SPXM111_bot')
    parser.add_argument('--target-bot', default='sougabot')
    parser.add_argument('--pages', type=int, default=1, help='scan 的页数；每页通常 10 个序号')
    parser.add_argument('--output', type=Path, default=Path('collected-links.csv'))
    args = parser.parse_args()
    config = dotenv_values(args.env)
    root = Path(config.get('DATA_DIR', 'data')).resolve()
    if not (root / 'index.sqlite3').exists() or not 1 <= args.pages <= 2000:
        parser.error('找不到转存数据库，或 pages 不在 1–2000 范围。')
    ledger = Ledger(root / 'collector.sqlite3')
    try:
        if args.command == 'status':
            print('扫描进度：', ledger.page(args.source))
            print('链接状态：', dict(ledger.db.execute('SELECT state,COUNT(*) FROM items GROUP BY state')))
            for row in ledger.db.execute("SELECT title,state,job FROM items WHERE state IN ('review','dispatching')"):
                print(tuple(row))
        elif args.command == 'export':
            with args.output.open('w', newline='', encoding='utf-8-sig') as handle:
                writer = csv.writer(handle)
                writer.writerow(['名称', '链接', '状态', '任务'])
                writer.writerows(ledger.db.execute('SELECT title,link,state,job FROM items ORDER BY rowid'))
            args.output.chmod(0o600)
        else:
            asyncio.run(operate(args, config, root, ledger))
    finally:
        ledger.db.close()


if __name__ == '__main__':
    main()
