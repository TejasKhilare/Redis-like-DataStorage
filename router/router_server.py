import asyncio
import json
from router.consistent_hash import ConsistentHashRing

SHARDS = [
    ("127.0.0.1", 6379),
    ("127.0.0.1", 6380),
    ("127.0.0.1", 6381),
]

hash_ring = ConsistentHashRing(
    nodes=[f"{host}:{port}" for host, port in SHARDS]
)

async def forward_to_shard(request, shard):
    host, port = shard.split(":")
    reader, writer = await asyncio.open_connection(host, int(port))

    writer.write((json.dumps(request) + "\n").encode())
    await writer.drain()

    response = await reader.readline()
    writer.close()
    await writer.wait_closed()

    return json.loads(response.decode())

async def handle_client(reader, writer):
    addr = writer.get_extra_info("peername")
    print(f"Client connected: {addr}")

    while True:
        data = await reader.readline()
        if not data:
            break

        try:
            request = json.loads(data.decode())
            key = request.get("key")

            if not key:
                response = {"status": "error", "message": "key required"}
            else:
                shard = hash_ring.get_node(key)
                response = await forward_to_shard(request, shard)

        except Exception as e:
            response = {"status": "error", "message": str(e)}

        writer.write((json.dumps(response) + "\n").encode())
        await writer.drain()

    writer.close()
    await writer.wait_closed()
    print(f"Client disconnected: {addr}")

async def start_router():
    server = await asyncio.start_server(handle_client, "127.0.0.1", 7000)
    print("Shard Router running on port 7000")

    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    asyncio.run(start_router())
