"""Pinned Telethon 1.45 adapter for independent, media-only TCP sessions.

No new login, authorization export, or changes to the on-disk login session.
Private SDK usage is contained here and checked by integration tests.
"""
import asyncio
import copy
import importlib.metadata
import time
from telethon import functions
from telethon.network import MTProtoSender
from telethon.tl.alltlobjects import LAYER


class UploadTimeoutError(ConnectionError):
    pass


class UploadTransport:
    def __init__(self, client, connections=2, scheduler='balanced'):
        if type(connections) is not int or not 2 <= connections <= 4:
            raise ValueError('Dedicated upload connections must be 2–4')
        if importlib.metadata.version('Telethon') != '1.45.0':
            raise RuntimeError('Dedicated uploads require Telethon 1.45.0')
        if scheduler not in {'balanced', 'round_robin'}:
            raise ValueError('Unknown upload scheduler')
        self.scheduler = scheduler
        self.client = client
        self.count = connections
        self.senders = []
        self.initialized = []
        self.locks = []
        self.dc_id = None
        self.inflight = []
        self.latencies = []
        self.cursor = 0

    async def prepare(self):
        client = self.client
        if self.senders and self.dc_id == client.session.dc_id:
            if all(sender.is_connected() for sender in self.senders):
                return
        await self.close()
        if not client._sender.auth_key or not client._sender.auth_key.key:
            raise ConnectionError('Main Telegram connection is not authorized')
        # GetConfig only runs on the control connection. Prefer the advertised
        # media endpoint of the SAME DC, where this authorization key is valid.
        fallback = await client._get_dc(client.session.dc_id)
        options = client.__class__._config.dc_options
        dc = next((x for x in options if x.id == client.session.dc_id
                   and x.media_only and not x.cdn
                   and bool(x.ipv6) == client._use_ipv6), fallback)
        self.dc_id = dc.id
        try:
            for _ in range(self.count):
                sender = MTProtoSender(client._sender.auth_key, loggers=client._log,
                    retries=1, auto_reconnect=False, connect_timeout=15)
                self.senders.append(sender)  # Also close a partially connected sender.
                await asyncio.wait_for(sender.connect(client._connection(
                    dc.ip_address, dc.port, dc.id, loggers=client._log,
                    proxy=client._proxy, local_addr=client._local_addr)), 20)
                self.initialized.append(False)
                self.locks.append(asyncio.Lock())
                self.inflight.append(0)
                self.latencies.append(0.0)
        except asyncio.TimeoutError:
            await self.close()
            raise UploadTimeoutError('Upload connection setup timed out') from None
        except BaseException:
            await self.close()
            raise

    async def send(self, request):
        if not isinstance(request, functions.upload.SaveBigFilePartRequest):
            raise TypeError('Only large-file upload parts may use media connections')
        if not self.senders or self.dc_id != self.client.session.dc_id:
            raise ConnectionError('Upload transport is not prepared for the current DC')
        if self.scheduler == 'round_robin':
            index = request.file_part % len(self.senders)
        else:
            index = min(range(len(self.senders)), key=lambda i: (
                self.inflight[i], self.latencies[i], (i-self.cursor) % len(self.senders)))
            self.cursor = (index+1) % len(self.senders)
        # Reserve without awaiting so other tasks observe the real lane load.
        loads = self.inflight
        loads[index] += 1
        began = time.monotonic()
        sender = self.senders[index]

        async def invoke():
            # Surface every RPC FloodWait to the shared persisted cooldown.
            # Using _call retains the SDK's error decoding and entity handling.
            return await self.client._call(sender, request, flood_sleep_threshold=0)

        try:
            if not self.initialized[index]:
                async with self.locks[index]:
                    if not self.initialized[index]:
                        query = copy.deepcopy(self.client._init_request)
                        query.query = functions.InvokeWithoutUpdatesRequest(request)
                        result = await asyncio.wait_for(self.client._call(sender,
                            functions.InvokeWithLayerRequest(LAYER, query),
                            flood_sleep_threshold=0), 60)
                        self.initialized[index] = bool(result)
                        return result
            # File-only senders have no updates queue. Explicit no-updates also
            # tells Telegram this session must never receive control updates.
            request = functions.InvokeWithoutUpdatesRequest(request)
            return await asyncio.wait_for(invoke(), 60)
        except asyncio.TimeoutError:
            raise UploadTimeoutError('Upload part did not finish within 60 seconds') from None
        finally:
            loads[index] -= 1
            if loads is self.inflight:
                elapsed = time.monotonic()-began
                previous = self.latencies[index]
                self.latencies[index] = .8*previous+.2*elapsed if previous else elapsed

    async def close(self):
        senders, self.senders = self.senders, []
        self.initialized, self.locks = [], []
        self.inflight, self.latencies = [], []
        self.dc_id = None
        if senders:
            await asyncio.gather(*(sender.disconnect() for sender in senders),
                                 return_exceptions=True)
