import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from source_recovery import open_menu_content


class Recovery(unittest.IsolatedAsyncioTestCase):
    async def test_observed_menu_recovers_partial_folder_and_excludes_cover(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = Path(temporary) / 'collector.sqlite3'
            db = sqlite3.connect(ledger)
            db.execute('create table items(source,item_key,job,link)')
            link = 'https://t.me/epan?start=Token'
            key = b'folder_preview_list/7/1/list'.hex()
            db.execute('insert into items values(?,?,?,?)', ('spxm111_bot', key, 'job', link))
            db.commit()
            db.close()
            directory = NS(text='文件夹列表', data=b'folders', url=None)
            folder = NS(text='1', data=b'folder_preview_list/7/1/list', url=None)
            view = NS(text='查看内容', data=b'folder_content/7/list', url=None,
                      click=AsyncMock())
            home = NS(id=11, buttons=[[directory]])
            panel = NS(id=12, raw_text='共 1 个文件夹（第 1/1 页）', buttons=[[folder]])
            detail = NS(id=13, raw_text='📁 测试\n图片数：1 张\n视频数：2 个\n分享链接：\n'+link,
                        buttons=[[view]], entities=[])
            client = NS(get_entity=AsyncMock(return_value=NS(id=101, bot=True)),
                        get_messages=AsyncMock(side_effect=[[NS(id=10)], [home], [NS(id=20)]]),
                        send_message=AsyncMock())
            store = NS(files=Mock(return_value=[NS(source_message=3)]), set_title=Mock(), set_folder_metadata=Mock())
            job = dict(id='job', chats=set(), baselines={}, expected=3)

            async def click(message, data, predicate):
                result = panel if message is home else detail
                self.assertTrue(predicate(result))
                return result

            with patch('source_recovery.Catalog.click', side_effect=click):
                recovered = await open_menu_content(client, store, job, link,
                                                     {'epan', 'spxm111_bot'}, ledger)
            self.assertTrue(recovered)
            self.assertEqual(job['baselines'], {101: 20})
            self.assertEqual(job['expected'], 3)
            self.assertTrue(job['menu_recovery'])
            view.click.assert_awaited_once()

    async def test_shifted_folder_is_found_through_observed_adjacent_page_button(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = Path(temporary)/'collector.sqlite3'
            with sqlite3.connect(ledger) as db:
                db.execute('create table items(source,item_key,job,link)')
                link = 'https://t.me/epan?start=Token'
                db.execute('insert into items values(?,?,?,?)',
                    ('spxm111_bot',b'folder_preview_list/7/1/list'.hex(),'job',link))
            directory = NS(text='文件夹列表',data=b'folders',url=None)
            old_folder = NS(text='1',data=b'folder_preview_list/8/1/list',url=None)
            next_page = NS(text='下一页',data=b'folders_list_page/2',url=None)
            moved = NS(text='1',data=b'folder_preview_list/7/2/list',url=None)
            view = NS(text='查看内容',data=b'view',url=None,click=AsyncMock())
            home = NS(id=11,buttons=[[directory]])
            first = NS(id=12,raw_text='共 11 个文件夹（第 1/2 页）',buttons=[[old_folder],[next_page]])
            second = NS(id=12,raw_text='共 11 个文件夹（第 2/2 页）',buttons=[[moved]])
            detail = NS(id=13,raw_text='📁 测试\n图片数：1 张\n分享链接：\n'+link,buttons=[[view]],entities=[])
            client = NS(get_entity=AsyncMock(return_value=NS(id=101,bot=True)),
                get_messages=AsyncMock(side_effect=[[NS(id=10)],[home],[NS(id=20)]]),
                send_message=AsyncMock())
            store = NS(set_title=Mock(),set_folder_metadata=Mock())
            async def click(message,data,predicate):
                if message is home:result = first
                elif data == next_page.data:result = second
                else:
                    self.assertIs(message,second)
                    self.assertEqual(data,moved.data)
                    result = detail
                self.assertTrue(predicate(result))
                return result
            job = dict(id='job',expected=1,chats=set(),baselines={})
            with patch('source_recovery.Catalog.click',side_effect=click):
                self.assertTrue(await open_menu_content(client,store,job,link,
                    {'epan','spxm111_bot'},ledger))
            view.click.assert_awaited_once()

    async def test_jump_ignores_stale_matching_page_and_replies_to_current_prompt(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = Path(temporary)/'collector.sqlite3'
            with sqlite3.connect(ledger) as db:
                db.execute('create table items(source,item_key,job,link)')
                link = 'https://t.me/epan?start=Token'
                db.execute('insert into items values(?,?,?,?)',
                    ('spxm111_bot',b'folder_preview_list/7/2/list'.hex(),'job',link))
            directory = NS(text='文件夹列表',data=b'folders',url=None)
            jump = NS(text='跳转',data=b'jump',url=None)
            folder = NS(text='1',data=b'folder_preview_list/7/2/list',url=None)
            home = NS(id=10,buttons=[[directory]])
            first = NS(id=11,raw_text='共 11 个文件夹（第 1/2 页）',buttons=[[folder],[jump]])
            prompt = NS(id=12,raw_text='请输入要跳转的页码',buttons=[])
            stale = NS(id=4,raw_text='共 11 个文件夹（第 2/2 页）',buttons=[[folder]])
            current = NS(id=20,raw_text=stale.raw_text,buttons=[[folder]])
            view = NS(text='查看内容',data=b'view',url=None,click=AsyncMock())
            detail = NS(id=21,raw_text='📁 测试\n图片数：1 张\n分享链接：\n'+link,buttons=[[view]],entities=[])
            sent = []
            async def send(peer,text,**kwargs):
                sent.append((text,kwargs));return NS(id=6 if text=='/start' else 15)
            async def messages(peer,**kwargs):
                if kwargs.get('ids')==prompt.id:return prompt
                if kwargs.get('limit')==1:return [NS(id=30 if len(sent)==2 else 5)]
                return [home] if len(sent)<2 else [stale,current]
            client = NS(get_entity=AsyncMock(return_value=NS(id=101,bot=True)),
                get_messages=AsyncMock(side_effect=messages),send_message=AsyncMock(side_effect=send))
            async def click(message,data,predicate):
                if message is home:result=first
                elif message is first:result=prompt
                else:
                    self.assertIs(message,current);result=detail
                self.assertTrue(predicate(result));return result
            store = NS(set_title=Mock(),set_folder_metadata=Mock())
            with patch('source_recovery.Catalog.click',side_effect=click):
                self.assertTrue(await open_menu_content(client,store,
                    dict(id='job',expected=1,chats=set(),baselines={}),link,
                    {'epan','spxm111_bot'},ledger))
            self.assertEqual(sent[1],('2',{'reply_to':prompt.id}))

    async def test_missing_ledger_does_not_touch_source(self):
        client = NS(get_entity=AsyncMock())
        with tempfile.TemporaryDirectory() as temporary:
            self.assertFalse(await open_menu_content(client, NS(), {'id': 'job'},
                'https://t.me/epan?start=Token', {'epan'}, Path(temporary)/'absent.sqlite3'))
        client.get_entity.assert_not_awaited()
