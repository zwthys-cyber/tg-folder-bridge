"""Run interactively only on the eventual deployment server."""
import asyncio
import os
from pathlib import Path
from dotenv import load_dotenv
from telethon import TelegramClient


async def main():
    load_dotenv()
    os.umask(0o077)
    root = Path(os.getenv('DATA_DIR', 'data'))
    root.mkdir(parents=True, exist_ok=True)
    async with TelegramClient(str(root / 'relay'), int(os.environ['API_ID']),
                              os.environ['API_HASH']) as client:
        me = await client.get_me()
        if me.bot:
            raise RuntimeError('请登录 Telegram 用户账号，而不是 Bot Token')
        print(f'登录成功。请将 RELAY_USER_ID 配置为 {me.id}')


if __name__ == '__main__':
    asyncio.run(main())
