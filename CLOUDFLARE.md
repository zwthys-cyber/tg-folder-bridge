# Cloudflare 日常服务 + 按需转存

新增入口对应 Bridge 0.13.0。**代码与线上切换分开**：合并代码不会改 Telegram Webhook、停止旧程序或上传旧数据库。请先验证、迁移，再切换。已有 Python 单机模式继续支持。

## 分工

| 部分 | 负责的内容 | 停机后的影响 |
| --- | --- | --- |
| Workers + D1 + Queues | Telegram Webhook、资源统计、文件夹列表、搜索、详情封面、收藏、订阅、已保存媒体发送 | 日常服务的运行位置 |
| Python `edge_importer.py` | 管理员转存命令、来源读取、下载、原件上传、回执核对、同步公开目录 | 停止新转存；已同步目录仍可使用 |
| `collect_links.py` | 按原采集账本扫描、去重和提交分享 | 停止采集；不影响日常浏览 |
| GitHub | 代码、文档、CI、手动发布流程、现有加密备份 | 不负责永久运行转存程序 |

Worker 只向 Telegram 发送**同一个机器人**的 `file_id`，不下载、代理或转码照片和视频。不能换一个 Bot Token 后直接复用旧文件。已有聊天媒体不删除；已停止补收的任务不恢复、不重新上传。Cloudflare 不会解除 Telegram 限流，也不会加速来源文件下载。

### 保留的用户流程

- `/start`：资源统计 → 文件夹列表；名称末尾编号倒序，每页 10 个，按钮从 1 起。上下页同一行，跳转按钮通过回复页码完成；普通数字搜索仍是搜索。
- 选择编号：封面与详情在同一条消息；卡片成功后删除相应列表及资源统计。缺少可用 photo 类型封面时使用文字卡片，Worker 不临时下载原图。
- 查看内容：先带文件名的原文件，再其他媒体，最后原生视频；每类保持来源顺序，不按推断拍摄时间重排。相同类型最多 10 个组成相册，保留原 `file_id` 和媒体类型。
- 全部确认发送后显示完成及消息数（媒体条目 + 一条详情，完成提示不计入）。卡片重复点击不重复发送同一份；需再取一次时重新打开详情卡片。
- 按用户保存收藏；普通文件夹列表底部不添加“我的收藏”。`/favorites` 独立访问收藏。`/catalog` 不恢复。
- `/notifications` 只有开关；首次迁移不群发历史目录，后续新完成文件夹在目录同步后通知订阅者。
- `/admin` 展示日常服务、转存程序心跳和待处理指令；`/status`、`/failures` 等转存指令进入私有 inbox，需转存程序在线处理。

### 当前边界

这是可部署的新增运行模式，并非已经完成线上切换。旧资源链接库 `resources.db` 的 `/add`、`/delete` 和外部频道搜索仍属于原 sougabot；Worker 当前搜索**文件夹名称**。旧聊天中已经发出的原件取回、分组、管理员面板按钮不全部兼容，迁移后以新的 `/start` 与命令为入口。公开目录沿用原 Store.catalog 的可见范围；未公开的管理员私有任务不进入 D1。

转存模式必须改为 `UPLOAD_BACKEND=relay`、云端 `BOT_API_BASE`。当前混合上传依赖本地 Bot API，不能与云 Webhook 混用；<=2000 MB 的本地上传优化暂不适用于这个新入口。大文件由专用用户账号上传到原机器人，再经 Webhook 回执确认，原文件无需重新上传。

## 1. 在 GitHub 验证

`Check` 工作流覆盖 Python 3.11 / 3.12 / 3.13；`Edge service` 使用 Node 24，在 GitHub 执行 JS 语法检查、真实 SQLite 的接口/队列集成测试、Wrangler 打包、本地 D1 迁移烟测。生产转存环境不安装 Node 构建依赖、不跑测试。

测试包含身份验证、不同机器人拒绝迁移、不完整快照不发布、收藏迁移、初次不补发历史通知、编号排序、卡片与旧列表清理、重复点击、显式 429 重试和网络结果未知时不自动重发。

## 2. 创建 Cloudflare 资源

用你的 Cloudflare 账号授权 Wrangler；在开发机器或临时开发环境执行。不要把 API Token、Bot Token、登录验证码发到公开 Issue、日志或仓库。

```bash
cd edge
npm ci
npx wrangler login
npx wrangler d1 create sougabot-catalog
npx wrangler queues create sougabot-work
```

将返回的 D1 database_id、`OWNER_IDS`（逗号分隔）和 `RELAY_USER_ID` 写入**私有** `wrangler.production.jsonc`，以仓库 `wrangler.jsonc` 为模板。管理员、转存采集账号、回执账号配置要与旧环境核对，避免采集器指令被当成普通搜索。

