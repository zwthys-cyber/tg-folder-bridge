"""Read embedded camera dates without modifying media or using upload/mtime dates."""
import asyncio
import datetime as dt
import json
import os
import re
import shutil
import warnings
from pathlib import Path
from zoneinfo import ZoneInfo

IMAGES={'.jpg','.jpeg','.heic','.heif','.tif','.tiff','.png','.webp','.dng'}
VIDEOS={'.mp4','.mov','.m4v','.mkv','.avi','.webm'}
VERSION=2


def original_source(source):
    if source in {'exif.DateTimeOriginal','video.com.apple.quicktime.creationdate',
                  'video.datetimeoriginal','video.date_time_original'}:return True
    return isinstance(source,str) and source.startswith('exiftool.') and source.rsplit('.',1)[-1] in {
        'SubSecDateTimeOriginal','DateTimeOriginal','GPSDateTime','CreationDate'}


def choose_time(*results):
    valid=[r for r in results if r.get('captured_at')]
    return next((r for r in valid if original_source(r.get('capture_time_source'))),valid[0] if valid else {})


def normalize(value,source,offset=''):
    if isinstance(value,bytes):value=value.decode('utf-8','ignore')
    if not isinstance(value,str):return {}
    text=value.strip().strip('\x00')
    text=re.sub(r'^(\d{4}):(\d{2}):(\d{2})[ T]',r'\1-\2-\3T',text)
    if offset and not re.search(r'(Z|[+-]\d\d:?\d\d)$',text):
        text+=str(offset).strip().strip('\x00')
    try:
        date=dt.datetime.fromisoformat(text.replace('Z','+00:00'))
        assumed=date.tzinfo is None
        if assumed:date=date.replace(tzinfo=ZoneInfo(os.getenv('MEDIA_TIMEZONE','Asia/Shanghai')))
        date=date.astimezone(dt.timezone.utc)
        if source.endswith('creation_time') and date.isoformat() in {
            '1904-01-01T00:00:00+00:00','1970-01-01T00:00:00+00:00'}:return {}
        return dict(captured_at=date.isoformat().replace('+00:00','Z'),
                    capture_time_source=source,capture_timezone_assumed=assumed,
                    capture_time_kind='original' if original_source(source) else 'creation')
    except (ValueError,TypeError,OverflowError,KeyError):
        return {}


def from_probe(info):
    tags=[info.get('format',{}).get('tags',{})]
    tags += [s.get('tags',{}) for s in info.get('streams',[]) if s.get('codec_type') in {None,'video'}]
    tags=[{k.lower():v for k,v in tag.items()} for tag in tags]
    for key in ('com.apple.quicktime.creationdate','datetimeoriginal','date_time_original','creation_time'):
        for tag in tags:
            result=normalize(tag.get(key),'video.'+key)
            if result:return result
    return {}


def photo_date(path):
    from PIL import Image
    from pillow_heif import register_heif_opener
    register_heif_opener()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error',Image.DecompressionBombWarning)
            with Image.open(path) as image:
                exif=image.getexif()
                try:nested=exif.get_ifd(34665)
                except (KeyError,ValueError,TypeError):nested={}
                for tags in (nested,exif):
                    original=tags.get(36867)
                    if original:
                        result=normalize(original,'exif.DateTimeOriginal',tags.get(36881,'') or tags.get(36880,''))
                        if result:return result
    except Exception:
        pass
    return {}


def from_exiftool(info):
    """Only explicit original/GPS/camera-key dates; never filesystem timestamps."""
    if not isinstance(info,dict):return {}
    for name in ('SubSecDateTimeOriginal','DateTimeOriginal','CreationDate','GPSDateTime'):
        for key,value in info.items():
            group,_,tag=key.rpartition(':')
            if tag!=name or group in {'File','System'}:continue
            offset=info.get(group+':OffsetTimeOriginal','')
            if name=='GPSDateTime':offset='+00:00'
            result=normalize(value,'exiftool.'+group+'.'+tag,offset)
            if result:return result
    return {}


async def exiftool_date(path):
    executable=shutil.which('exiftool')
    if not executable:return {}
    from content_identity import capture
    # Do not use -fast2: QuickTime camera keys may follow the mdat atom.
    args=[executable,'-j','-G1','-s','-api','QuickTimeUTC=1',
          '-SubSecDateTimeOriginal','-DateTimeOriginal','-OffsetTimeOriginal',
          '-CreationDate','-GPSDateTime',str(Path(path).resolve())]
    try:
        data=await capture(args,timeout=15,maximum=1024**2)
        result=json.loads(data)
        return from_exiftool(result[0]) if isinstance(result,list) and result else {}
    except (OSError,ValueError,TypeError,asyncio.TimeoutError):return {}


async def inspect(path,probe_info=None):
    path=Path(path)
    if path.suffix.lower() not in IMAGES|VIDEOS:return {}
    extended=await exiftool_date(path)
    if extended:return extended
    if path.suffix.lower() in IMAGES:
        return await asyncio.to_thread(photo_date,path)
    if probe_info is not None:return from_probe(probe_info)
    probe=shutil.which('ffprobe')
    if path.suffix.lower() not in VIDEOS or not probe:return {}
    from content_identity import capture
    try:
        data=await capture([probe,'-v','error','-probesize','65536','-analyzeduration','1',
            '-show_entries','format_tags:stream=codec_type:stream_tags','-of','json',str(path)],timeout=15,maximum=1024**2)
        return from_probe(json.loads(data))
    except (OSError,ValueError,asyncio.TimeoutError):return {}


def epoch(item):
    value=item.get('captured_at')
    if not value or not original_source(item.get('capture_time_source')):return None
    try:
        date=dt.datetime.fromisoformat(value.replace('Z','+00:00'))
        if date.tzinfo is None:return None
        return date.timestamp()
    except (ValueError,TypeError,OverflowError):return None
