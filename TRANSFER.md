# 逐文件下载、上传与清理（0.12.0）

`TRANSFER_MODE=download_upload` 使用中转账号实际可读取的来源媒体，将一个原文件暂存在服务器，核验、上传并确认保存到自己的机器人后清理。它不修改 Telegram 的来源权限；来源已删除或不可访问时仍无法恢复。

## 开启与默认值

仓库 `.env.example` 默认是 forward。已接入且要改为下载上传时，在真实部署服务器执行：

```bash
/root/sougabot/.venv/bin/python /root/tg-folder-bridge/configure_transfer.py
```

工具私有、原子更新以下五项，保留其他账号、Token、API 地址和上传通道配置：

```dotenv
TRANSFER_MODE=download_upload
MAX_MEDIA_FILE_GB=4
MIN_FREE_DISK_GB=10
JOB_TIMEOUT_SECONDS=86400
DISPLAY_MODE=document_group
```

重启后生效；常规升级步骤见 [UPGRADE_CATALOG.md](UPGRADE_CATALOG.md)。新分享 `/fetch 分享链接`，已有任务 `/resume 原任务编号`。

## 处理顺序和确认

先收集各批次的消息元数据，自动处理允许的下一组按钮；发送和保存仍保持来源顺序；普通新文件可在当前文件上传期间预下载下一个文件，不把整文件夹下载到磁盘。上传前核验原始字节与可用的视频参数，确认重复后合并索引，规则见 [DEDUP.md](DEDUP.md)。

relay 通道由用户账号上传，等待带随机关联标记的机器人收件回执，再保存自己的 file_id。hybrid 小文件通道以本地 API 返回的回执和实际大小确认保存，不依赖用户聊天的随机标记。两者都确认后计数，不把“请求已发出”等同于“上传成功”。

## 暂存和磁盘

- 默认原件上限 4 GiB，至少保留 10 GiB 空闲；下载前和写入中都检查。配置上限不提升 Telegram 账号或通道权限。
- 普通新导入最多暂存当前与下一个两个原文件（各不超过 4 GiB）及小型展示图/缩略图；修复/审计仍串行，不下载整个分享。JPEG 展示图最多 2 MiB，文档缩略图小于 200 KiB。
- 正常确认、失败、取消或任务超时都会退出暂存上下文并清理。强制退出后的 `transfer-*` 残留由下一次启动清理；spool 独占锁防止两个实例互相操作。
- hybrid 使用同文件系统的交付硬链接，不再复制一个大文件；共享目录权限见 [HYBRID.md](HYBRID.md)。
- 这是桥接 spool/交付目录的清理规则，不代表管理官方本地 Bot API 服务自身的缓存。

```bash
df -h /root/tg-folder-bridge/data
du -sh /root/tg-folder-bridge/data/spool
```

不要在运行中手工删除当前暂存文件、索引或会话。清理本地暂存不删除 Telegram 已有媒体。

## 恢复与进度

默认 `AUTO_RESUME_INTERRUPTED=1` 会在重启后恢复此前活跃的下载上传任务，按来源稳定媒体标识和别名跳过已保存文件。当前未完成文件通常重新下载；同次并行下载的短暂限流/断线按失败偏移重试最多 3 次，保留其他已完成分块；同一次 MTProto 上传调用内的短暂重连也能复用已确认分块，不声称跨进程保留文件字节进度。

本地 API 未知回执不会自动重发或换通道。管理员确认聊天未收到后使用 `/resolve 任务编号 retry`，再 `/resume`；详见 [HYBRID.md](HYBRID.md)。

`/status` 显示来源条目/独立资源、当前阶段、下载或 relay 上传进度、平均/近期速度和等待。首次几个分块的 ETA 不稳定。本地 API 上传无可靠远端字节回调，显示等待确认而不是虚构百分比。

## 显示与格式

默认 `DISPLAY_MODE=document_group` 保留原文件名、大小及可用缩略图；原生视频保留视频类型并在浏览时成相册。HEIC 原件不替换，封面/缩略图使用小型 JPEG 展示副本。旧照片预览布局可以额外存小预览，但默认文档布局不是“每张 HEIC 都单独发照片预览”。

不会额外生成视频转码播放版。圆视频、贴纸等特殊媒体可能作为普通附件处理；caption 保存为普通文本，原来源的相册分组/嵌套目录不复刻。具体客户端排版由 Telegram 控制。

并发默认下载/上传各 8 路，可用范围 1–32；自动探测最多 16 路，配置优先级、连接池和限流见 [SPEED.md](SPEED.md)、[RATE_LIMITS.md](RATE_LIMITS.md)。
