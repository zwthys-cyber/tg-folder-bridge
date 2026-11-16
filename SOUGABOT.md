# 接入另一台服务器的 sougabot

目标配置：Python 3.13、标准库 urllib 调用 Bot API、长轮询 getUpdates、systemd 服务 sougabot、项目目录 `/root/sougabot`，原数据库 `/root/sougabot/resources.db`。

如果已经完成接入，现在需要开启逐文件临时下载、上传后删除：直接按 [TRANSFER.md](TRANSFER.md) 更新转存模块和配置即可，无需再次覆盖 `/root/sougabot/bot.py`。

已根据提供的完整 `bot.py` 制作 [接入版 bot.py](integrations/sougabot_bot.py)。保留原搜索、搜索分页、管理员绑定、添加和删除功能，并在原更新循环中先分发转存消息，两个分支都会推进 offset。转存索引仍独立于原 resources.db；导入文件通过 `/folders`、`/folder` 查看，不自动混入原关键词搜索。

## 已登录中转账号：安装接入版

在实际运行 sougabot 的服务器执行以下命令。假定此前已经配置好 `.env` 并完成 `login.py` 登录：

```bash
git -C /root/tg-folder-bridge pull --ff-only
/root/sougabot/.venv/bin/pip install -r /root/tg-folder-bridge/requirements.txt
systemctl stop sougabot
backup_dir="/root/sougabot/backup-$(date +%Y%m%d-%H%M%S)"
install -d -m 700 "$backup_dir"
cp -p /root/sougabot/bot.py /root/sougabot/resources.db "$backup_dir/"
cp /root/tg-folder-bridge/integrations/sougabot_bot.py /root/sougabot/bot.py
systemctl start sougabot
journalctl -u sougabot -n 50 --no-pager
```

备份失败时先解决错误，不继续覆盖文件。原 `.bot-token`、`.admin-id` 和 `resources.db` 保持原位置，不要删除，也不需要把 Token 写进接入代码。成功启动应看到 `文件转存模块已连接，沿用原 getUpdates 接收消息。`。

随后在 Telegram 中先用 `/help` 检查连接，再发送一个小文件夹分享链接。使用 `/folders` 查看任务，用 `/folder 任务编号` 取回文件。首次真实账号转存仍需联调，GitHub 检查不代表已经访问过源分享。

若转存模块因配置或登录问题启动失败，原搜索功能仍继续运行，日志会显示错误类型。处理配置后 `systemctl restart sougabot` 可重试。需要回滚时，停止服务，将上面备份目录中的 `bot.py` 复制回原位置，再启动服务；无需回滚独立的转存索引。

以下各节记录模块接入原理和手工接入方法，采用完整接入版时无需再手工修改原代码。

## 工作方式

`souga_adapter.SougaBridge` 在原 sougabot 进程中运行后台线程，线程内处理 Telegram 用户会话和转存任务。原程序继续使用 urllib/getUpdates 接收消息，把相关更新字典传给 `handle_update()`。

模块内部使用 python-telegram-bot 处理字典和发送消息，但明确使用 `updater(None)`，不启动 polling，不调用 getUpdates、不变更 webhook。原程序仍是唯一的消息接收者。不要启动本仓库的 main.py 或额外的 tg-folder-bridge systemd 服务。

## 在实际部署服务器准备文件

以下命令在运行 sougabot 的另一台服务器执行。当前工作服务器与 GitHub Actions 都不执行登录或服务启动。

```bash
git clone https://github.com/zwthys-cyber/tg-folder-bridge.git /root/tg-folder-bridge
/root/sougabot/.venv/bin/pip install -r /root/tg-folder-bridge/requirements.txt
cp /root/tg-folder-bridge/.env.example /root/tg-folder-bridge/.env
```

编辑 `/root/tg-folder-bridge/.env`，填写 API_ID、API_HASH、OWNER_IDS、RELAY_USER_ID，明确设置 `DATA_DIR=/root/tg-folder-bridge/data`。适配器直接使用原程序传入的 Bot Token，不从这里读取 BOT_TOKEN。

在部署服务器中登录用户账号：

```bash
cd /root/tg-folder-bridge
/root/sougabot/.venv/bin/python login.py
chmod 600 .env
```

用登录输出核对 RELAY_USER_ID，并用该账号在 Telegram 中打开自己的机器人点击 Start。

## 修改原 bot.py 的三个位置

先备份原代码。以下的 `BOT_TOKEN`、`updates`、`existing_handle_update` 是说明用变量，需换成原程序的实际名称。不要将整个示例覆盖到原 bot.py。

导入并初始化一次，在原长轮询循环开始之前：

```python
import sys
sys.path.insert(0, '/root/tg-folder-bridge')
from souga_adapter import SougaBridge

folder_bridge = SougaBridge(
    bot_token=BOT_TOKEN,  # 原程序已读取的真实 token 变量
    env_file='/root/tg-folder-bridge/.env',
).start()
```

在原程序收到每一个原始 update 字典后、进入原业务处理之前：

```python
for update in updates:
    # 沿用原程序对 update_id/offset 的持久化策略。
    if folder_bridge.handle_update(update):
        # 即使跳过原业务处理，也必须正常推进该 update 的 offset。
        # 此处使用原程序的 offset 更新代码，然后继续下一条。
        continue
    existing_handle_update(update)  # 原有处理逻辑
```

**offset 是接入时必须核对的位置。** 如果原程序在业务处理之后才推进 offset，不能直接复制上面的 continue：应将 offset 推进放入每条更新处理共同执行的位置，确保桥接和原业务两个分支都能执行。不要另写一套 getUpdates。

在原程序正常退出的 finally/停止清理中：

```python
folder_bridge.close()
```

保持顺序调用 handle_update，不要并发派发；该调用会等待相关处理完成，取回一页文件时可能需要十几秒。若原程序有统一的默认回复，桥接返回 True 后不要再进入默认回复。

## 重启与测试

完成对照修改后，在实际部署服务器执行：

```bash
systemctl restart sougabot
systemctl status sougabot --no-pager -l
journalctl -u sougabot -n 50 --no-pager
```

先确认原功能仍可用，再私聊自己的机器人发送一个小的分享链接，之后用 `/folders` 和 `/folder 任务编号` 查看和取回。实际跳转、接收数量、源机器人的其他交互仍需真实账号联调。

若要提供 bot.py 用于生成精确补丁，请先移除 Token、API Hash、账号会话和其他密钥。若还需要导入原资源搜索，另外提供相关建表语句和查询/保存函数，无需发送真实数据库和文件内容。
