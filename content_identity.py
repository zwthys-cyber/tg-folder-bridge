"""Content identity without transcoding or stripping original camera metadata."""
import asyncio
import hashlib
import json
import shutil
from pathlib import Path

VIDEO_EXTENSIONS = {'.mov', '.mp4', '.m4v', '.mkv', '.avi', '.webm'}


def raw_digest(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(4*1024**2), b''):
            digest.update(chunk)
    return digest.hexdigest()


async def capture(command, timeout=90, maximum=2*1024**2):
    process = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    async def read():
        data = bytearray()
        while True:
            block = await process.stdout.read(65536)
            if not block:
                break
            data.extend(block)
            if len(data) > maximum:
                raise ValueError('Probe output exceeds budget')
        code = await process.wait()
        if code:
            raise ValueError('Media probe failed')
        return bytes(data)
    try:
        return await asyncio.wait_for(read(), timeout)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise


async def analyze(path, native=False):
    path = Path(path)
    result = dict(content_sha256=await asyncio.to_thread(raw_digest, path),
                  stream_sha256='', quality=dict(native_source=bool(native), has_location=False,
                                                 metadata_available=False))
    probe = shutil.which('ffprobe')
    if path.suffix.lower() not in VIDEO_EXTENSIONS or not probe:
        return result
    try:
        info = json.loads(await capture([probe, '-v', 'error', '-show_streams', '-show_format',
                                        '-show_data_hash', 'sha256', '-of', 'json', str(path)], timeout=30))
        streams = [s for s in info.get('streams', []) if s.get('codec_type') in {'video', 'audio'}]
        video = next((s for s in streams if s.get('codec_type') == 'video'), None)
        if not video:
            return result
        tags = [info.get('format', {}).get('tags', {})] + [s.get('tags', {}) for s in streams]
        has_location = any(value and any(token in key.lower() for token in ('location', 'gps'))
                           for tag in tags for key, value in tag.items())
        result['quality'].update(metadata_available=True, has_location=has_location,
            metadata_fields=sum(len(tag) for tag in tags), width=video.get('width', 0),
            height=video.get('height', 0), codec=video.get('codec_name', ''),
            pixel_format=video.get('pix_fmt', ''), frame_rate=video.get('avg_frame_rate', ''),
            bitrate=video.get('bit_rate', 0), duration=video.get('duration', ''))
        # Decoder parameters and orientation are part of the identity. Equal
        # packet bytes with different codec headers must not be merged.
        keys = ('codec_type', 'codec_name', 'profile', 'width', 'height', 'pix_fmt',
                'sample_aspect_ratio', 'field_order', 'color_range', 'color_space',
                'color_transfer', 'color_primaries', 'sample_rate', 'channels',
                'channel_layout', 'extradata_hash', 'side_data_list', 'avg_frame_rate',
                'r_frame_rate', 'time_base', 'duration', 'duration_ts', 'nb_frames')
        configuration = [{k: s[k] for k in keys if k in s} for s in streams]
        from imageio_ffmpeg import get_ffmpeg_exe
        packet_hashes = await capture([shutil.which('ffmpeg') or get_ffmpeg_exe(), '-nostdin',
            '-v', 'error', '-i', str(path), '-map', '0:v:0', '-map', '0:a?', '-c', 'copy',
            '-f', 'streamhash', '-hash', 'sha256', '-'], timeout=120, maximum=65536)
        lines = packet_hashes.decode('ascii').strip().splitlines()
        if not lines or any('SHA256=' not in line for line in lines):
            return result
        signature = json.dumps(configuration, sort_keys=True, separators=(',', ':')).encode()+b'\n'+packet_hashes
        result['stream_sha256'] = hashlib.sha256(signature).hexdigest()
    except (OSError, ValueError, asyncio.TimeoutError):
        # Exact byte identity remains usable; unavailable probing must never
        # become evidence that two videos have the same content.
        pass
    return result


def preference(quality):
    return (bool(quality.get('has_location')), bool(quality.get('native_source')),
            int(quality.get('metadata_fields', 0)))
