import asyncio
import json
import logging
import ssl
import time
from asyncpg import create_pool
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaBlackhole, MediaPlayer, MediaRecorder
from queue import Queue
from sanic import Sanic
from sanic.response import json as json_response, html
from sanic.websocket import WebSocketProtocol
from uuid import uuid4
import cv2
from av import VideoFrame


context = ssl.create_default_context(purpose=ssl.Purpose.CLIENT_AUTH)
context.load_cert_chain("ssl_keys/selfsigned.crt", keyfile="ssl_keys/selfsigned.key")


app = Sanic("live_stream")
app.static('/static', './static')
logger = logging.getLogger("pc")

pcs = set()


class VideoTransformTrack(MediaStreamTrack):
    """
    A video stream track that transforms frames from an another track.
    """

    kind = "video"

    def __init__(self, track, transform):
        super().__init__()  # don't forget this!
        self.track = track
        self.transform = transform

    async def recv(self):
        frame = await self.track.recv()

        if self.transform == "cartoon":
            img = frame.to_ndarray(format="bgr24")

            # prepare color
            img_color = cv2.pyrDown(cv2.pyrDown(img))
            for _ in range(6):
                img_color = cv2.bilateralFilter(img_color, 9, 9, 7)
            img_color = cv2.pyrUp(cv2.pyrUp(img_color))

            # prepare edges
            img_edges = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
            img_edges = cv2.adaptiveThreshold(
                cv2.medianBlur(img_edges, 7),
                255,
                cv2.ADAPTIVE_THRESH_MEAN_C,
                cv2.THRESH_BINARY,
                9,
                2,
            )
            img_edges = cv2.cvtColor(img_edges, cv2.COLOR_GRAY2RGB)

            # combine color and edges
            img = cv2.bitwise_and(img_color, img_edges)

            # rebuild a VideoFrame, preserving timing information
            new_frame = VideoFrame.from_ndarray(img, format="bgr24")
            new_frame.pts = frame.pts
            new_frame.time_base = frame.time_base
            return new_frame
        elif self.transform == "edges":
            # perform edge detection
            img = frame.to_ndarray(format="bgr24")
            img = cv2.cvtColor(cv2.Canny(img, 100, 200), cv2.COLOR_GRAY2BGR)

            # rebuild a VideoFrame, preserving timing information
            new_frame = VideoFrame.from_ndarray(img, format="bgr24")
            new_frame.pts = frame.pts
            new_frame.time_base = frame.time_base
            return new_frame
        elif self.transform == "rotate":
            # rotate image
            img = frame.to_ndarray(format="bgr24")
            rows, cols, _ = img.shape
            M = cv2.getRotationMatrix2D((cols / 2, rows / 2), frame.time * 45, 1)
            img = cv2.warpAffine(img, M, (cols, rows))

            # rebuild a VideoFrame, preserving timing information
            new_frame = VideoFrame.from_ndarray(img, format="bgr24")
            new_frame.pts = frame.pts
            new_frame.time_base = frame.time_base
            return new_frame
        else:
            return frame


