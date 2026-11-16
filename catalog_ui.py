"""Telegram-native folder lists and file albums."""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaDocument


def catalog_page(rows, page=1, page_callback_prefix='bridge_catalog'):
    pages = max(1, (len(rows) + 9) // 10)
    page = max(1, min(page, pages))
    start = (page - 1) * 10
    selected = rows[start:start + 10]
    body = [f'📁 文件夹列表\n\n共 {len(rows)} 个文件夹（第 {page}/{pages} 页）']
    buttons = []
    for i, row in enumerate(selected, start + 1):
        title = row['title'] or ('文件夹 ' + row['id'][:8])
        pending = ('，内容核验中' if row.get('audit_public') else
                   '，导入未完成' if row.get('state', 'complete') != 'complete' else '')
        body.append(f"{i}. 📁 {title}（{row['file_count']} 个文件{pending}）")
    for offset in range(0, len(selected), 5):
        buttons.append([InlineKeyboardButton(str(start + offset + i + 1),
                        callback_data=f"bridge_open:{row['id']}:1")
                        for i, row in enumerate(selected[offset:offset + 5])])
    navigation = []
    if page > 1:
        navigation.append(InlineKeyboardButton('⬅️ 上一页', callback_data=f'{page_callback_prefix}:{page-1}'))
    if page < pages:
        navigation.append(InlineKeyboardButton('下一页 ➡️', callback_data=f'{page_callback_prefix}:{page+1}'))
    if navigation:
        buttons.append(navigation)
    if not rows:
        body.append('暂时没有已导入完成的文件夹，请稍后查看。')
    return '\n\n'.join(body), InlineKeyboardMarkup(buttons)


def folder_details(job, items):
    title = job.get('title') or ('文件夹 ' + job['id'][:8])
    counts = dict(photo=0, video=0, document=0)
    for item in items:
        kind = item.get('source_kind') or item['kind']
        counts[kind if kind in counts else 'document'] += 1
    sizes = [item.get('file_size') for item in items]
    if any(size is None for size in sizes):
        total = '未知'
    else:
        size = sum(sizes)
        divisor, unit = (1_000_000_000, 'GB') if size >= 1_000_000_000 else (1_000_000, 'MB')
        total = f'{size/divisor:.2f} {unit}'
    # The source convention counts the folder information message as one
    # message in addition to its media entries; navigation is not included.
    return (f'📁 {title}\n\n📖 内容统计：\n'
            f'　• 消息数：{len(items)+1} 条\n'
            f'　• 图片数：{counts["photo"]} 张\n'
            f'　• 视频数：{counts["video"]} 个\n'
            f'　• 文档数：{counts["document"]} 个\n'
            f'　• 总大小：{total}\n\n'
            f'📅 创建时间：{job.get("source_created_at") or "未知"}\n'
            f'✏️ 最后更新：{job.get("source_updated_at") or "未知"}\n\n'
            '⏳ 开始发送内容...')


async def send_file_page(bot, chat, job, items, page, send_one):
    pages = max(1, (len(items) + 9) // 10)
    page = max(1, min(page, pages))
    title = job.get('title') or ('文件夹 ' + job['id'][:8])
    await bot.send_message(chat, folder_details(job, items), parse_mode=None)
    selected = items[(page-1)*10:page*10]
    documents = []

    async def flush():
        if len(documents) >= 2:
            await bot.send_media_group(chat_id=chat, media=[InputMediaDocument(
                media=x['file_id'], caption=x.get('caption', '')[:1024], parse_mode=None)
                for x in documents])
        elif documents:
            await send_one(bot, chat, documents[0], use_preview=False)
        documents.clear()

    for item in selected:
        if item['kind'] == 'document':
            documents.append(item)
        else:
            await flush()
            await send_one(bot, chat, item)
    await flush()
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton('⬅️ 上一组', callback_data=f"bridge_open:{job['id']}:{page-1}"))
    if page < pages:
        nav.append(InlineKeyboardButton('下一组 ➡️', callback_data=f"bridge_open:{job['id']}:{page+1}"))
    keyboard = ([nav] if nav else []) + [[InlineKeyboardButton('📁 返回文件夹列表', callback_data='bridge_catalog:1')]]
    await bot.send_message(chat, f'📁 {title}\n共 {len(items)} 个文件（第 {page}/{pages} 组）',
                           reply_markup=InlineKeyboardMarkup(keyboard))
