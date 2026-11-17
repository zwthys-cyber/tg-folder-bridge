# 按容量选择上传通道（0.7.0）

`UPLOAD_BACKEND=hybrid` 根据实际字节数路由：

| 原文件大小 | 通道 |
| --- | --- |
| `0 < size <= 2,000,000,000` | 官方本地 Bot API，传递本地文件 URI |
| 更大 | 专用用户账号的 MTProto 上传与机器人收件确认 |

边界为十进制 2000 MB，包含等于边界的文件。项目默认仍为 relay，hybrid 必须显式配置。官方本地服务的能力见 [Bot API 本地服务说明](https://core.telegram.org/bots/api#using-a-local-bot-api-server)；会员账号能力与该本地边界独立，见 [Premium FAQ](https://telegram.org/faq_premium)。

## 入口和首次迁移

```dotenv
BOT_API_BASE=http://127.0.0.1:8081
UPLOAD_BACKEND=hybrid
LOCAL_UPLOAD_DIR=/var/lib/tg-folder-bridge-handoff
LOCAL_UPLOAD_GROUP=ipazake
```

组名仅为一个部署示例，应与实际官方本地服务的运行组一致。同一个机器人的 urllib getMe/getUpdates/搜索发送和 PTB 菜单、浏览、上传必须共用入口；项目仅接受云端或此 loopback 地址。适配器仍不新建 Updater。

首次迁移前确认本地官方服务可用、绑定 loopback，备份原 bot.py、.env、索引和会话；等待当前发送确认后停机器人。仅对要迁移的机器人执行一次云端 logOut，再使用本地服务。不要注销中转用户或重启其他项目共用的本地服务。

官方 [logOut 说明](https://core.telegram.org/bots/api#logout) 指出迁移到本地后 10 分钟内不能重新登录云端。已迁移的服务器日常更新不再重复 logOut；代码回退也不能把同一个 Token 的轮询和发送拆到不同入口。

## 文件交付

本地服务以另一个用户运行时，使用同文件系统的专用交付目录：root 拥有，服务组可读，不向其他用户开放。每次创建私有子目录并硬链接当前原文件，清理路径分隔符/控制字符以保留安全文件名；发送结束后删除交付链接。

不要开放整个 /root。部署到不同文件系统时硬链接不能成立，先调整共享目录位置和服务权限；独立 systemd 沙箱也要允许该目录读写。此项目不安装或管理本地 Bot API 服务自身。

Python 将 file URI 和小型缩略图交给本地服务，不把整个原文件塞入 HTTP 请求体。原生视频按视频发送，其他原件按对应文件形式发送，不为兼容播放额外转码。封面是独立小 JPEG 展示图，原文件不替换。

## 回执与进度

提交前持久保存发送意图。回执中的聊天、自己机器人的 file_id、原件大小应匹配；发送意图确认和索引保存一起提交。本地 API 只在回执到达后计入已保存数量，没有可靠远端字节进度，因此不显示上传百分比。管理员调节 /speed 或 /transport 不直接控制本地官方服务的内部上传队列。

明确 API 拒绝可安全重新处理。发送超时、取消或回执后数据库故障可能出现未知结果：不自动切换通道，不在重启后盲目重复发送。

```text
/resolve 任务编号 retry
/resume 任务编号
```

只有管理员实际确认聊天没有收到该文件后才用上述重试。先等待当前任务停止，再处理未知意图。已收到时不要授权重发；保留现场和备份索引进行核对。

本地 API 的 RetryAfter 按返回时间冷却，不把另一身份的用户下载/上传配置一起降速。桥接 spool/交付链接会清理，本地官方服务自身的缓存由该服务独立管理。旧 Telegram 媒体不清理，取回继续使用同一个机器人的 file_id。
