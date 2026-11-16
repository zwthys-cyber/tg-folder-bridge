# 部署与接入

## 从 GitHub 获取检查通过的代码包

进入仓库 Actions → 最新成功的 Check → Artifacts → tg-folder-bridge。
解压 Artifact，再解压其中的 `tg-folder-bridge.zip`。`SHA256SUMS` 用于校验该代码包。
也可直接 `git clone` 仓库。打包和检查均由 GitHub 执行，不包含媒体文件、登录会话或真实配置。

## 选择接入方式

1. **保留现有机器人功能**：按 README 的模块示例，将 `Bridge` 加入现有的 python-telegram-bot 22.x 程序。沿用原程序的启动方式，不另开同 Token 的 polling 服务。其他语言或框架需要先适配，不能直接用示例替换。
2. **独立使用本程序**：在最终部署服务器停止旧程序后运行 `main.py`，此时只有本项目提供的功能。下列 systemd 示例只适用于这种方式。

## 独立程序的 Linux systemd 示例

适用于已安装 Python 3.11+、python3-venv、git 的 Linux 服务器。以下命令由你在最终部署服务器执行；不在 GitHub Actions 中执行。

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

登录输出的用户 ID 必须与 `RELAY_USER_ID` 一致。验证码和二次验证密码只在此登录终端输入。
用中转账号在 Telegram 中打开自己的机器人并点击 Start。

确认当前 Token 的旧机器人进程已停止，然后执行：

```bash
sudo cp deploy/tg-folder-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tg-folder-bridge
sudo systemctl status tg-folder-bridge
```

查看日志：`sudo journalctl -u tg-folder-bridge -n 100 --no-pager`。
停止服务：`sudo systemctl stop tg-folder-bridge`。

## 首次验证

1. 用 `OWNER_IDS` 中的账号私聊自己的机器人，先发送一个文件数较少的、你有权访问的分享链接。
2. 在专用中转账号中确认程序依次打开源机器人、获取按钮的目标机器人，并收到文件。
3. 自己的机器人应回复任务编号；发送 `/folders` 查看最近 20 个任务。
4. 发送 `/folder 任务编号`，确认返回图片、视频、文档；超过 10 个文件时用 `/folder 任务编号 2` 翻页。
5. 若任务为 `needs_review`，核对卡片媒体数量与实际收件数，检查是否存在未支持按钮、加入频道提示、超时或目标机器人未加入允许列表。

GitHub 模拟检查不能代替这一步。没有登录会话和真实 Bot Token 时，程序不会实际接入 Telegram。

## 保存和恢复

默认 forward 模式的 `data/` 只有账号会话和文件索引。download_upload 模式还使用 `data/spool/` 临时保存当前一个媒体文件，上传确认或任务异常后清理，不保留整文件夹。备份应停止服务，避免复制正在写入的 SQLite 文件；同时妥善保管 `.env`，无需备份 spool。空间上限和升级步骤见 [TRANSFER.md](TRANSFER.md)。
取回文件使用**自己的机器人收到文件后产生的 file_id**，不依赖中转聊天中的原消息仍然存在。原始文本说明保留为普通文本 caption，原 caption 格式实体和相册分组不保留。
索引与特定 Bot Token 对应的机器人绑定，换成另一个机器人不能直接复用这些 file_id。服务重启后未完成任务标为 `interrupted`；等待源机器人停止发送后，再重新提交。
