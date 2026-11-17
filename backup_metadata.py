"""Private online snapshots of metadata/configuration; excludes media spools."""
import argparse
import contextlib
import datetime
import json
import shutil
import sqlite3
from pathlib import Path
from dotenv import dotenv_values
from source_lock import source_lease
import asyncio


def snapshot(source, target):
    with contextlib.closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        with contextlib.closing(sqlite3.connect(target)) as output:
            db.backup(output)
    target.chmod(0o600)


def backup(project, destination, bot_dir=None, keep=7):
    project, destination = Path(project).resolve(), Path(destination).resolve()
    config = dotenv_values(project / '.env')
    data = Path(config.get('DATA_DIR', 'data'))
    if not data.is_absolute():
        data = project / data
    if not (data / 'index.sqlite3').exists():
        raise RuntimeError('Missing bridge index')
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.stat().st_mode & 0o077:
        raise PermissionError('Backup directory must be private (0700)')
    name = 'metadata-' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    staging = destination / ('.pending-' + name)
    final = destination / name
    staging.mkdir(mode=0o700)
    try:
        for relative in ('index.sqlite3', 'collector.sqlite3', 'relay.session'):
            source = data / relative
            if source.exists():
                snapshot(source, staging / relative)
        for relative in ('performance.json', 'automation.json', 'rate-limit.json'):
            source = data / relative
            if source.exists():
                shutil.copyfile(source, staging / relative)
                (staging / relative).chmod(0o600)
        if (project / '.env').exists():
            shutil.copyfile(project / '.env', staging / 'bridge.env')
            (staging / 'bridge.env').chmod(0o600)
        if bot_dir:
            bot_dir = Path(bot_dir)
            for relative in ('.bot-token', '.admin-id', 'bot.py'):
                source = bot_dir / relative
                if source.exists():
                    target = staging / ('sougabot-' + relative.lstrip('.'))
                    shutil.copyfile(source, target)
                    target.chmod(0o600)
            if (bot_dir / 'resources.db').exists():
                snapshot(bot_dir / 'resources.db', staging / 'resources.db')
        manifest = {'format': 1, 'created_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    'files': {p.name: p.stat().st_size for p in staging.iterdir()}}
        (staging / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False))
        (staging / 'manifest.json').chmod(0o600)
        staging.rename(final)
    except BaseException:
        shutil.rmtree(staging)
        raise
    # Only retire snapshots produced by this tool; never touch manual backups.
    snapshots = sorted(p for p in destination.glob('metadata-*') if p.is_dir() and not p.is_symlink()
                       and (p / 'manifest.json').is_file())
    for old in snapshots[:-keep]:
        shutil.rmtree(old)
    return final


async def main_async(args):
    async with source_lease(args.destination / 'backup.lock'):
        print(backup(args.project, args.destination, args.bot_dir, args.keep), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path(__file__).parent)
    parser.add_argument('--destination', type=Path, default=Path('/root/sougabot-backups/scheduled'))
    parser.add_argument('--bot-dir', type=Path)
    parser.add_argument('--keep', type=int, default=7)
    args = parser.parse_args()
    if args.keep < 1:
        parser.error('keep must be positive')
    # Create private parent before the lock helper creates any directory.
    args.destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    asyncio.run(main_async(args))


if __name__ == '__main__':
    main()
