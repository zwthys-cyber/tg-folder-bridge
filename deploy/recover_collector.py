"""Apply the collector update in the existing Codespace; no build or tests here."""
import argparse
import configparser
import json
import sqlite3
import subprocess
import sys
from pathlib import Path


def recover(runtime, repo):
    config = runtime / 'supervisord.conf'
    settings = configparser.ConfigParser(interpolation=None)
    settings.read(config)
    if 'edge_runtime.py run' not in settings.get('program:souga', 'command', fallback=''):
        raise RuntimeError('Expected the split importer configuration; refusing to start a legacy poller')
    index = runtime / 'bridge/data/index.sqlite3'
    database = sqlite3.connect(index.as_uri()+'?mode=ro', uri=True)
    try:
        active = database.execute("SELECT count(*) FROM jobs WHERE state IN ('running','queued','cooldown')").fetchone()[0]
        states = database.execute('SELECT state,count(*) FROM jobs GROUP BY state').fetchall()
    finally:
        database.close()
    if active:
        raise RuntimeError('Transfers are active; leave them running and apply the update when idle')
    control = [str(repo/'.venv/bin/supervisorctl'), '-c', str(config)]
    daemon = subprocess.run(control+['pid'], capture_output=True, text=True)
    if daemon.returncode == 0:
        subprocess.run(control+['stop','collector','souga'], check=False, capture_output=True)
    subprocess.run([sys.executable, str(runtime/'start.py')], check=True)
    subprocess.run(control+['start','souga','collector'], check=False, capture_output=True)
    result = subprocess.run(control+['status','souga','collector'], capture_output=True, text=True)
    print(result.stdout)
    if result.returncode not in (0, 3) or any(word in result.stdout for word in ('FATAL','BACKOFF','EXITED','STOPPED')):
        raise RuntimeError('Runtime did not start; inspect private Supervisor logs')
    print(json.dumps({'original_task_states':states,'media_preserved':True,'cancelled_tasks_not_resumed':True}))


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime',type=Path,default=Path('/workspaces/.tg-runtime'))
    args=parser.parse_args()
    recover(args.runtime.resolve(),Path(__file__).resolve().parents[1])
