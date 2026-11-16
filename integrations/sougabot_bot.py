"""Sougabot integration based on the supplied bot.py; deploy as bot.py.

Keeps the existing .bot-token, .admin-id and resources.db in the same directory.
"""
import json
import os
import secrets
import signal
import sqlite3
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

BASE = Path(__file__).resolve().parent
TOKEN = (BASE / '.bot-token').read_text().strip()
ADMIN = BASE / '.admin-id'
BIND = secrets.token_hex(16)
db = sqlite3.connect(BASE / 'resources.db')
db.execute(
    'CREATE TABLE IF NOT EXISTS resources '
    '(id INTEGER PRIMARY KEY, title TEXT, link TEXT UNIQUE, tags TEXT)'
)
db.commit()
sessions = {}


def api(method, **data):
    request = Request(
        'https://api.telegram.org/bot' + TOKEN + '/' + method,
        data=json.dumps(data).encode(),
        headers={'Content-Type': 'application/json'}
    )
    try:
        with urlopen(request, timeout=40) as response:
            result = json.load(response)
    except HTTPError as error:
        raise RuntimeError('Telegram HTTP ' + str(error.code)) from None
    if not result.get('ok'):
        raise RuntimeError('Telegram API request failed')
    return result['result']


def send(chat, text, **extra):
    return api('sendMessage', chat_id=chat, text=text, **extra)


def page(chat, uid, key, offset=0):
    session = sessions.get(key)
    if not session or session[0] != uid:
        return send(chat, '搜索已过期，请重新输入关键词。')
    query = session[1]
    rows = db.execute(
        'SELECT id,title,link FROM resources '
        'WHERE instr(lower(title || " " || tags), lower(?)) > 0 '
        'ORDER BY id DESC LIMIT 6 OFFSET ?',
        (query, offset)
    ).fetchall()
    if not rows:
        return send(chat, '没有找到相关资源，请换一个关键词。')
    text = '\n\n'.join(
        f'{rid}. {title}\n{link}' for rid, title, link in rows[:5]
    )
    buttons = []
    if offset:
        buttons.append({
            'text': '上一页',
            'callback_data': f'{key}:{max(0, offset-5)}'
        })
    if len(rows) > 5:
        buttons.append({
            'text': '下一页',
            'callback_data': f'{key}:{offset+5}'
        })
    send(
        chat,
        f'搜索：{query}\n第 {offset//5+1} 页\n\n{text}',
        reply_markup={'inline_keyboard': [buttons] if buttons else []},
        link_preview_options={'is_disabled': True}
    )


