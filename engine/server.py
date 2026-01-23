# TCP Server

import asyncio
from engine.engine import Engine
from engine.protocol import decode_request,encode_response
import os
PORT = int(os.getenv("PORT", 6379))

engine=Engine()

async def handle_client(reader,writer):
    addr=writer.get_extra_info("peername")
    print(f"Client connected:{addr}")

    while True:
        data=await reader.readline()
        if not data:
            break
        line=data.decode().strip()
        if not line:
            continue
        request=decode_request(line)
        if not request:
            writer.write(encode_response({
                "status": "error",
                "message": "invalid json"
            }).encode())
            await writer.drain()
            continue
        try:
            command=request["command"]
            key=request.get("key")
            if command == "SET":
                print(f"[SHARD {PORT}] storing key = {key}")
                result = engine.execute(command, key, request["value"])
            elif command == "GET":
                result = engine.execute(command, key)
            elif command == "DEL":
                result = engine.execute(command, key)
            elif command == "TTL":
                result = engine.execute(command, key)
            elif command == "EXPIRE":
                result = engine.execute(command, key, request["seconds"])
            else:
                result = {"status": "error", "message": "unknown command"}

        except Exception as e:
            result = {"status": "error", "message": str(e)}
        
        writer.write(encode_response(result).encode())
        await writer.drain()

    writer.close()
    await writer.wait_closed()
    print(f"Client disconnected: {addr}")


async def start_server():
    
    server=await asyncio.start_server(handle_client,"127.0.0.1",PORT)
    print(f"Server running on port {PORT}")
    async with server:
        await server.serve_forever()






