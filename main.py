import asyncio
from asyncpg import create_pool
from uuid import uuid4
from sanic import Sanic
from sanic.response import json, html

app = Sanic("live_stream")
app.static('/static', './static')


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


@app.route("/")
async def home(request):
    async with app.config.pg_pool.acquire() as connection:
        records = await connection.fetch('select * from stream')
        result = []
        for record in records:
            result.append(dict(record))
        return json({"result": result})


@app.route("/stream/<stream_id:int>")
async def view_stream(request, stream_id):
    user_id = int(request.args.get('uid'))
    allow = True
    async with app.config.pg_pool.acquire() as connection:
        stream = await connection.fetchrow(
            """
            select stream_key, private from stream
            where id = $1
            """,
            stream_id
        )
        if stream:
            if stream['private']:
                user_record = await connection.fetchrow(
                    """
                    select * from user_stream
                    where user_id = $1
                    and stream_id = $2
                    """,
                    user_id, stream_id
                )
                if not user_record:
                    allow = False
        else:
            allow = False
    
    if allow:
        with open('templates/player.html') as f:
            content = f.read()
            content = content.replace('~stream_key~', stream['stream_key'])
        return html(content)
    else:
        return html('Permission_denied')


@app.route("/stream/add", methods=["GET", "POST"])
async def add_show(request):
    if request.method == 'POST':
        user_id = request.json.get('user_id')
        wall_id = request.json.get('wall_id')
        private = request.json.get('private')
        async with app.config.pg_pool.acquire() as connection:
            record = await connection.fetchrow(
                """
                insert into stream
                (wall_id, stream_key, private)
                values
                ($1, $2, $3)
                returning id
                """,
                wall_id, uuid4().hex, private
            )
        await inform_followers(user_id, record['id'])
        return json({'stream_id': record['id']})
    else:
        with open('templates/add.html') as f:
            content = f.read()
            return html(content)


async def inform_followers(user_id, stream_id):
    followers = [1,4,8,32] # simulate get_followers
    values = [(follower, stream_id) for follower in followers]
    async with app.config.pg_pool.acquire() as connection:
        record = await connection.executemany(
            """
            insert into user_stream
            (user_id, stream_id)
            values
            ($1, $2)
            """,
            values
        )

    


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)