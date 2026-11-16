"""Optional standalone entry point. Integrate Bridge into an existing bot instead
of starting a second polling process for the same token.
"""
import os
from dotenv import load_dotenv
from telegram.ext import Application
from bridge import Bridge


def main():
    load_dotenv()
    os.umask(0o077)
    bridge = Bridge()
    app = (Application.builder().token(os.environ['BOT_TOKEN'])
           .post_init(bridge.start).post_stop(bridge.stop).build())
    bridge.install(app)
    app.run_polling(drop_pending_updates=False)


if __name__ == '__main__':
    main()
