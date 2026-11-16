# 文件夹列表、文档缩略图与重新导入

此版本按确认的要求更新：

- 点击开始或发送 `/start`，显示已导入完成的文件夹名称、原文件数量、每页 10 个文件夹及编号按钮。任何私聊用户都能浏览这些已完成文件夹。未完成的导入不公开。
- 点编号打开文件夹，以 Telegram 原生文档相册分组发送，每组最多 10 个文件。图片/HEIC/视频可附缩略图，文件行由 Telegram 显示原文件名与大小；原文件不转码。具体布局由用户的 Telegram 客户端控制。
- HEIC 缩略图从解码后的 JPEG 生成；视频从首帧生成。首次安装新增 imageio-ffmpeg 依赖，Linux wheel 自带 ffmpeg，无需手工安装系统 ffmpeg。缩略图生成失败时原件仍上传，可能显示普通文件图标。
- 自动识别“➡️ 点击查看下一组”。旧 `.env` 的 BUTTON_LABELS 缺少“下一组”也会兼容；按批次计数去重，同一消息重复编辑不重复点击，计数改变后同一个按钮仍能用于下一批。
- 接收批次按钮时提前获取后续消息列表，不等前一组的大文件全部上传结束；只预取消息元数据，磁盘仍最多暂存一个原文件和小型预览/缩略图。
- 更新公开命令菜单以及管理员专属命令菜单。`/help` 提供新使用说明；原 `/add`、`/delete`、管理员绑定及关键词搜索保持原程序逻辑。
- 直接输入关键词时先搜索转存文件夹名称，命中后给出可点击、可翻页的文件夹列表；未命中继续执行原 resources.db 搜索。普通用户只能搜到导入完成的文件夹，管理员还能搜索自己尚未完成的导入。旧版本没有保存文件夹名称，升级后重新导入/续传读取源卡片时才会补齐名称；不会靠文件名猜测标题。
- 启动日志显示中转账号的 Premium 状态，便于确认开通的是服务器使用的同一账号；不承诺会员或并发上传带来固定倍数提速。

## 已部署服务器：清空全部转存索引并重新导入

已确认 Telegram 聊天中的旧媒体不删除，只清空所有转存任务与文件索引。脚本完全不连接 Telegram，不读取 Bot Token，也不打开原 resources.db。会在 `data/reset-backups/` 保存一份小型旧索引备份；备份只有数据库元数据，不含媒体。

在实际部署 sougabot 的服务器依次执行，任何命令报错时先处理错误，不继续下一步：

```bash
git -C /root/tg-folder-bridge pull --ff-only
/root/sougabot/.venv/bin/pip install -r /root/tg-folder-bridge/requirements.txt
/root/sougabot/.venv/bin/python /root/tg-folder-bridge/configure_transfer.py
systemctl stop sougabot
/root/sougabot/.venv/bin/python /root/tg-folder-bridge/reset_transfers.py --apply
systemctl start sougabot
journalctl -u sougabot -n 50 --no-pager
```

不需重新登录，也无需再次覆盖 bot.py。此升级由适配器接管 `/start`、`/catalog`、`/help` 及文件夹按钮；仍使用原有 getUpdates。

启动后管理员重新发送 `/fetch 分享链接`。清空索引之后旧任务编号不再有效，请使用新返回的编号查询 `/status 新编号`。旧聊天媒体仍存在，新的导入会产生新副本，这是本次重新上传的预期行为。

导入完成后，任何用户向机器人发送 `/start` 即可看到新文件夹。若列表暂时为空，请先用管理员账号检查任务是否 complete；完成前不会公开显示部分内容。

### 可先查看清理数量

不加 `--apply` 只预览，不删除：

```bash
/root/sougabot/.venv/bin/python /root/tg-folder-bridge/reset_transfers.py
```

默认索引目录为 `/root/tg-folder-bridge/data`；自定义 DATA_DIR 时，用 `--data-dir 实际目录` 显式指定。清理需要 sougabot 已停止，且不能有其他下载进程持有该目录的 spool 锁。登录会话、旧 Telegram 消息和原搜索资源不受清理影响。

## 检查与实际验收

GitHub Actions 检查跨批次去重、下一组预取、标题提取、公开列表权限、分页按钮、原生文档分组、真实图片/视频缩略图、菜单设置以及清空索引的范围。检查不使用真实 Telegram 账号。

实际部署后请确认：源机器人持续翻到最后一组；计数达到卡片媒体总数；用户开始菜单显示文件夹名称与数量；文档行显示图片和视频缩略图；所有转存完成后临时目录无 transfer-* 文件。原生消息的具体视觉表现仍需 Telegram 客户端验收。