def handle(update):
    if 'callback_query' in update:
        c = update['callback_query']
        api('answerCallbackQuery', callback_query_id=c['id'])
        key, offset = c['data'].split(':')
        return page(
            c['message']['chat']['id'],
            c['from']['id'],
            key,
            max(0, int(offset))
        )
    m = update.get('message', {})
    if m.get('chat', {}).get('type') != 'private' or 'text' not in m:
        return
    chat = m['chat']['id']
    uid = m['from']['id']
    text = m['text'].strip()
    command = text.split(maxsplit=1)[0].split('@')[0] if text else ''

    if command in ('/start', '/help'):
        return send(
            chat,
            '欢迎使用 Souga 搜索！直接发送关键词即可搜索。\n'
            '管理员收录：\n'
            '/add 标题 | https://t.me/用户名 | 关键词\n'
            '删除：/delete 编号\n\n'
            '文件夹转存（仅授权用户）：\n'
            '/fetch 分享链接\n'
            '/folders 查看最近任务\n'
            '/folder 任务编号 [页码] 取回文件'
        )

    if command == '/bind':
        if ADMIN.exists():
            return send(chat, '管理员已经绑定。')
        if text != '/bind ' + BIND:
            return send(chat, '绑定码不正确。')
        ADMIN.write_text(str(uid))
        ADMIN.chmod(0o600)
        return send(chat, '管理员绑定成功！可以使用 /add 收录资源了。')

    if command in ('/add', '/delete'):
        if not ADMIN.exists() or ADMIN.read_text().strip() != str(uid):
            return send(chat, '只有管理员可以执行这个操作。')
        argument = text.partition(' ')[2].strip()
        if command == '/add':
            parts = [p.strip() for p in argument.split('|')]
            if (
                len(parts) != 3
                or not parts[0]
                or not parts[1].startswith('https://t.me/')
                or len(parts[0]) > 100
                or len(parts[1]) > 250
                or len(parts[2]) > 300
                or any(c.isspace() for c in parts[1])
            ):
                return send(
                    chat,
                    '格式：/add 标题 | https://t.me/用户名 | 关键词\n'
                    '标题最多100字，关键词最多300字。'
                )
            try:
                cursor = db.execute(
                    'INSERT INTO resources(title,link,tags) VALUES(?,?,?)',
                    parts
                )
                db.commit()
            except sqlite3.IntegrityError:
                return send(chat, '这个链接已经收录。')
            return send(chat, f'收录成功，编号：{cursor.lastrowid}')
        if not argument.isdigit():
            return send(chat, '格式：/delete 编号')
        cursor = db.execute('DELETE FROM resources WHERE id=?', (argument,))
        db.commit()
        return send(chat, '已删除。' if cursor.rowcount else '没有这个编号。')

    # Reached only if the bridge could not start; ordinary search still works.
    if command in ('/fetch', '/folder', '/folders') or (
        text.startswith('https://t.me/') and '?start=' in text
    ):
        return send(chat, '文件转存模块未启动，请检查 sougabot 服务日志和转存配置。')
    if not text or len(text) > 100:
        return send(chat, '请输入不超过100字的关键词。')
    if text.startswith('/'):
        return send(chat, '未知命令，请发送 /help。')
    if len(sessions) >= 1000:
        sessions.pop(next(iter(sessions)))
    key = secrets.token_hex(8)
    sessions[key] = (uid, text)
    page(chat, uid, key)


def start_bridge():
    if os.getenv('SOUGA_BRIDGE_ENABLED', '1') == '0':
        print('文件转存模块已禁用，搜索功能继续运行。', flush=True)
        return None
    root = Path(os.getenv('SOUGA_BRIDGE_DIR', '/root/tg-folder-bridge')).resolve()
    sys.path.insert(0, str(root))
    try:
        from souga_adapter import SougaBridge
        bridge = SougaBridge(bot_token=TOKEN, env_file=root / '.env').start()
    except Exception as error:
        print('文件转存模块启动失败：' + type(error).__name__ +
              '；请检查转存目录、.env 和登录会话。搜索功能继续运行。', flush=True)
        return None
    print('文件转存模块已连接，沿用原 getUpdates 接收消息。', flush=True)
    return bridge


def poll_once(offset, folder_bridge):
    updates = api(
        'getUpdates', offset=offset, timeout=25,
        allowed_updates=['message', 'callback_query']
    )
    for update in updates:
        try:
            if folder_bridge is None or not folder_bridge.handle_update(update):
                handle(update)
        except Exception as error:
            print('处理消息失败：' + type(error).__name__, flush=True)
        # Both bridge and existing handlers reach this line: no early continue.
        offset = update['update_id'] + 1
    return offset


def stop_signal(signum, frame):
    raise KeyboardInterrupt


def main():
    me = api('getMe')
    print('机器人已连接：@' + me['username'], flush=True)
    if not ADMIN.exists():
        print('请在 Telegram 私聊自己的机器人，发送以下命令绑定管理员：', flush=True)
        print('/bind ' + BIND, flush=True)
    signal.signal(signal.SIGTERM, stop_signal)
    folder_bridge = start_bridge()
    offset = 0
    try:
        while True:
            try:
                offset = poll_once(offset, folder_bridge)
            except Exception as error:
                print('连接暂时失败：' + type(error).__name__ + '；5秒后重试', flush=True)
                time.sleep(5)
    except KeyboardInterrupt:
        print('机器人正在停止。', flush=True)
    finally:
        try:
            if folder_bridge is not None:
                folder_bridge.close()
        finally:
            db.close()


if __name__ == '__main__':
    main()
