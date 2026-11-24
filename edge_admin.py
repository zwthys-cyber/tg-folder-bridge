"""Explicit Telegram cutover helper. Secrets are read from the environment only."""
import argparse
import json
import os
import urllib.request


def call(method, body):
    request = urllib.request.Request(
        'https://api.telegram.org/bot' + os.environ['BOT_TOKEN'] + '/' + method,
        data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=45) as response:
        result = json.load(response)
    if not result.get('ok'):
        raise RuntimeError('Telegram rejected operation')
    return result['result']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['identity', 'webhook-info', 'set-webhook', 'delete-webhook', 'set-menu'])
    args = parser.parse_args()
    if args.command == 'identity':
        me = call('getMe', {})
        print(json.dumps({'id': me['id'], 'username': me.get('username')}, ensure_ascii=False))
    elif args.command == 'webhook-info':
        value = call('getWebhookInfo', {})
        print(json.dumps({key: value.get(key) for key in
            ('url', 'pending_update_count', 'last_error_date', 'last_error_message')}, ensure_ascii=False))
    elif args.command == 'set-webhook':
        base = os.environ['EDGE_URL'].rstrip('/')
        if not base.startswith('https://'):
            raise ValueError('EDGE_URL must use HTTPS')
        call('setWebhook', dict(url=base+'/webhook', secret_token=os.environ['WEBHOOK_SECRET'],
            allowed_updates=['message', 'callback_query'], max_connections=2, drop_pending_updates=False))
        print('Webhook configured; pending updates preserved.')
    elif args.command == 'delete-webhook':
        call('deleteWebhook', {'drop_pending_updates': False})
        print('Webhook removed; pending updates preserved.')
    else:
        call('setMyCommands', {'commands': [dict(command=command, description=description) for command, description in
            [('start', '浏览文件夹'), ('favorites', '我的收藏'), ('notifications', '新文件夹通知'), ('help', '使用帮助')]]})
        print('Public command menu configured.')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        raise SystemExit('Operation failed: ' + type(error).__name__) from None
