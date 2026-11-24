# 当前部署与验收

更新：2026-11-24；运行代码 Bridge 0.13.0。本文记录维护者现有部署的结果，不表示克隆仓库后会自动创建 Cloudflare 资源。

## 已上线

- 日常机器人已切换到 Cloudflare Workers + D1 + Queues；Telegram Webhook 使用私密校验头，原 getUpdates 消费者和本地 Bot API 已停用。
- 公开目录按原 Store.catalog 范围迁移：988 个文件夹、33,784 个媒体文件。资源统计为 34,772 条消息、4,903 张图片、15,891 个视频；消息统计包含每个文件夹的一条详情，文档单独分类。
- 收藏、订阅选择已迁移。首次不广播历史目录，后续同步不覆盖 D1 中的用户选择。已有聊天媒体和原件 file_id 不变，无需重传原文件。
- Python 采集器与转存专用入口仍位于 Codespaces。已停止的 76 个补收任务保持停止，已保存文件保留；未主动恢复或重传。
- 转存程序已经完成迁移后的再次目录同步；元数据请求最多四路并行，临时拒绝有限退避，发布确认丢失时核对 active generation。

## 已验证的行为

- `/start` 返回统计和编号目录；点击编号后显示图片与详情的单条卡片，并删除对应列表和统计。
- 一个包含 9 个文件的既有文件夹，经 Cloudflare 队列连续发送并显示完成消息；没有经过 Codespaces 重新下载原件。
- 没有旧封面缓存的小型 JPEG 文件夹，能够通过受限流式上传生成原生图片详情卡；随后复用缓存 photo file_id。HEIC 或超限首图无现成预览时仍使用文字卡片。
- 管理员 `/status` 经私有 inbox 正常返回，`/admin` 能显示转存程序心跳。
- 转存与采集服务停止后，独立向已认证的 Cloudflare Webhook 提交目录请求，事件正常完成，证明日常服务不依赖这两个 Python 进程。
- `Check` 的 Python 3.11/3.12/3.13、Edge 集成测试、Worker 打包、本地 D1 迁移、实际部署均在 GitHub Actions 完成。

## 备份与后续运行

[Backup edge database](https://github.com/zwthys-cyber/tg-folder-bridge/actions/workflows/edge-backup.yml) 已成功生成并校验加密 D1 Artifact，后续每天 04:17 UTC 执行，保留 90 天。解密密钥由 GitHub Secret `EDGE_BACKUP_KEY` 提供，维护者须另外私下保管；不放进仓库、日志或同一个备份 Artifact。原 Python 索引的加密恢复包仍单独保留。

Codespaces 停机只会停止新采集、新转存和目录同步。已同步的浏览、收藏、订阅设置和原件取回继续由 Cloudflare 处理。转存程序重启需使用迁移后的私有启动器；不要同时启动旧轮询入口。Cloudflare 免费额度和 Telegram 限流仍然适用，不承诺无限请求或解除第三方限流。

迁移、当前兼容边界和回滚见 [CLOUDFLARE.md](CLOUDFLARE.md)。
