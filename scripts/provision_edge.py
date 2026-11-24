"""Create/reuse named Cloudflare resources in GitHub Actions; no token output."""
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path


def api(path, body=None):
    account = os.environ['CLOUDFLARE_ACCOUNT_ID']
    if not account.isalnum():
        raise ValueError('Invalid account ID')
    request = urllib.request.Request(
        'https://api.cloudflare.com/client/v4/accounts/' + account + '/' + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Authorization': 'Bearer ' + os.environ['CLOUDFLARE_API_TOKEN'],
                 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=60) as response:
        result = json.load(response)
    if not result.get('success'):
        raise RuntimeError('Cloudflare API rejected resource provisioning')
    return result['result']


def main():
    database = next((x for x in api('d1/database?per_page=100') if x['name'] == 'sougabot-catalog'), None)
    if database is None:
        database = api('d1/database', {'name': 'sougabot-catalog'})
    queues = api('queues?per_page=100')
    queue = next((x for x in queues if x['queue_name'] == 'sougabot-work'), None)
    if queue is None:
        queue = api('queues', {'queue_name': 'sougabot-work'})
    ui_queue = next((x for x in queues if x['queue_name'] == 'sougabot-ui'), None)
    if ui_queue is None:
        ui_queue = api('queues', {'queue_name': 'sougabot-ui'})
    # IDs are configuration, not credentials. The artifact contains no secrets.
    Path('edge-resources.json').write_text(json.dumps({
        'database_id': database['uuid'], 'queue_name': queue['queue_name'], 'ui_queue_name': ui_queue['queue_name']}, indent=2)+'\n')
    print('Cloudflare database and queue ready; IDs written to artifact.')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        raise SystemExit('Provisioning failed: ' + type(error).__name__) from None
