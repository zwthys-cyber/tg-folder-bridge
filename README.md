# Telegram 文件夹转存机器人

将 `https://t.me/epan?start=...` 分享链接交给自己的机器人，由专用 Telegram 用户账号打开分享、点击获取文件按钮，跟随允许的机器人跳转，将图片、视频和文档直接在 Telegram 内转发给自己的机器人。

**媒体不下载到服务器。** 服务器只保存 Telegram 登录会话和 SQLite 文件索引。自己的机器人记录收到的 `file_id` 和消息 ID，之后通过 `/folder` 按文件夹取回文件。

## 当前状态

本项目根据分享卡片截图实现，支持 URL 按钮和回调按钮。尚未登录真实账号验证 `@epan` 的完整交互，不保证其全部菜单已兼容。GitHub Actions 只检查代码、解析、索引持久化和模拟云端转存，不会连接 Telegram。

卡片上的消息总数可能包含说明文字。例如 64 条消息、2 张图片、46 个视频、15 个文档，媒体预期数量是 63。只有收到数量与卡片媒体数量一致时，任务才标为 `complete`；静默超时且无法核对的任务标为 `needs_review`，已收到的文件仍可取回。`complete` 表示数量核对通过，并不验证每个文件的内容。

## 必需配置

- 你现有机器人的 Bot Token。
- 一个专门用于中转的 Telegram **用户账号**，可以正常访问源分享内容。
- 从 [my.telegram.org](https://my.telegram.org) 的 API development tools 获取 `API_ID` 和 `API_HASH`。
- 允许使用的用户 ID：`OWNER_IDS`；中转登录账号的 ID：`RELAY_USER_ID`。

验证码、二次验证密码、Token、API Hash 和 `data/relay.session` 只放在实际部署服务器，不能提交到 GitHub，也不需要发到聊天里。中转账号使用该服务期间不要手动向自己的机器人转发文件，也不要手动操作源机器人，以免把人工消息混入任务。一次只运行一个实例。

## 在你的部署服务器安装

以下命令在你准备部署的服务器上执行。本仓库的 GitHub Actions 不部署服务、不保存账号会话。

```bash
git clone https://github.com/zwthys-cyber/tg-folder-bridge.git
cd tg-folder-bridge
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env，填写实际参数
python login.py
# 根据登录输出确认 .env 中的 RELAY_USER_ID
python main.py
```

使用中转账号先在 Telegram 中打开自己的机器人并点击 Start。账号登录只需在部署服务器交互完成一次，后续使用会话文件连接。

**已有机器人正在运行时，不能使用同一 Token 再启动本项目的独立 polling 进程。** 请选择将模块接入原程序，或者停止原进程后由本项目接管。后者不会保留原程序功能。本项目不会自动修改你服务器上的现有程序。

## 接入已有 Python 机器人

**如果现有程序是 sougabot，使用 urllib/getUpdates：请按 [SOUGABOT.md](SOUGABOT.md) 接入。** 该适配器将原程序收到的更新字典交给模块，不会另启长轮询，也不会修改原 resources.db。

目前提供 `python-telegram-bot 22.x` 的模块接入方式。`bridge.install(app)` 添加 `/fetch`、`/folder` 和中转文件接收处理器，使用处理组 `-100`，并阻止这些消息进入其他处理器。

```python
import os
from dotenv import load_dotenv
from telegram.ext import Application
from bridge import Bridge

load_dotenv()
os.umask(0o077)
bridge = Bridge()

async def startup(app):
    # 原程序的启动逻辑放在这里
    await bridge.start(app)

async def shutdown(app):
    await bridge.stop(app)
    # 原程序的停止逻辑放在这里

app = (Application.builder().token(os.environ['BOT_TOKEN'])
       .post_init(startup).post_stop(shutdown).build())
bridge.install(app)
# 添加原程序其他处理器
app.run_polling(drop_pending_updates=False)
```

若原程序使用自定义 webhook 生命周期，需在初始化之后手动 `await bridge.start(app)`，停止时 `await bridge.stop(app)`。保持顺序处理更新；不要启用并发更新。其他语言/框架需要按同样流程移植，当前未提供通用 HTTP 接口。

## 使用

在与自己机器人的私聊中发送：

```text
/fetch https://t.me/epan?start=分享参数
```

也可直接发送链接。机器人返回任务编号。查询任务并取回第一页：

```text
/folder 任务编号
```

每页发送 10 个文件；第二页使用 `/folder 任务编号 2`。只允许任务提交者取回自己的文件。程序先将文件转发到中转用户与自己机器人的私聊，再由机器人保存索引，用自己的 `file_id` 发给请求者。收件确认失败会停止后续导入，避免延迟消息混入新任务。

发送 `/folders` 可查看自己最近 20 个任务。具体安装方式、systemd 服务配置及首次联调步骤见 [DEPLOY.md](DEPLOY.md)。每次 GitHub 检查通过后，Actions 会提供 `tg-folder-bridge` 下载包，内含源码 ZIP 和 SHA-256 校验文件。

## 限制与恢复

- 文件存储在 Telegram 中，服务器上的索引需要备份；删除索引会失去按任务检索能力。`/folder` 使用自己机器人的 `file_id` 重新发送，即使中转聊天收件消息已删除也不依赖原消息；不保证文件在 Telegram 中永久可用。caption 保留为普通文本，原格式实体不保留。
- 默认只跟随 `epan`、`SPXM111_bot`，实际按钮指向其他机器人时需在 `.env` 的 `ALLOWED_SOURCE_BOTS` 增加目标。只自动点击 `BUTTON_LABELS` 匹配的获取/下载/翻页按钮，最多执行 100 次操作。
- 需要加入频道、密码、付费或其他交互时，任务可能结束为 `needs_review`。程序不自动处理这些步骤。
- 源内容禁止转发时报告失败，不下载重传。图片相册逐条转存，原相册分组和嵌套目录层级暂不保留；一个分享对应一个文件夹索引。
- 服务不读取源机器人历史文件，不保证断线期间消息补齐。重启将未完成任务标为 `interrupted`；需确认源发送已停止后重新提交，重新导入会产生新任务。
- 默认 45 秒无新消息结束收集，整个任务最多一小时；可用 `IDLE_SECONDS`、`JOB_TIMEOUT_SECONDS` 调整。服务无通用的源请求关联标识，使用专用中转账号、串行任务和独占源聊天；特别慢的上一任务回复仍可能影响后一任务，需要根据实际发送方式联调。
- Telegram 对同一机器人收到的文件提供自己的 `file_id`，不能直接复用对方机器人的 `file_id`。参考 [Telegram 文件说明](https://core.telegram.org/bots/api#file)、[Telethon 转发接口](https://docs.telethon.dev/en/stable/modules/client.html#telethon.client.messages.MessageMethods.forward_messages)。

## GitHub 检查

推送自动触发 Python 3.11、3.12、3.13 检查。不需要任何 GitHub Secrets，不使用真实 Telegram 账号。真实登录和端到端验证需要在最终部署环境完成。
