# 独立链接采集器（0.7.0）

`collect_links.py` 是独立进程，共用已登录的专用中转账号。当前适配 SPXM111_bot 的目录：每页通常 10 个序号，逐个打开详情、读取明确的分享链接、返回目录，再扫描下一页。不会点击“查看内容”，不靠猜测文件夹编号生成分享参数。

## 去重与续传

- 先以只读方式检查现有 `index.sqlite3`，按分享目标和完整 start 参数匹配，不按名称或文件大小判断。用户名忽略大小写，分享参数保留大小写。
- 已完成任务跳过；同链接存在多次历史尝试时，优先采用已完成任务。不会删除历史任务或聊天媒体。
- 现有未完成任务使用 `/resume 原编号`，不新建第二个任务。采集器一次只提交一个任务，等待转存空闲后继续；失败的自动提交不反复重试，列为 `review`。
- 来源回调标识与链接分别去重，逐项持久化到 `DATA_DIR/collector.sqlite3`。每页全部处理成功才推进页码，重启从断点继续。
- 命令提交前持久化 `dispatching`；提交后核对真实任务入库。若发送结果不明确，停止后续自动提交，供管理员查看机器人回复和任务记录，避免盲目重发。
- 手动 `/fetch` 相同分享也会返回已有任务编号；未完成时改用 `/resume`。

因此已导入 1051–1040 时，重新扫描这些条目也不会再次上传。1039 若已有未完成任务，沿用它继续。文件夹内部仍沿用既有完整内容核验规则，不增加不同转码视频的相似画面去重。

## 部署

先升级转存服务到 0.7.0，使转存与采集共同使用 `source-control.lock`。同一时刻只有一个进程操作来源菜单。采集账号必须是 `RELAY_USER_ID`，且在 `OWNER_IDS` 中；读取已有登录认证到内存，不写共享 `relay.session`，不启动第二个 Bot API 轮询。

```bash
cd /root/tg-folder-bridge
# 采集一页并与现有任务核对，不提交下载任务
/root/sougabot/.venv/bin/python collect_links.py scan --pages 1
# 查看页码、链接状态及需要处理的记录
/root/sougabot/.venv/bin/python collect_links.py status
# 导出链接和状态
/root/sougabot/.venv/bin/python collect_links.py export --output /root/collected-links.csv
# 自动扫描、去重、顺序提交给自己的机器人
/root/sougabot/.venv/bin/python collect_links.py run --target-bot sougabot
```

确认配置后，使用 [独立服务](deploy/tg-link-collector.service)：

```bash
cp deploy/tg-link-collector.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now tg-link-collector
journalctl -u tg-link-collector -n 30 --no-pager
```

停止自动采集/提交使用 `systemctl stop tg-link-collector`，已提交的转存任务继续完成。账号限流时保存进度并遵守等待时间；来源菜单变化会停止当前扫描并记录错误，不误点内容按钮。服务失败后隔 60 秒重启。

`run` 在待处理链接用尽时扫描下一页；每隔至少 10 分钟、转存空闲时重新检查第一页，发现 1052、1053 等新目录后加入队列并优先处理。已扫目录仍按链接去重。主扫描断点不被顶部检查覆盖；完成全目录后继续定时检查新增文件夹。现有文件夹内部新增媒体尚不自动触发重新导入。采集器对自己的队列使用进程锁，不能同时运行第二个采集命令；`status`、`export` 可随时使用。导出文件含分享链接，应由管理员保存。
