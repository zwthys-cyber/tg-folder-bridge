# Sougabot edge service

部署、迁移、配额、当前兼容边界与回滚均见 [CLOUDFLARE.md](../CLOUDFLARE.md)。新增代码不会自动切换正在运行的机器人。

- `src/catalog_cache.js`：每次核对主库版本的有界公开快照缓存，不缓存用户偏好。
- `src/model.js`：展示顺序、原生媒体相册、详情和输入校验。
- `src/cover.js`：小型 JPEG/PNG 封面流式上传，2 MiB 硬上限，原件不变。
- `src/worker.js`：已认证的 Webhook、D1 目录和偏好、持久发送队列、私有 importer inbox、目录分阶段发布。
- `migrations/0001_edge.sql`：独立 D1 schema，不是原生产 SQLite 的整库复制。
- `test/worker.test.js`：Node 24 内置 SQLite 的集成测试，Telegram 网络仅使用可控桩；不调用真实机器人。
- `wrangler.jsonc`：无秘密模板，零 UUID 不是可部署的生产数据库 ID。
- `../edge_sync.py`：原 SQLite 只读导出与目录发布。
- `../edge_importer.py`：不轮询 Telegram 的 Python 转存入口。
- `../edge_admin.py`：显式配置 Webhook、查询入口、设置命令菜单。

CI 在 GitHub 安装固定锁文件依赖，执行 `npm run check`、`npm test`、`npm run build` 和 Wrangler 本地 D1 迁移。生产环境不运行这些验证。

`/health` 仅返回进程入口可达；私有 `/api/status` 才包含 active 版本与各事件状态计数。所有 `/api/*` 需要 Bearer `SYNC_TOKEN`；`/webhook` 需要 Telegram `secret_token` 头。不得把这些凭据写进 URL 或日志。
