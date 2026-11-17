# 界面与日常升级（0.5.9）

## 当前界面

- `/start`：先发实时资源统计，再发公开文件夹列表。列表按名称末尾数字编号倒序排列（1051、1050、1049……），分页前先排序，搜索与收藏列表一致。`/catalog` 命令已删除，普通列表底部无“我的收藏”按钮。
- 点击编号：第一张图片与名称、数量、大小、来源创建/更新时间在同一张卡片中。卡片成功发送后清理对应的列表与统计消息；发送失败保留菜单。若删除接口暂不可用，卡片仍正常打开并记录错误类型。旧版未记录对应关系的菜单，重新 `/start` 后即可按新流程使用。
- 卡片按钮：查看内容、收藏/已收藏、我的收藏、文件夹列表。没有可用封面时使用文字卡片。
- 查看内容：首次发送详情，再按来源顺序发送文件；连续文档或原生视频分别成相册，每组最多 10 个。24 个文档加 7 个视频会分成 10、10、4、7。
- 上一组/下一组不重复详情。所有内容确认发出后显示“✅ 文件夹内容发送完成”和本次内容消息数；未浏览前面分组而跳到末组时只报告本组进度。
- 收藏按用户保存，可用 `/favorites` 访问、取消；浏览状态和收藏重启后保留。列表与内容页的返回按钮继续正常工作。

消息数包括首次内容详情和真正发出的每个媒体消息，相册内每项各算一条；不含菜单/翻组提示。重新发送已经浏览过的一组会增加实际发送数，完成判断使用独立资源回执覆盖，不能只用当前页号判断。

## 已接入服务器的正常代码升级

不清空索引，不重复登录，不整体覆盖定制的 `/root/sougabot/bot.py`。先确认当前导入已完成，尤其不要在本地 API 的未知发送结果尚未核对时直接重发。

以下示例在实际部署服务器执行；任一命令失败先排查，不继续覆盖数据：

```bash
set -e
systemctl stop sougabot
bridge_backup="/root/sougabot-backups/update-$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 700 "$bridge_backup"
cp -p /root/tg-folder-bridge/.env /root/tg-folder-bridge/data/index.sqlite3 "$bridge_backup/"
cp -p /root/sougabot/bot.py /root/sougabot/resources.db "$bridge_backup/"
for item in performance.json rate-limit.json relay.session; do
    if [ -f "/root/tg-folder-bridge/data/$item" ]; then
        cp -p "/root/tg-folder-bridge/data/$item" "$bridge_backup/"
    fi
done
git -C /root/tg-folder-bridge rev-parse HEAD > "$bridge_backup/previous-commit"
git -C /root/tg-folder-bridge pull --ff-only
/root/sougabot/.venv/bin/pip install -r /root/tg-folder-bridge/requirements.txt
systemctl start sougabot
journalctl -u sougabot -n 50 --no-pager
```

仅在依赖变化时需要重新安装 requirements；配置已正确时不必重复运行 configure_transfer.py。若只是 Markdown 文档更新，`git pull --ff-only` 同步后无需重启服务。

默认自动恢复之前活跃的下载上传任务；失败/partial 等任务不会无限重试。需要手动续传时用 `/resume 原任务编号`，新分享才用 `/fetch 分享链接`。未知本地发送结果按 [HYBRID.md](HYBRID.md) 先核对。

## 主动清空不是升级步骤

`reset_transfers.py` 仅用于另行决定清空全部转存索引时。它会清除任务、文件、别名、收藏、浏览回执和菜单对应关系等，旧编号失效；不会删除 relay 登录会话、Telegram 聊天旧媒体或原 resources.db。脚本会备份索引并要求 spool 未被其他进程占用。

以下命令仅预览数量：

```bash
/root/sougabot/.venv/bin/python /root/tg-folder-bridge/reset_transfers.py
```

只有主动追加 `--apply` 才清空；自定义 DATA_DIR 需带 `--data-dir 实际目录`。正常升级不要执行清空后重新 /fetch。更完整的备份和回退边界见 [OPERATIONS.md](OPERATIONS.md)。