```bash
npx wrangler d1 migrations apply sougabot-catalog --remote --config wrangler.production.jsonc
npx wrangler deploy --config wrangler.production.jsonc
npx wrangler secret put BOT_TOKEN --config wrangler.production.jsonc
npx wrangler secret put WEBHOOK_SECRET --config wrangler.production.jsonc
npx wrangler secret put SYNC_TOKEN --config wrangler.production.jsonc
```

- `BOT_TOKEN`：保持原机器人。
- `WEBHOOK_SECRET`：随机生成；Telegram Webhook 与 Worker 使用同一值。
- `SYNC_TOKEN`：独立随机生成；Python 端配置为 `EDGE_SYNC_TOKEN`，仅用于私有同步/inbox 接口。
- D1 和队列创建需要账号权限；尚未授权时仅能完成代码和 CI，不能声称已上线。

也可以先把下面两个 Cloudflare 凭据保存到 GitHub 仓库 Actions secrets，手动运行 `Provision edge resources`：它只创建或复用 `sougabot-catalog` / `sougabot-work`，返回不含密钥的 `edge-resource-ids` Artifact，避免在生产环境安装构建工具。把其中 database_id 保存为 `EDGE_DATABASE_ID`。

可在 GitHub 环境 `cloudflare` 添加 Secrets `CLOUDFLARE_API_TOKEN`、`CLOUDFLARE_ACCOUNT_ID`，Variables `EDGE_DATABASE_ID`、`EDGE_OWNER_IDS`、`EDGE_RELAY_USER_ID`。手动运行 `Deploy edge service` 在 GitHub 完成测试、D1 迁移和部署；机器人私密变量可用 Wrangler secrets 单独设置，或保存为 GitHub Secrets `EDGE_BOT_TOKEN`、`EDGE_WEBHOOK_SECRET`、`EDGE_SYNC_TOKEN`，由该工作流通过标准输入写入 Worker secrets，不输出值。**该工作流不会自动切 Telegram Webhook**。

## 3. 迁移目录与用户偏好

首次正式导入用户偏好前，先暂停旧程序的用户入口，冻结收藏/订阅写入，避免快照后仍有用户变更而遗漏。不要先发布一个空偏好的正式 active 版本，再尝试补种偏好。

在能够读取原 `index.sqlite3` 的私有环境设置 `EDGE_URL`、`EDGE_SYNC_TOKEN`、`BOT_TOKEN`。不要把导出的目录或明文数据库提交仓库。

```bash
python edge_admin.py identity
python edge_sync.py --database /绝对路径/index.sqlite3 --bot-id 实际机器人数字ID --seed-preferences
```

`edge_sync.py` 使用 SQLite 只读连接与一致读事务，直接复用 Store.catalog / Store.files，不触发 Store 构造函数的任务恢复或迁移。仅发送公开目录、原文件名、file_id、展示封面、统计字段，以及首次迁移的收藏和订阅；不包含来源 API Hash、relay.session、Token、原文件字节、异常重试表。

先创建 staging generation，逐文件夹写入，核对实际条数后原子更新 active。上传失败保持旧目录。首次允许导入用户偏好；一旦有 active 目录，后续同步拒绝覆盖 Cloudflare 上的收藏、订阅。发布时统计由 D1 重新计算，机器人 ID 必须匹配 Token。

单个文件夹最多 5000 个条目、JSON 最大约 900 KB，超限拒绝发布，不能截断内容冒充成功。较大目录必须在实际 Cloudflare 配额下验收 CPU、存储和查询耗时；SQLite 集成测试不等于已经证明免费生产额度足够。

## 4. 切换前与切换步骤

1. 保存当前加密恢复备份、Supervisor 配置和环境配置；确认没有进行中的上传、未确认发送或用户发送任务。76 个已停止补收任务保持停止。
2. 记录目录文件夹/媒体数量、抽查几个 file_id、收藏和订阅。Worker `/health` 只说明入口可达，**不代表完整业务验收**。
3. 停止旧 sougabot 的 getUpdates 消费者和采集器；不能让两个环境操作同一来源会话。按 Telegram 官方迁移规则关闭该机器人的本地 Bot API 实例。不要关闭同一服务器上其他机器人的服务。
4. 确认云 Bot API 的 `getMe` 与测试 file_id 请求可用；若先前调用过云端 `logOut`，遵守 Telegram 的 10 分钟重登约束，不循环重试。
5. 设置 Python 环境：

```dotenv
EDGE_MODE=1
EDGE_URL=https://你的-worker.workers.dev
EDGE_SYNC_TOKEN=与Worker的SYNC_TOKEN一致
BOT_API_BASE=https://api.telegram.org
UPLOAD_BACKEND=relay
```

原 `API_ID`、`API_HASH`、`OWNER_IDS`、`RELAY_USER_ID`、`DATA_DIR` 和原会话保持在私有环境。不要同时启动原 `main.py` / souga 轮询入口。

```bash
python edge_admin.py set-webhook
python edge_admin.py webhook-info
python edge_admin.py set-menu
python edge_importer.py
```

