"""Read a tiny original tail before spending bandwidth on a repair download."""
import asyncio
import time
from telethon import functions,types


async def probe_original_tail(client,message,stats):
    document=getattr(message,'document',None)
    if not isinstance(document,types.Document) or not document.size:
        return
    offset=(document.size-1)//4096*4096
    stats.update(transport='source_tail_probe',bytes=0,total=document.size,
                 workers=1,connections=1)
    began=time.monotonic();sender=None
    try:
        if document.dc_id!=client.session.dc_id:
            sender=await asyncio.wait_for(client._borrow_exported_sender(document.dc_id),20)
        location=types.InputDocumentFileLocation(document.id,document.access_hash,
                                                 document.file_reference,'')
        request=functions.upload.GetFileRequest(location,offset,4096,
                                               precise=True,cdn_supported=False)
        response=await asyncio.wait_for(client._call(sender or client._sender,request,
                                                      flood_sleep_threshold=0),20)
        if isinstance(response,types.upload.File):
            stats['bytes']=len(response.bytes)
        # A readable tail is not proof of full-file integrity. The normal
        # downloader and content checks remain mandatory after this probe.
    finally:
        stats['seconds']=time.monotonic()-began
        if sender is not None:
            await client._return_exported_sender(sender)
