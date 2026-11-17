"""Telegram-native folder lists and file albums."""
import re
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaDocument, InputMediaVideo


def resource_summary(stats):
    return (f'📊 资源统计\n'
            f'　• 文件夹：{stats["folders"]} 个\n'
            f'　• 消息：{stats["messages"]} 条\n'
            f'　• 图片：{stats["photos"]} 张\n'
            f'　• 视频：{stats["videos"]} 个')


def catalog_page(rows, page=1, page_callback_prefix='bridge_catalog', favorites=False):
    def number(row):
        match=re.search(r'(\d+)\s*$',row.get('title') or '')
        return (1,int(match[1])) if match else (0,0)
    # Sort before slicing pages; keep input order for equal/missing numbers.
    rows=sorted(rows,key=number,reverse=True)
    pages = max(1, (len(rows) + 9) // 10)
    page = max(1, min(page, pages))
    start = (page - 1) * 10
    selected = rows[start:start + 10]
    heading = '⭐ 我的收藏' if favorites else '📁 文件夹列表'
    body = [f'{heading}\n\n共 {len(rows)} 个文件夹（第 {page}/{pages} 页）']
    buttons = []
    for i, row in enumerate(selected, 1):
        title = row['title'] or ('文件夹 ' + row['id'][:8])
        pending = ('，内容核验中' if row.get('audit_public') else
                   '，导入未完成' if row.get('state', 'complete') != 'complete' else '')
        body.append(f"{i}. 📁 {title}（{row['file_count']} 个文件{pending}）")
    for offset in range(0, len(selected), 5):
        buttons.append([InlineKeyboardButton(str(offset + i + 1),
                        callback_data=f"bridge_open:{row['id']}:1")
                        for i, row in enumerate(selected[offset:offset + 5])])
    navigation = []
    if page > 1:
        navigation.append(InlineKeyboardButton('⬅️ 上一页', callback_data=f'{page_callback_prefix}:{page-1}'))
    if pages > 1:
        navigation.append(InlineKeyboardButton(f'📍 {page}/{pages} 跳转', callback_data=f'bridge_jump:{page_callback_prefix}'))
    if page < pages:
        navigation.append(InlineKeyboardButton('下一页 ➡️', callback_data=f'{page_callback_prefix}:{page+1}'))
    if navigation:
        buttons.append(navigation)
    if not rows:
        body.append('你还没有收藏文件夹。' if favorites else '暂时没有已导入完成的文件夹，请稍后查看。')
    if favorites:
        buttons.append([InlineKeyboardButton('📁 文件夹列表', callback_data='bridge_catalog:1')])
    return '\n\n'.join(body), InlineKeyboardMarkup(buttons)


def folder_details(job, items, starting=True):
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
        # Use the same binary-size display convention as Telegram's files.
        divisor, unit = (1024**3, 'GB') if size >= 1024**3 else (1024**2, 'MB')
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
            + ('⏳ 开始发送内容...' if starting else '👇 点击下方按钮查看内容'))


def favorite_button(job, saved=False):
    return InlineKeyboardButton('⭐ 已收藏' if saved else '☆ 收藏',
        callback_data=f"{'bridge_unfav' if saved else 'bridge_fav'}:{job['id']}")


async def send_folder_card(bot, chat, job, items, photo=None, favorite=False, bookmark=None):
    rows = [
        [InlineKeyboardButton('查看内容', callback_data=f"bridge_view:{job['id']}:1")],
        [favorite_button(job,favorite),InlineKeyboardButton('⭐ 我的收藏',callback_data='bridge_favorites:1')],
        [InlineKeyboardButton('文件夹列表', callback_data='bridge_catalog:1')]]
    if bookmark:
        page = min(max(1, bookmark['page'] + 1), len(file_pages(items,compact=bool(bookmark.get('layout',0)))))
        rows.insert(1, [InlineKeyboardButton(f'▶️ 继续浏览（第 {page} 组）',
                        callback_data=f"bridge_page:{bookmark['session']}:{page}")])
    keyboard = InlineKeyboardMarkup(rows)
    text = folder_details(job, items, starting=False)
    if photo:
        return await bot.send_photo(chat_id=chat, photo=photo, caption=text,
                                    parse_mode=None, reply_markup=keyboard)
    return await bot.send_message(chat_id=chat, text=text, parse_mode=None, reply_markup=keyboard)


