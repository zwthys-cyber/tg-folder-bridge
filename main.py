"""Optional standalone entry point. Integrate Bridge into an existing bot instead
of starting a second polling process for the same token.
"""
import os
from dotenv import load_dotenv
from telegram.ext import Application
from bridge import Bridge
from bot_api_config import configure


def main():
    load_dotenv()
    if os.getenv('EDGE_MODE') == '1':
        raise RuntimeError('EDGE_MODE requires edge_importer.py; polling must stay disabled')
    os.umask(0o077)
    bridge = Bridge()
    app = (configure(Application.builder().token(os.environ['BOT_TOKEN']))
           .post_init(bridge.start).post_stop(bridge.stop).build())
    bridge.install(app)
    app.run_polling(drop_pending_updates=False)


if __name__ == '__main__':
    main()