`WEBHOOK_SECRET` 也需在执行 set-webhook 的私有环境设置。不要使用 drop_pending_updates 清空未处理更新。然后按原流程启动采集器；它通过专用账号向机器人发指令，由 Worker 私有 inbox 转交 Python。

6. 验收 `/start`、翻页/跳转、封面卡片、收藏、整个文件夹发送、订阅、管理员 `/status` 和一个允许的新转存任务。再停止 Codespace 验证已有目录仍能使用，管理员面板显示转存离线。

已有私有 Supervisor runtime 可使用 `deploy/edge_runtime.py install --runtime /绝对路径/runtime` 安装转存入口；先停止旧 souga / collector，该工具会拒绝覆盖仍在运行的消费者，备份原配置和启动器，并保留所有数据库。安装后通过 Supervisor reread/update 启动；重启入口 `runtime/start.py` 保留新模式，原启动器在 `runtime/backups/edge-cutover`。该工具不会自行切 Webhook。

默认 importer 每小时检查一次公开目录变化，未变化不上传；**新增目录和通知最多会滞后一个同步周期**。初次手动迁移立即生效。当前采用完整目录快照，以减少实现歧义；大规模高频更新前应评估 D1 写入配额，再考虑增量版本。

## 5. 故障处理与配额

Webhook 先持久化再确认，队列只传事件 ID。后台按最多四次 Telegram 发送操作一批处理，D1 保留发送游标与结果。重复队列、重复更新和同一卡片重复点击不重复发送；确定被 Telegram 429 拒绝的操作按 retry_after 等待，成功操作复用已保存结果。

网络超时、服务端 5xx 或发送时进程中断不能证明“未送达”，标为 uncertain，不盲目重发媒体。用户收到核对上一批的按钮后，可明确确认已收到或未收到。通知发送结果未知不自动重复广播。`GET /api/status` 需要私有 Bearer Token，显示事件状态计数；不能把 `/health` 作为无异常证明。D1 仍保留未解决事件与发送记录。

私有 inbox 领取有 120 秒租约；Python 本地 `edge_updates` 记录处理状态，防止回执/管理员指令因 ACK 丢失被重复处理。处理时崩溃或异常的更新留作 uncertain，需查本地记录，不能据此自动重建转存任务。正常离线待办保留 7 天，超过 7 天的 inbox 不自动处理，需人工核查。Codespaces 停机前未完成的转存仍应按原恢复规则检查。

以下为 2026-11-24 查阅的免费额度，最终以账号控制台和官方文档为准：

| 项目 | 免费额度 / 约束 |
| --- | --- |
| Workers | 100,000 请求/日；10 ms CPU/次；128 MB；50 个外部子请求/次 |
| D1 | 5,000,000 行读/日；100,000 行写/日；单库 500 MB |
| Queues | 10,000 操作/日；一次正常消息约含写、读、删三次操作；免费保留 24 小时 |

因此 10,000 次队列操作不等于 10,000 个完整文件夹；Webhook、翻页、重试、目录同步、索引维护和用户发送都会消耗额度。免费方案没有无限用户或永不限流保证。旧目录版本通过 Cron 清理，发送计划带固定副本；已完成事件保留 7 天，未确认记录不自动丢弃。D1 sends、菜单、收藏等表需定期监控与备份，不能宣称永不增长。

参考：[Workers 限制](https://developers.cloudflare.com/workers/platform/limits/)、[D1 限制](https://developers.cloudflare.com/d1/platform/limits/)、[D1 价格](https://developers.cloudflare.com/d1/platform/pricing/)、[Queues 价格](https://developers.cloudflare.com/queues/platform/pricing/)、[Telegram 发送既有文件](https://core.telegram.org/bots/api#sending-files)、[Telegram 本地服务](https://github.com/tdlib/telegram-bot-api)。

## 6. 备份与回滚

Cloudflare D1 成为收藏、订阅和用户发送状态的权威源；原 Python SQLite 不再包含这些最新变更。应同时保留原索引加密备份与 D1 的私有导出，恢复时避免丢失用户选择：

```bash
npx wrangler d1 export sougabot-catalog --remote --output /私有目录/edge-d1.sql --config wrangler.production.jsonc
```

明文 D1 导出含 file_id 和用户 ID，必须加密后才可以放到备份位置。仓库已有加密备份不是新 D1 的自动备份，不能把旧“最近备份”时间当成新服务已备份。

回滚顺序：停止新 importer / 采集器 → `python edge_admin.py delete-webhook`（保留 pending）→ 私有导出 D1 偏好和状态 → 按 Telegram 迁移规则恢复原本地/云端 Bot API → 恢复原配置，确认只有一个旧消费者后再启动旧 sougabot。回滚代码不等于把 Cloudflare 上新增的收藏自动写回旧 SQLite，必须单独迁移这些用户数据。不要删除 D1 或旧库来排除重复。
