# 加密元数据备份

当前快照生成于 2026-11-22 13:02 UTC。

备份包含转存索引、采集账本、收藏、订阅、任务状态、机器人配置、登录会话和原搜索资源库。照片与视频由 Telegram 保存，备份中的文件索引用于重新取回这些资源。

备份使用 GnuPG AES-256 对称加密，解密口令文件由管理员独立保存。解密后的文件包含敏感配置与登录会话，应保存在私有目录中。

下载 `.tgz.gpg` 文件和 `SHA256SUMS` 后，可检查文件完整性。

```bash
sha256sum -c SHA256SUMS
gpg --batch --pinentry-mode loopback --passphrase-file /path/to/private/key.txt --output metadata.tgz --decrypt metadata-20261122T130213Z.tgz.gpg
```

此快照记录生成时的状态。后续补收进度需要通过更新快照保存。
