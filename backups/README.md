# 加密恢复备份

最新恢复快照：`recovery-20261123T143537Z.tgz.gpg`，生成于 2026-11-23 14:35 UTC，对应程序版本 `2d36273`。另保留 11 月 22 日的元数据快照。

恢复备份包含转存索引、采集账本、收藏、订阅、任务与补收状态、机器人配置、登录会话、原搜索资源库，以及 Codespaces 的启动脚本、Supervisor 配置和 Linux x86_64 本地 Bot API 可执行文件。照片、视频、下载缓存和日志不在备份内。照片与视频由 Telegram 保存，备份中的文件索引用于重新取回这些资源。

备份使用 GnuPG AES-256 对称加密，解密口令文件由管理员独立保存。解密后的文件包含敏感配置与登录会话，应保存在私有目录中。

下载 `.tgz.gpg` 文件和 `SHA256SUMS` 后，可检查文件完整性。

```bash
sha256sum -c SHA256SUMS
gpg --batch --pinentry-mode loopback --passphrase-file /path/to/private/key.txt --output /path/to/private/recovery.tgz --decrypt recovery-20261123T143537Z.tgz.gpg
```

恢复时先停止旧实例，避免两处同时轮询机器人或使用中转会话。在私有目录解压，目录权限设为 0700，敏感文件设为 0600；不要把解密内容放进公开仓库。

快照中的 `runtime/` 对应 `/workspaces/.tg-runtime/`。`metadata/` 中的数据库、`relay.session` 和状态 JSON 对应该目录下的 `bridge/data/`；`bridge.env` 对应项目根目录的 `.env`。`sougabot-bot.py`、`sougabot-bot-token`、`sougabot-admin-id` 和 `resources.db` 分别对应 `souga/bot.py`、`souga/.bot-token`、`souga/.admin-id` 和 `souga/resources.db`。

检出对应程序版本后建立 Python 虚拟环境，安装 `requirements.txt` 和 `supervisor==4.3.0`，准备 ffmpeg/ffprobe，恢复私有文件，再运行 `/workspaces/.tg-runtime/start.py`。快照环境为 Python 3.13.8、Linux x86_64。编译与测试通过 GitHub Actions 执行。

此快照记录生成时的状态。后续补收进度需要通过更新快照保存。
