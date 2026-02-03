🔴 Redis-Inspired Distributed In-Memory Datastore:
A Redis-inspired distributed in-memory key-value datastore built from scratch in Python, 
featuring TTL, LRU eviction, persistence, crash recovery, TCP networking, horizontal sharding 
with consistent hashing, and shard-failure handling — and integrated with a real Flask + React application.

✨ Features

📌Core Datastore:

=> In-memory key-value store

=> Supports strings, numbers, objects (JSON)

=> O(1) GET / SET

📌TTL & Expiry:

=> Per-key TTL

=> Active expiry thread

📌Redis-compatible TTL semantics:

=> -2 → key does not exist

=> -1 → no expiry

📌LRU Eviction:

=> Configurable capacity

=> Least-Recently-Used eviction

=> O(1) operations using ordered structures

📌Persistence & Recovery:

=> Append-Only File (AOF)

=> Write-ahead logging

=> Crash recovery via command replay

=> Per-shard persistence

📌Networking:

=> Raw TCP server

=> Custom newline-delimited JSON protocol

=> Async I/O using asyncio

📌Distributed Sharding:

=> Stateless router

=> Consistent hashing

=> Virtual nodes (replicas)

=> Even key distribution


⚖️ Performance Characteristics

📌Operation	    =>Complexity

GET / SET    =>         O(1)

TTL lookup	     =>      O(1)

LRU eviction	    =>     O(1)

Routing	       =>      O(log N)

▶️ How to Run (Windows / VS Code)

1️⃣ Start Shards (3 terminals)

python main.py

$env:PORT=6380; python main.py

$env:PORT=6381; python main.py


2️⃣ Start Router

python -m router.router_server

3️⃣ Run Real Client(Outside project anywhere)

python redis_client.py
