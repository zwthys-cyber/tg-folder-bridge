# 拍摄时间与发送顺序（0.10.0）

“查看内容”保持原文件优先、原生视频最后的规则，每类内部按可识别的拍摄日期从新到旧发送。例如同类四个文件的时间为 2026-10-23、2026-10-17、2026-10-06、2025-05-04，则按这个顺序发送。原生图片和其他媒体位于文档之后、原生视频之前，分别组成相册。

## 时间来源

- 照片读取 EXIF `DateTimeOriginal`，包括 JPEG、HEIC/HEIF 等能够解析的格式；不把文件修改时间作为拍摄时间。
- 新增 ExifTool 只读解析照片 EXIF/XMP、带毫秒与时区的原始拍摄日期、GPS UTC 日期，以及视频 QuickTime 相机日期；没有该引擎时使用 Pillow/FFprobe 兼容读取。
- 视频优先读取 QuickTime 相机原始日期。只有容器/视频流 `creation_time` 时单独记录为创建时间，**不再把它用于拍摄日期排序**；此类文件按日期未知处理。原始日期字段本身也可能被设备时钟错误或人为编辑影响，不代表时间真实性认证。
- 优先采用文件自带时区，未携带时区时使用 `MEDIA_TIMEZONE`，默认 `Asia/Shanghai`。保存为 UTC，比较的是实际时间。
- 不使用 Telegram 上传时间、导入时间或文件名猜测拍摄日期。无有效日期的文件排在同类末尾，相同日期及日期未知时保留原有来源顺序。

该功能只读元数据，不转码、不重新压缩、不更改文件名、GPS、文件内容哈希或已保存的 `file_id`。

## 新导入与旧索引

新导入在已有临时原件上读取日期；不需要再次下载。索引保存 `captured_at`、`capture_time_source` 与检查状态，管理员面板显示识别数量及待补读数量。

旧文件由后台在导入空闲时补读；持续导入时也会在原文件已确认收件、下一个文件开始之前，定额补读一个历史文件，默认每 15 秒最多一次，可用 `CAPTURE_BACKFILL_INTERVAL_SECONDS` 调整。不会同时上传和取样，也不改变来源菜单。单次取样与解析限时 45 秒，失败不会直接使正常导入失败；限流则等待全局冷却。

优先处理最近打开详情的已完成文件夹。优先复用完整内容 SHA-256 相同、已由新版检查的已知日期；否则从原来源消息取文件头/尾，每端约 4 MiB，实际总量不超过 8.5 MiB，写入稀疏临时文件后立即清理。不会重新上传媒体，也不把整个大视频保存到磁盘；保留至少 10 GiB 空闲。检查版本 `capture_version=2`，旧版检查过的文件也会补读一轮。

如果日期位于取样范围之外、来源消息已失效、元数据已被删除或格式无法解析，该文件保持日期未知；不会伪造日期。设置 `CAPTURE_BACKFILL=0` 可关闭旧文件补读；新导入仍正常读取日期。管理员面板分别显示原始日期、仅创建时间、未知数量，以及仍待新版补读的数量。

Debian/Ubuntu 部署完整元数据引擎：`apt-get install -y --no-install-recommends libimage-exiftool-perl ffmpeg`。程序只传只读标签查询参数，不改写媒体。保留 ExifTool 默认扫描行为，以读取位于 QuickTime 媒体数据之后的相机日期标签；不使用会提前停止扫描的 `-fast2`。

日期补读是渐进的，补齐前日期未知的旧文件不会伪造日期。已经建立的发送计划不随日期更新重排，以免重复或跳漏；重新打开文件夹详情再点“查看内容”，会用当时已识别的日期建立新计划。

技术依据：[FFprobe 元数据输出](https://ffmpeg.org/ffprobe.html)、[Telethon 有界范围下载](https://docs.telethon.dev/en/stable/modules/client.html#telethon.client.downloads.DownloadMethods.iter_download)。

ExifTool 依据：[读取选项和扫描限制](https://exiftool.org/exiftool_pod.html)、[QuickTime 标签](https://exiftool.org/TagNames/QuickTime.html)、[原始日期与 GPS 合成标签](https://exiftool.org/TagNames/Composite.html)。
