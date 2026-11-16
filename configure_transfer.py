"""Enable bounded download/upload in a deployment .env; never print secrets."""
import argparse
import os
import re
import tempfile
from pathlib import Path


def configure(path):
    path = Path(path).resolve()
    text = path.read_text()
    values = {
        'TRANSFER_MODE': 'download_upload',
        'MAX_MEDIA_FILE_GB': '4',
        'MIN_FREE_DISK_GB': '10',
        'JOB_TIMEOUT_SECONDS': '86400',
        'DISPLAY_MODE': 'document_group',
    }
    lines = []
    for line in text.splitlines():
        match = re.match(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=', line)
        if not match or match[1] not in values:
            lines.append(line)
    lines.extend(f'{key}={value}' for key, value in values.items())
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            os.fchmod(output.fileno(), 0o600)
            output.write('\n'.join(lines) + '\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env', default=str(Path(__file__).resolve().parent / '.env'))
    args = parser.parse_args()
    configure(args.env)
    print('已启用逐文件下载上传；单文件上限 4 GiB，保留 10 GiB 空闲，任务超时 24 小时。')
    print('账号配置保持原样；重启 sougabot 后生效。')
