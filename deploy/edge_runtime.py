"""Install/run the split importer in an existing private Supervisor runtime.

This helper changes no webhook and performs no source downloads by itself.
Run install only after stopping the old souga/collector consumers.
"""
import argparse
import asyncio
import configparser
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def paths(runtime):
    return runtime / 'supervisord.conf', REPO / '.venv/bin/supervisorctl'


def install(runtime):
    config_path, control = paths(runtime)
    for name in ('souga', 'collector'):
        status = subprocess.run([str(control), '-c', str(config_path), 'status', name],
                                capture_output=True, text=True)
        if 'RUNNING' in status.stdout or 'STARTING' in status.stdout:
            raise RuntimeError('Stop the old souga and collector before installing')
    database = runtime / 'bridge/data/index.sqlite3'
    db = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
    try:
        if db.execute("SELECT 1 FROM jobs WHERE state IN ('running','queued','cooldown') LIMIT 1").fetchone():
            raise RuntimeError('An active transfer must settle before migration')
    finally:
        db.close()
    secret_file = runtime / 'edge-secrets.json'
    if not secret_file.is_file():
        raise RuntimeError('Private edge connection settings are missing')
    config = configparser.ConfigParser(interpolation=None)
    config.read(config_path)
    backup = runtime / 'backups/edge-cutover'
    backup.mkdir(parents=True, exist_ok=True, mode=0o700)
    for source in (config_path, runtime / 'start.py'):
        target = backup / source.name
        if source.is_file() and not target.exists():
            shutil.copy2(source, target)
            target.chmod(0o600)
    config['program:souga']['command'] = f'{sys.executable} -u {REPO}/deploy/edge_runtime.py run --runtime {runtime}'
    config['program:souga']['directory'] = str(REPO)
    config['program:souga']['autostart'] = 'true'
    config['program:local-api']['autostart'] = 'false'
    with config_path.open('w') as handle:
        config.write(handle)
    config_path.chmod(0o600)
    # Codespace restarts keep the selected Supervisor configuration. The old
    # launcher is preserved privately instead of briefly starting a second poller.
    launcher = runtime / 'start.py'
    launcher.write_text('import os,sys\nos.execv(' + repr(str(REPO / '.venv/bin/python')) + ',['
                        + repr(str(REPO / '.venv/bin/python')) + ',' + repr(str(Path(__file__).resolve()))
                        + ',"boot","--runtime",' + repr(str(runtime)) + '])\n')
    launcher.chmod(0o600)
    print('Split runtime configuration installed; webhook is unchanged.')


def run(runtime):
    from dotenv import load_dotenv
    load_dotenv(REPO / '.env')
    private = json.loads((runtime / 'edge-secrets.json').read_text())
    for key in ('EDGE_URL', 'EDGE_SYNC_TOKEN', 'WEBHOOK_SECRET'):
        os.environ[key] = private[key]
    os.environ.update(EDGE_MODE='1', BOT_API_BASE='https://api.telegram.org',
                      UPLOAD_BACKEND='relay', DATA_DIR=str(runtime / 'bridge/data'),
                      BOT_TOKEN=(runtime / 'souga/.bot-token').read_text().strip())
    os.chdir(REPO)
    from edge_importer import serve
    asyncio.run(serve())


def boot(runtime):
    config, control = paths(runtime)
    status = subprocess.run([str(control), '-c', str(config), 'pid'], capture_output=True)
    if status.returncode:
        subprocess.run([str(REPO / '.venv/bin/supervisord'), '-c', str(config)], check=True)
    subprocess.run([str(control), '-c', str(config), 'status'], check=False)


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['install', 'run', 'boot'])
    parser.add_argument('--runtime', type=Path, required=True)
    args = parser.parse_args()
    if not args.runtime.is_absolute():
        parser.error('--runtime must be absolute')
    globals()[args.command](args.runtime)
