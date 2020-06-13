import asyncio
import aiohttp
import m3u8
import os
import sys
import time

video_basepath = '/home/mohammad/Videos/stream_videos'
if not os.path.exists(video_basepath):
    os.makedirs(video_basepath)

WORKERS = 500


async def get_streams(index, stream_key, playlist_url):
    start = False
    downloaded = []
    timeout = aiohttp.ClientTimeout(total=10)
    session = aiohttp.ClientSession(timeout=timeout)
    while True:
        await asyncio.sleep(1)
        try:
            playlist = m3u8.load(playlist_url)
            if start is False:
                print(f'{index} got the playlist')
            start = True
            with open(f'{video_basepath}/{stream_key}-{index}.mp4', 'ab') as f:
                for segment in playlist.data['segments']:
                    if segment['uri'] not in downloaded:
                        url = playlist.base_uri + segment['uri']
                        async with session.get(url) as resp:
                            data = await resp.content.read()
                            f.write(data)
                        downloaded.append(segment['uri'])
        except m3u8.HTTPError:
            if start is True:
                print('Stream is finished!')
                await session.close()
                break
        except Exception as e:
            print(e)



loop = asyncio.get_event_loop()
stream_key = sys.argv[1]
playlist_url = f"http://localhost:8080/hls/{stream_key}.m3u8"
for i in range(1, WORKERS+1):
    loop.create_task(get_streams(i, stream_key, playlist_url))
loop.run_forever()