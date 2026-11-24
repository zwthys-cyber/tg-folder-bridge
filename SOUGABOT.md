# 接入已有 sougabot（0.11.0）

继续沿用现有 sougabot 时使用本页。要摆脱 Codespaces 常驻限制，请使用 [Cloudflare 迁移流程](CLOUDFLARE.md)；新入口 `edge_importer.py` 不调用 getUpdates，不能与本页旧轮询同时启动。

目标为 Python 3.13、urllib 调用 Bot API、唯一的 getUpdates 长轮询、systemd 服务 `sougabot`。示例路径为 `/root/sougabot` 和 `/root/tg-folder-bridge`；现有搜索数据库保持在 `/root/sougabot/resources.db`。

## 已接入的服务器

正常更新只拉取桥接项目，不再把参考 [integrations/sougabot_bot.py](integrations/sougabot_bot.py) 整体覆盖到定制的 `/root/sougabot/bot.py`。备份、升级和恢复步骤见 [UPGRADE_CATALOG.md](UPGRADE_CATALOG.md)。下载上传和 hybrid 参数见 [TRANSFER.md](TRANSFER.md)、[HYBRID.md](HYBRID.md)。

适配器处理 `/start`、`/favorites`、`/help`、`/notifications`、`/subscribe`、`/unsubscribe`、文件夹浏览和管理命令，`/catalog` 命令已删除。先搜索文件夹名称，未命中再走原 `resources.db` 搜索。公开取回、收藏、视频相册、发送回执、通知订阅与菜单清理均使用独立的 `data/index.sqlite3`，不写入原资源表，也不增加第二个轮询进程。通知默认关闭；用户主动订阅后接收新完成文件夹更新，见 [NOTIFICATIONS.md](NOTIFICATIONS.md)。

## 首次准备

在真正部署服务器执行：

```bash
git clone https://github.com/zwthys-cyber/tg-folder-bridge.git /root/tg-folder-bridge
/root/sougabot/.venv/bin/pip install -r /root/tg-folder-bridge/requirements.txt
cp /root/tg-folder-bridge/.env.example /root/tg-folder-bridge/.env
chmod 600 /root/tg-folder-bridge/.env
```

填写 API_ID、API_HASH、OWNER_IDS、RELAY_USER_ID，使用绝对的 DATA_DIR。此接入方式由原程序传入 Token，桥接 `.env` 不需要复制真实 BOT_TOKEN。已有 `.env` 时不要再用示例覆盖。

```bash
cd /root/tg-folder-bridge
/root/sougabot/.venv/bin/python login.py
```

只在交互终端输入验证码/二次验证密码，按输出核对 RELAY_USER_ID。用中转用户账号打开自己的机器人点击开始。

## 在原程序接入

先停止服务并备份原 `bot.py`、资源库、真实配置和转存索引。参考接入版保留原收录、搜索、搜索分页和管理员绑定；若原代码已有定制，逐处合并，不整体覆盖。

初始化桥接一次：

```python
import sys
sys.path.insert(0, '/root/tg-folder-bridge')
from souga_adapter import SougaBridge

folder_bridge = SougaBridge(
    bot_token=TOKEN,
    env_file='/root/tg-folder-bridge/.env',
).start()
```

原 getUpdates 循环收到更新后优先派发，并让所有分支都推进同一 offset：

```python
for update in updates:
    try:
        if folder_bridge is None or not folder_bridge.handle_update(update):
            handle(update)
    finally:
        offset = update['update_id'] + 1
```

程序退出时调用 `folder_bridge.close()`。顺序调用 `handle_update()`，不要另起 polling 或同时运行 `main.py`。适配器使用 `updater(None)`；线程内只处理消息、用户会话和转存，不接管 getUpdates。

## 本地 API 入口必须一致

使用 hybrid 时，原 urllib 的 getMe/getUpdates/搜索发送与桥接的所有 Bot API 调用必须使用同一个 `BOT_API_BASE`。在第一次 getMe 之前加载桥接 `.env` 并调用 `bot_api_config.endpoint()`；参考接入版已实现此顺序。现有定制程序按相同方式修改 URL，不能只修改桥接的发送地址。云端迁移到本地的专门步骤见 [HYBRID.md](HYBRID.md)，已迁移服务器不要重复 logOut。

## 启动和验收

```bash
systemctl start sougabot
systemctl status sougabot --no-pager -l
journalctl -u sougabot -n 50 --no-pager
```

应看到 Bridge 版本、传输模式和“沿用原 getUpdates 接收消息”。验证 `/start` 的统计和列表、目录卡片、翻组、收藏，以及原 `/add`、搜索、`/delete`。管理员用一个小分享测试 `/fetch`、`/status`、`/folder`；已有任务续传用 `/resume 任务编号`，不是分享链接。

配置或登录失败时，参考接入版保留原搜索并记录错误类型；这不代表转存已连接。回退时保留原 Bot API 入口，按 [OPERATIONS.md](OPERATIONS.md) 的数据库兼容说明处理。
