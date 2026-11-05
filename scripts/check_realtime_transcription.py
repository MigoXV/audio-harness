"""使用内存静音检查标准转写提交；不写入录音或凭据。"""
import argparse
import asyncio
import base64
import json
import ssl
import time

import websockets


async def check(url, model, insecure):
    context = ssl._create_unverified_context() if insecure else ssl.create_default_context()
    for manual in (False, True):
        for aha in (False, True):
            uri = url.replace('https://', 'wss://').rstrip('/') + '/v1/realtime?intent=transcription'
            if aha:
                uri += '&x_aha=v1'
            async with websockets.connect(uri, ssl=context) as ws:
                await ws.recv()
                audio_input = {'format': {'type': 'audio/pcm', 'rate': 24000}, 'transcription': {'model': model}}
                if manual:
                    audio_input['turn_detection'] = None
                await ws.send(json.dumps({'type': 'session.update', 'session': {'type': 'transcription', 'audio': {'input': audio_input}}}))
                while True:
                    event = json.loads(await ws.recv())
                    if event['type'] in ('session.updated', 'error'):
                        break
                if event['type'] == 'error':
                    print(json.dumps(event, ensure_ascii=False))
                    continue
                for _ in range(10):
                    await ws.send(json.dumps({'type': 'input_audio_buffer.append', 'audio': base64.b64encode(bytes(4800)).decode()}))
                    await asyncio.sleep(.1)
                started = time.monotonic()
                await ws.send(json.dumps({'type': 'input_audio_buffer.commit', 'event_id': 'probe-commit'}))
                events = []
                try:
                    async def collect():
                        while True:
                            event = json.loads(await ws.recv())
                            if not event['type'].startswith('x_aha.'):
                                events.append({'ms': round((time.monotonic()-started)*1000), **event})
                            if event['type'] in ('error', 'conversation.item.input_audio_transcription.completed', 'conversation.item.input_audio_transcription.failed'):
                                return
                    await asyncio.wait_for(collect(), 6)
                    outcome = 'terminal'
                except asyncio.TimeoutError:
                    outcome = 'observation_timeout'
                print(json.dumps({'manual': manual, 'aha': aha, 'outcome': outcome, 'events': events}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url')
    parser.add_argument('--model', default='default-audio')
    parser.add_argument('--insecure', action='store_true', help='允许开发环境自签名证书')
    args = parser.parse_args()
    asyncio.run(check(args.url, args.model, args.insecure))
