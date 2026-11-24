"""Optional transfer-only runner. The Worker is the sole Telegram update receiver."""
import asyncio
import contextlib
import os
import signal
from pathlib import Path
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application
from bot_api_config import CLOUD, endpoint
from edge_sync import EdgeClient, snapshot, fingerprint


def validate():
    if os.getenv('EDGE_MODE') != '1':
        raise ValueError('Set EDGE_MODE=1 explicitly for the transfer-only runner')
    if endpoint() != CLOUD or os.getenv('UPLOAD_BACKEND', 'relay') != 'relay':
        raise ValueError('Edge importer requires cloud Bot API and UPLOAD_BACKEND=relay')


async def inbox(client, app):
    while True:
        try:
            result = await asyncio.to_thread(client.call, 'inbox/claim')
            for raw in result['updates']:
                # Markers are processed even while Bridge.work awaits a receipt.
                # Commands only enqueue work; they do not wait for media transfer.
                store = app.bot_data['edge_store']
                previous = store.db.execute('SELECT state FROM edge_updates WHERE id=?', (raw['update_id'],)).fetchone()
                if previous is None:
                    with store.db:
                        store.db.execute("INSERT INTO edge_updates VALUES(?,'processing')", (raw['update_id'],))
                    app.bot_data['edge_error'] = False
                    await app.process_update(Update.de_json(raw, app.bot))
                    with store.db:
                        store.db.execute('UPDATE edge_updates SET state=? WHERE id=?',
                            ('uncertain' if app.bot_data['edge_error'] else 'done', raw['update_id']))
                # Unknown processing is retained locally for inspection, never
                # blindly repeated as a potentially accepted transfer command.
                await asyncio.to_thread(client.call, 'inbox/ack', {'ids': [raw['update_id']]})
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Do not print URLs/response bodies containing private credentials.
            print('Edge inbox temporarily unavailable: ' + type(error).__name__, flush=True)
        await asyncio.sleep(2)


async def sync(client, database, bot_id):
    last = ''
    while True:
        try:
            data = await asyncio.to_thread(snapshot, database)
            current = fingerprint(data)
            if current != last:
                await asyncio.to_thread(client.publish, data, bot_id)
                last = current
                print(f"Edge catalog synced: {len(data['folders'])} folders", flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print('Edge sync postponed: ' + type(error).__name__, flush=True)
        await asyncio.sleep(3600)


async def serve():
    validate()
    client = EdgeClient()
    status = await asyncio.to_thread(client.call, 'status')
    if not status.get('active'):
        raise RuntimeError('Seed the edge catalog and user preferences before starting the importer')
    # Guard the source session against accidentally starting another importer.
    from source_lock import source_lease
    root = Path(os.getenv('DATA_DIR', 'data'))
    async with source_lease(root / 'edge-importer-process.lock'):
        from bridge import Bridge
        bridge = Bridge()
        app = Application.builder().token(os.environ['BOT_TOKEN']).updater(None).build()
        bridge.install(app)
        bridge.store.db.execute('CREATE TABLE IF NOT EXISTS edge_updates(id INTEGER PRIMARY KEY,state TEXT NOT NULL)')
        bridge.store.db.execute("UPDATE edge_updates SET state='uncertain' WHERE state='processing'")
        bridge.store.db.commit()
        app.bot_data['edge_store'] = bridge.store
        async def record_error(update, context):
            app.bot_data['edge_error'] = True
            print('Edge update requires inspection: ' + type(context.error).__name__, flush=True)
        app.add_error_handler(record_error)
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stopped.set)
        async with app:
            await app.start()
            await bridge.start(app)
            tasks = [asyncio.create_task(inbox(client, app)),
                     asyncio.create_task(sync(client, root / 'index.sqlite3', (await app.bot.get_me()).id))]
            try:
                await stopped.wait()
            finally:
                for task in tasks:
                    task.cancel()
                for task in tasks:
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                await bridge.stop(app)
                await app.stop()


if __name__ == '__main__':
    load_dotenv()
    os.umask(0o077)
    asyncio.run(serve())
