"""Operator-only A/B upload benchmark. Uploads temporary parts, sends no messages.

Run on the deployment host with the bridge stopped. Never run in GitHub CI.
"""
import argparse
import asyncio
import fcntl
import json
import os
import shutil
import sqlite3
import statistics
import tempfile
from pathlib import Path
from dotenv import dotenv_values
from telethon import TelegramClient
from telethon.crypto import AuthKey
from telethon.sessions import MemorySession
from fast_upload import upload_file
from transport_recovery import Cooldown, error_detail, rate_wait
from upload_transport import UploadTransport


async def benchmark(root, config, size, connections, rounds):
    guard = Cooldown(root/'rate-limit.json')
    if guard.remaining():
        raise RuntimeError(f'Rate-limit cooldown active: {guard.remaining():.0f} seconds')
    with sqlite3.connect(f'file:{root / "relay.session"}?mode=ro', uri=True) as db:
        row = db.execute('SELECT dc_id,server_address,port,auth_key FROM sessions').fetchone()
    if not row or not row[3]:
        raise RuntimeError('Relay login session is missing')
    memory = MemorySession()
    memory.set_dc(*row[:3])
    memory.auth_key = AuthKey(row[3])
    client = TelegramClient(memory, int(config['API_ID']), config['API_HASH'],
        receive_updates=False, flood_sleep_threshold=0, raise_last_call_error=True)
    pool = UploadTransport(client, connections)
    results = []
    try:
        await client.connect()
        me = await client.get_me()
        if not me or me.bot or me.id != int(config['RELAY_USER_ID']):
            raise RuntimeError('Relay account does not match configuration')
        required = size*1024**2
        if shutil.disk_usage(root).free < required + 10*1024**3:
            raise RuntimeError('Insufficient disk reserve for benchmark')
        with tempfile.TemporaryDirectory(prefix='benchmark-', dir=root) as temporary:
            path = Path(temporary)/'sample.bin'
            with path.open('wb') as output:
                for _ in range(size):
                    output.write(os.urandom(1024**2))
            for repeat in range(rounds):
                # Reverse the order to reduce simple warm-up/time-order bias.
                order = (1, connections) if repeat % 2 == 0 else (connections, 1)
                for count in order:
                    stats = {}
                    await upload_file(client, path, lambda *_: None, workers=8,
                                      stats=stats, transport=pool if count > 1 else None)
                    result = dict(round=repeat+1, **stats)
                    results.append(result)
                    print(json.dumps(result), flush=True)
            base = statistics.median(x['average_mib'] for x in results if x['connections'] == 1)
            candidate = statistics.median(x['average_mib'] for x in results if x['connections'] == connections)
            summary = dict(baseline_mib=base, candidate_mib=candidate,
                           gain_percent=100*(candidate/base-1), connections=connections,
                           size_mib=size, rounds=rounds)
            report = root/'upload-benchmark.json'
            # Atomic private report; never contains API credentials or session data.
            with tempfile.NamedTemporaryFile(mode='w', dir=root, delete=False) as output:
                json.dump(dict(summary=summary, results=results), output)
                temporary_report = Path(output.name)
            os.replace(temporary_report, report)
            print(json.dumps(summary), flush=True)
    except Exception as exc:
        wait = rate_wait(exc, 0)
        if wait is not None:
            guard.defer(wait, error_detail(exc))
        raise
    finally:
        await pool.close()
        await client.disconnect()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env', default=str(Path(__file__).with_name('.env')))
    parser.add_argument('--size-mib', type=int, default=128)
    parser.add_argument('--connections', type=int, default=2)
    parser.add_argument('--rounds', type=int, default=2)
    args = parser.parse_args()
    if not 16 <= args.size_mib <= 512 or not 2 <= args.connections <= 4 or not 1 <= args.rounds <= 3:
        parser.error('size:16–512 MiB; connections:2–4; rounds:1–3')
    os.umask(0o077)
    config = dotenv_values(args.env)
    root = Path(config.get('DATA_DIR') or 'data').resolve()
    # The running bridge holds this lock, preventing simultaneous load or
    # conflicting session access. Never delete spool files during a benchmark.
    with (root/'spool'/'.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.exit(2, 'Stop the bridge before benchmarking.\n')
        try:
            asyncio.run(benchmark(root, config, args.size_mib, args.connections, args.rounds))
        except Exception as exc:
            parser.exit(1, error_detail(exc)+'\n')


if __name__ == '__main__':
    main()
