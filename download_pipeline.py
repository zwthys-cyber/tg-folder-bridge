"""At most one foreground file and one look-ahead download, no upload queue."""
import asyncio
from contextlib import asynccontextmanager


def source_key(message):
    return message.chat_id, message.id


class DownloadPipeline:
    def __init__(self, messages, allowed, factory):
        self.messages, self.allowed, self.factory = messages, set(allowed), factory
        self.positions = {source_key(m): i for i, m in enumerate(messages)}
        self.pending = None

    def contains(self, message):
        return source_key(message) in self.allowed

    def _start(self, message):
        slot = dict(key=source_key(message), stats={}, callback=None)
        def progress(current, total):
            if slot['callback']:
                slot['callback'](current, total)
        slot['context'] = self.factory(message, slot['stats'], progress)
        slot['task'] = asyncio.create_task(slot['context'].__aenter__())
        return slot

    @asynccontextmanager
    async def stage(self, message, use_stats, progress):
        key = source_key(message)
        slot, self.pending = self.pending, None
        if slot is not None and slot['key'] != key:
            self.pending = slot
            await self.close()
            slot = None
        if slot is None:
            slot = self._start(message)
        use_stats(slot['stats'])
        slot['callback'] = progress
        entered = False
        released = False
        try:
            path = await slot['task']
            entered = True
            index = self.positions[key]+1
            if index < len(self.messages):
                following = self.messages[index]
                if self.contains(following):
                    self.pending = self._start(following)
            yield path
        except BaseException as error:
            if entered:
                released = True
                # The spool needs to distinguish an interrupted upload from
                # normal consumption or unused look-ahead cleanup.
                await slot['context'].__aexit__(type(error), error, error.__traceback__)
            raise
        finally:
            slot['callback'] = None
            if entered and not released:
                await slot['context'].__aexit__(None, None, None)
            elif not entered:
                if not slot['task'].done():
                    slot['task'].cancel()
                result = (await asyncio.gather(slot['task'], return_exceptions=True))[0]
                if not isinstance(result, BaseException):
                    await slot['context'].__aexit__(None, None, None)

    async def close(self):
        slot, self.pending = self.pending, None
        if slot is None:
            return
        if not slot['task'].done():
            slot['task'].cancel()
        result = (await asyncio.gather(slot['task'], return_exceptions=True))[0]
        if not isinstance(result, BaseException):
            await slot['context'].__aexit__(None, None, None)
