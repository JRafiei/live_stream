import asyncio
from asyncpg import create_pool
from sanic import Sanic
from sanic.response import json, html

app = Sanic("live_stream")
app.static('/static', './static')


@app.listener('before_server_start')
async def register_db(app, loop):
    # Create a database connection pool
    conn = "postgres://{user}:{password}@{host}:{port}/{database}".format(
        user='postgres', password='Feanor90', host='localhost',
        port=5432, database='map_game'
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
        records = await connection.fetch('select * from plan')
        result = []
        for record in records:
            result.append(dict(record))
        return json({"result": result})


@app.route("/live/1")
async def test(request):
    with open('player.html') as f:
        content = f.read()
    return html(content)


@app.route("/live/add")
async def test(request):
    async with app.config.pg_pool.acquire() as connection:


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)