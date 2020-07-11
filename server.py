import asyncio
import logging
from asyncpg import create_pool
from uuid import uuid4
from sanic import Sanic
from sanic.response import json as json_response, html
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaBlackhole, MediaPlayer, MediaRecorder
from queue import Queue
import ssl
import time

context = ssl.create_default_context(purpose=ssl.Purpose.CLIENT_AUTH)
context.load_cert_chain("ssl_keys/selfsigned.crt", keyfile="ssl_keys/selfsigned.key")


app = Sanic("live_stream")
app.static('/static', './static')
logger = logging.getLogger("pc")

pcs = set()


@app.listener('before_server_start')
async def register_db(app, loop):
    # Create a database connection pool
    conn = "postgres://{user}:{password}@{host}:{port}/{database}".format(
        user='postgres', password='Feanor90', host='localhost',
        port=5432, database='live_stream'
    )
    app.config['pg_pool'] = await create_pool(
        dsn=conn,
        min_size=10, #in bytes,
        max_size=10, #in bytes,
        max_queries=50000,
        max_inactive_connection_lifetime=300,
        loop=loop
    )

    app.chats = {
        'a495fba2': {'audio_track': None, 'video_track': None, 'peer': '34551d92'},
        '34551d92': {'audio_track': None, 'video_track': None, 'peer': 'a495fba2'},
    }

    app.add_track_queue = Queue()


async def notify_server_started_after_five_seconds(app):
    while True:
        if not app.add_track_queue.empty():
            task = app.add_track_queue.get_nowait()
            pc = task['pc']
            peer = task['peer']
            kind = task['kind']
            if kind == 'video' and app.chats[peer]['video_track']:
                pc.addTrack(app.chats[peer]['video_track'])
                app.chats[peer]['video_track'] = None
            elif kind == 'audio' and app.chats[peer]['audio_track']:
                pc.addTrack(app.chats[peer]['audio_track'])
                app.chats[peer]['audio_track'] = None
            else:
                app.add_track_queue.put(task)
        await asyncio.sleep(3)



async def home(request):
    async with app.config.pg_pool.acquire() as connection:
        records = await connection.fetch('select * from stream')
        result = []
        for record in records:
            result.append(dict(record))
        return json_response({"result": result})


async def join_stream(request, stream_id):
    user_id = int(request.args.get('uid'))
    async with app.config.pg_pool.acquire() as connection:
        stream = await connection.fetchrow(
            """
            select * from stream
            where id = $1
            """,
            stream_id
        )
        if not stream:
            return html('Permission_denied')

    if stream['private']:
        if stream['wall_id']:
            # get wall followers
            followers = [1,3,6]
            if user_id not in followers:
                return html('Permission_denied')
        else:
            async with app.config.pg_pool.acquire() as connection:
                user_record = await connection.fetchrow(
                    """
                    select * from user_stream
                    where user_id = $1
                    and stream_id = $2
                    """,
                    user_id, stream_id
                )
                if not user_record:
                    return html('Permission_denied')

    with open('templates/player.html') as f:
        content = f.read()
        content = content.replace('{{stream_key}}', stream['stream_key'])
    return html(content)


async def add_stream(request):
    if request.method == 'POST':
        user_id = request.json.get('user_id')
        wall_id = request.json.get('wall_id')
        private = request.json.get('private')
        stream_key = uuid4().hex[:8]
        async with app.config.pg_pool.acquire() as connection:
            stream = await connection.fetchrow(
                """
                insert into stream
                (wall_id, stream_key, private)
                values
                ($1, $2, $3)
                returning *
                """,
                wall_id, stream_key, private
            )
        app.chats[stream_key] = {'audio_track': None, 'video_track': None, 'peer': None}
        await inform_followers(user_id, stream)
        return json_response({'stream_id': stream['id']})
    else:
        with open('templates/add.html') as f:
            content = f.read()
            return html(content)


async def send_stream(request, stream_id):
    async with app.config.pg_pool.acquire() as connection:
        stream = await connection.fetchrow(
            """
            select * from stream
            where id = $1
            """,
            stream_id
        )
        if not stream:
            return html('Permission_denied')
        stream_key = stream['stream_key']
    with open('templates/send.html') as f:
        content = f.read()
        content = content.replace('{{stream_key}}', stream['stream_key'])
        return html(content)


async def offer(request):
    params = request.json
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])
    stream_key = params["stream_key"]
    peer = app.chats[stream_key]['peer']

    pc = RTCPeerConnection()
    pc_id = "PeerConnection(%s)" % uuid4()
    pcs.add(pc)

    def log_info(msg, *args):
        logger.info(pc_id + " " + msg, *args)

    recorder = MediaRecorder(f'rtmp://localhost:1935/show/{stream_key}', format='flv', options={
        'framerate': '1',
        'video_size': '720x404', 
        'vcodec': 'libx264',
        'maxrate': '768k',
        'bufsize': '8080k',
        'vf': '"format=yuv420p"',
        'g': '60'
    })

    @pc.on("datachannel")
    def on_datachannel(channel):
        @channel.on("message")
        def on_message(message):
            if isinstance(message, str) and message.startswith("ping"):
                channel.send("pong" + message[4:])

    @pc.on("iceconnectionstatechange")
    async def on_iceconnectionstatechange():
        log_info("ICE connection state is %s", pc.iceConnectionState)
        if pc.iceConnectionState == "failed":
            await pc.close()
            pcs.discard(pc)

    @pc.on("track")
    def on_track(track):
        log_info("Track %s received", track.kind)

        if track.kind == "audio":
            app.chats[stream_key]['audio_track'] = track
            app.add_track_queue.put({'pc': pc, 'peer': peer, 'kind': track.kind})
            # pc.addTrack(track)
        elif track.kind == "video":
            # local_video = VideoTransformTrack(
            #     track, transform=params["video_transform"]
            # )
            app.chats[stream_key]['video_track'] = track
            app.add_track_queue.put({'pc': pc, 'peer': peer, 'kind': track.kind})
            # pc.addTrack(track)

            # recorder.addTrack(track)

        @track.on("ended")
        async def on_ended():
            log_info("Track %s ended", track.kind)
            await recorder.stop()

    # handle offer
    await pc.setRemoteDescription(offer)
    # await recorder.start()

    # send answer
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    print(pc.getTransceivers()[0].currentDirection)


    return json_response({
        "sdp": pc.localDescription.sdp,
        "type": pc.localDescription.type
    })



async def inform_followers(user_id, stream):
    if stream['wall_id']:
        # send message to wall's group
        pass
    else:
        if not stream['private']:
            # inform all users that have chat with this user
            pass


if __name__ == "__main__":
    app.add_route(home, '/')
    app.add_route(add_stream, '/stream/add', methods=["GET", "POST"])
    app.add_route(send_stream, '/stream/<stream_id:int>/send')
    app.add_route(join_stream, '/stream/<stream_id:int>')
    app.add_route(join_stream, '/offer/', methods=["GET", "POST"])

    app.add_task(notify_server_started_after_five_seconds)
    app.run(host="0.0.0.0", port=8443, ssl=context, workers=1)