def file_pages(items, compact=True):
    groups = []
    for item in items:
        family = item['kind'] if item['kind'] in {'document', 'video'} else 'other'
        if (not groups or len(groups[-1]) == 10
                or family != (groups[-1][0]['kind'] if groups[-1][0]['kind'] in {'document', 'video'} else 'other')):
            groups.append([])
        groups[-1].append(item)
    if compact:
        i=0
        while i<len(groups):
            if len(groups[i])!=1 or len(groups)==1:
                i+=1
                continue
            left=i-1 if i else 0
            combined=groups[left]+groups[left+1]
            if len(combined)<=10:
                groups[left:left+2]=[combined]
                i=max(0,left)
            else:
                middle=(len(combined)+1)//2
                groups[left:left+2]=[combined[:middle],combined[middle:]]
                i=left+2
    return groups or [[]]


async def send_file_page(bot, chat, job, items, page, send_one, progress=None, favorite=False):
    groups = file_pages(items,compact=progress.compact if progress is not None else True)
    pages = len(groups)
    page = max(1, min(page, pages))
    title = job.get('title') or ('文件夹 ' + job['id'][:8])
    if (progress is not None and not progress.details_sent) or (progress is None and page == 1):
        await bot.send_message(chat, folder_details(job, items), parse_mode=None)
        if progress is not None:
            progress.record_details()
    selected = groups[page-1]
    album = []
    media_types = {'document': InputMediaDocument, 'video': InputMediaVideo}

    async def flush():
        if len(album) >= 2:
            media_type = media_types[album[0]['kind']]
            await bot.send_media_group(chat_id=chat, media=[media_type(
                media=x['file_id'], caption=x.get('caption', '')[:1024], parse_mode=None)
                for x in album])
        elif album:
            if album[0]['kind'] == 'document':
                await send_one(bot, chat, album[0], use_preview=False)
            else:
                await send_one(bot, chat, album[0])
        if album and progress is not None:
            progress.record(album)
        album.clear()

    for item in selected:
        if item['kind'] in media_types:
            if album and album[0]['kind'] != item['kind']:
                await flush()
            album.append(item)
        else:
            await flush()
            await send_one(bot, chat, item)
            if progress is not None:
                progress.record([item])
    await flush()
    nav = []
    prefix = f'bridge_page:{progress.id}' if progress is not None else f"bridge_view:{job['id']}"
    if page > 1:
        nav.append(InlineKeyboardButton('⬅️ 上一组', callback_data=f'{prefix}:{page-1}'))
    if page < pages:
        nav.append(InlineKeyboardButton('下一组 ➡️', callback_data=f'{prefix}:{page+1}'))
    keyboard = ([nav] if nav else []) + [
        [InlineKeyboardButton('📁 文件夹详情',callback_data=f"bridge_open:{job['id']}:1"),favorite_button(job,favorite)],
        [InlineKeyboardButton('⭐ 我的收藏',callback_data='bridge_favorites:1'),
         InlineKeyboardButton('📁 文件夹列表',callback_data='bridge_catalog:1')]]
    text = f'📁 {title}\n共 {len(items)} 个文件（第 {page}/{pages} 组）'
    if progress is not None:
        received = progress.received_count(items)
        if received == len(items):
            text = f'✅ 文件夹内容发送完成\n\n共发送 {progress.message_count} 条消息'
        elif page == pages:
            text = f'✅ 当前组发送完成\n\n已发送 {received} / {len(items)} 个文件\n共发送 {progress.message_count} 条消息'
    await bot.send_message(chat, text,
                           reply_markup=InlineKeyboardMarkup(keyboard))
    if progress is not None:
        progress.bookmark(page)