@app.listener('before_server_start')
async def register_db(app, loop):
    # Create a database connection pool
    conn = "postgres://{user}:{password}@{host}:{port}/{database}".format(
        user='postgres', password='secret', host='localhost',
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

    app.ws_connections = {}
    
    app.chats = {}

    # app.add_track_queue = Queue()


# async def notify_server_started_after_five_seconds(app):
#     while True:
#         if not app.add_track_queue.empty():
#             task = app.add_track_queue.get_nowait()
#             pc = task['pc']
#             peer = task['peer']
#             kind = task['kind']
#             if kind == 'video' and app.chats[peer]['video_track']:
#                 pc.addTrack(app.chats[peer]['video_track'])
#                 app.chats[peer]['video_track'] = None
#             elif kind == 'audio' and app.chats[peer]['audio_track']:
#                 pc.addTrack(app.chats[peer]['audio_track'])
#                 app.chats[peer]['audio_track'] = None
#             else:
#                 app.add_track_queue.put(task)
#         await asyncio.sleep(3)


async def feed(request, ws):
    while True:
        data = await ws.recv()
        data = json.loads(data)
        if data.get('type') == 'register':
            name = data.get('name')
            app.ws_connections[name] = ws
            app.chats[name] = {
                'audio_track': None, 'video_track': None,
                'peer': None, 'pc': None, 'role': None
            }


async def call(request):
    caller = request.json.get('from')
    callee = request.json.get('to')
    if callee in app.ws_connections:
        ws = app.ws_connections[callee]
        await ws.send(json.dumps({'type': 'call', 'from': caller, 'to': callee}))
        return json_response({'status': 'success'})
    return json_response({'status': 'error'})


async def call_response(request):
    caller = request.json.get('from')
    callee = request.json.get('to')
    stream_key = request.json.get('stream_key')
    caller_dict = app.chats[caller]
    callee_dict = app.chats[callee]
    if request.json.get('status'):
        caller_dict['peer'] = callee
        callee_dict['peer'] = caller
        caller_dict['role'] = 'caller'
        callee_dict['role'] = 'callee'

        ws = app.ws_connections[caller]
        await ws.send(json.dumps({
            'type': 'call_response', 'from': callee, 'to': caller, 'status': 'accepted'
        }))
        peer_stream_key = uuid4().hex[:8]
        async with app.config.pg_pool.acquire() as connection:
            qresult = await connection.execute(
                """
                update stream
                set peer_stream_key = $1
                where stream_key = $2
                """,
                peer_stream_key, stream_key
            )
        for ws in app.ws_connections.values():
            await ws.send(json.dumps({
                'type': 'peer-accept', 'peer_stream_key': peer_stream_key
            }))
        return json_response({'status': 'success', 'stream_key': peer_stream_key})
    else:
        await ws.send(json.dumps({
            'type': 'call_response', 'from': callee, 'to': caller, 'status': 'rejected'
        }))
        return json_response({'status': 'error'})


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
        content = content.replace('{{ws_url}}', request.host)
        return html(content)


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
        peer_stream_key = ''
        if stream['peer_stream_key']:
            peer_stream_key = stream['peer_stream_key']
        content = content.replace('{{peer_stream_key}}', peer_stream_key)
    return html(content)


async def offer(request):
    params = request.json
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])
    stream_key = params["stream_key"]

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
    # recorder = MediaRecorder(f'{this_user}.mp4')

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

        this_user = params["name"]
        this_user_dict = app.chats[this_user]
        peer = this_user_dict['peer']
        peer_dict = app.chats.get(peer)
        this_user_dict['pc'] = pc

        if track.kind == "audio":
            # pc.addTrack(track)
            recorder.addTrack(track)
            this_user_dict['audio_track'] = track
            if this_user_dict['role'] == 'callee':
                peer_dict['pc'].addTrack(track)
                this_user_dict['pc'].addTrack(peer_dict['audio_track'])
        elif track.kind == "video":
            transformed_track = VideoTransformTrack(
                track, transform=params["video_transform"]
            )
            recorder.addTrack(transformed_track)
            # pc.addTrack(track)
            this_user_dict['video_track'] = transformed_track
            if this_user_dict['role'] == 'callee':
                peer_dict['pc'].addTrack(transformed_track)
                this_user_dict['pc'].addTrack(peer_dict['video_track'])


        @track.on("ended")
        async def on_ended():
            if this_user_dict:
                this_user_dict['peer'] = None
            if peer_dict:
                peer_dict['peer'] = None
            log_info("Track %s ended", track.kind)
            await recorder.stop()

    # handle offer
    await pc.setRemoteDescription(offer)
    await recorder.start()

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
    app.add_route(offer, '/offer/', methods=["GET", "POST"])
    app.add_route(call, '/call/', methods=["GET", "POST"])
    app.add_route(call_response, '/call-response/', methods=["GET", "POST"])
    app.add_websocket_route(feed, '/ws')

    # app.add_task(notify_server_started_after_five_seconds)
    app.run(host="0.0.0.0", port=8443, ssl=context, workers=1)
