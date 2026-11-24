# 部署与接入（0.13.0）

新增 [Cloudflare 日常服务部署](CLOUDFLARE.md)：适用于 Codespaces 停机后仍需提供目录和已有文件的场景。下文是原 Python 单机/适配器部署；两种模式不能同时消费同一机器人的更新。

## 获取代码与选择模式

GitHub Actions 的成功 Check 提供 `tg-folder-bridge` Artifact，包含源码 ZIP 和 SHA256SUMS；也可使用 Git 仓库。检查与打包在 GitHub 执行，登录和服务在实际部署服务器执行。

- 已有 sougabot：使用 [SOUGABOT.md](SOUGABOT.md)，不创建第二个服务。
- 已有 python-telegram-bot：将 Bridge 接入同一个 Application 和生命周期。
- 独立运行：仅在没有其他进程消费同一 Token 的更新时运行 `main.py`。

## 独立服务器安装

以下为 `/opt/tg-folder-bridge` 的专用用户示例：

```bash
sudo useradd --system --no-create-home --home-dir /opt/tg-folder-bridge --shell /usr/sbin/nologin tgbridge
sudo install -d -o tgbridge -g tgbridge /opt/tg-folder-bridge
sudo -u tgbridge git clone https://github.com/zwthys-cyber/tg-folder-bridge.git /opt/tg-folder-bridge
cd /opt/tg-folder-bridge
sudo -u tgbridge python3 -m venv .venv
sudo -u tgbridge .venv/bin/pip install -r requirements.txt
sudo -u tgbridge cp .env.example .env
sudo -u tgbridge mkdir -p data
sudo chmod 700 data
sudo chmod 600 .env
sudo -u tgbridge nano .env
sudo -u tgbridge .venv/bin/python login.py
```

填写真实配置并核对用户 ID。默认是云端 API、forward、relay；需要逐文件下载上传时显式配置，见 [TRANSFER.md](TRANSFER.md)。内容核验使用 FFprobe，Debian/Ubuntu 可安装 `ffmpeg`；拍摄日期识别与补读已停用，不要求安装 ExifTool。发送顺序见 [CAPTURE_TIME.md](CAPTURE_TIME.md)，更新通知见 [NOTIFICATIONS.md](NOTIFICATIONS.md)。缩略图还可使用 imageio-ffmpeg 自带的 ffmpeg。

独立服务模板位于 `deploy/tg-folder-bridge.service`：

```bash
sudo cp deploy/tg-folder-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tg-folder-bridge
sudo journalctl -u tg-folder-bridge -n 50 --no-pager
```

模板以 tgbridge 运行并限制写入目录为 data。hybrid 的共享交付目录若位于模板之外，需同时配置该服务的 ReadWritePaths 和目录/组权限；不要通过放开 `/root` 来解决文件读取。已有 sougabot 不使用这个独立服务模板。

## 接入 PTB Application

```python
import os
from dotenv import load_dotenv
from telegram.ext import Application
from bridge import Bridge
from bot_api_config import configure

load_dotenv()
os.umask(0o077)
bridge = Bridge()
app = (configure(Application.builder().token(os.environ['BOT_TOKEN']))
       .post_init(bridge.start).post_stop(bridge.stop).build())
bridge.install(app)
# 在这里添加原程序其他处理器；-100 组为 Bridge 保留。
app.run_polling(drop_pending_updates=False)
```

原程序若已有启动/停止回调，需要把 Bridge 的调用合并进去。自定义 webhook 生命周期在应用初始化后调用 `bridge.start(app)`，退出前调用 `bridge.stop(app)`。保持顺序处理更新；不同时开启 webhook 与长轮询，不重复消费同一个 Token。

## 首次验收

1. 日志确认版本、账号身份、传输模式与唯一的更新接收进程。
2. 管理员用 `/fetch 分享链接` 导入一个小文件夹，观察来源下一批是否继续发送；`/status 任务编号` 查看阶段与计数。
3. 完成后，普通用户 `/start` 可看到统计和列表。点编号显示封面详情卡片，并移除对应菜单消息。
4. 点查看内容，验证文档/视频相册、来源顺序、翻组不重复详情、完整发送后的计数。
5. 验证收藏按用户隔离，服务重启后收藏与浏览进度仍在；原搜索功能正常。
6. 确认暂存清理和磁盘保留空间正常，没有新 429 或未确认发送。

GitHub 不使用真实 Token 或会话，不能替代来源权限、实际菜单和客户端布局验收。

## 状态、备份与恢复

`data/index.sqlite3` 不仅保存任务和 file_id，还包括重复别名、来源顺序、文件元数据、封面缓存、收藏、浏览回执、本地发送意图和菜单消息对应关系。保留它及 `.env`、relay.session、performance.json、rate-limit.json；原 sougabot 的资源库和令牌文件仍单独保存。

正常升级不清空任务，见 [UPGRADE_CATALOG.md](UPGRADE_CATALOG.md)。默认可自动续传升级前活跃的下载上传任务；未知本地回执先人工核对。不要把重启恢复与文件内部字节断点续传混为一谈。代码回退、共享 Bot API 和数据兼容见 [OPERATIONS.md](OPERATIONS.md)。
