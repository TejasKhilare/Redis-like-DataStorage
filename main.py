# main.py
import asyncio
from engine.server import start_server

if __name__ == "__main__":
    try:
        asyncio.run(start_server())
    except KeyboardInterrupt:
        print("Server shutting down gracefully")































# from engine.engine import Engine
# import time

# engine = Engine()

# engine.execute("SET", "user", {
#     "id": 7,
#     "name": "Tejas Khilare",
#     "email": "tejas.khilare@example.com",
#     "phone": "9876543210",
#     "address": "Pune, Maharashtra, India",
#     "age": 22,
#     "role": "admin",
#     "profile_pic": "tejas_profile.jpg",
#     "document": "tejas_id_proof.pdf",
#     "created_at": "2025-01-12T10:45:30",
#     "updated_at": "2026-01-21T14:10:05"
# })


# print("Kill the process now and restart.")


# print(engine.execute("GET", "user"))     # must exist





# main.py
# import time
# from engine.engine import Engine

# print("=== STARTING ENGINE ===")
# engine = Engine()

# print("\n=== BASIC SET / GET ===")

# engine.execute("SET", "a", 10) # ok

# print(engine.execute("SET", "b", "2"))   
#   # ok
# print(engine.execute("GET", "a"))   
    # 1
# print(engine.execute("GET", "b"))          # 2

# print("\n=== TTL (LAZY + ACTIVE EXPIRY) ===")
# print(engine.execute("SET", "temp", "x"))
# print(engine.execute("EXPIRE", "temp", 2))
# print("TTL immediately:", engine.execute("TTL", "temp"))  # ~2
# time.sleep(3)

# # Key should be gone even without GET (active expiry)
# print("After sleep, GET:", engine.execute("GET", "temp"))  # None
# print("After sleep, TTL:", engine.execute("TTL", "temp"))  # -2

# print("\n=== LRU EVICTION TEST (capacity = 3) ===")
# engine.execute("SET", "k1", "v1")
# engine.execute("SET", "k2", "v2")
# engine.execute("SET", "k3", "v3")

# # Access k1 to make it most recently used
# engine.execute("GET", "k1")

# # Insert k4 → should evict k2 (least recently used)
# engine.execute("SET", "k4", "v4")

# print("k1:", engine.execute("GET", "k1"))  # exists
# print("k2:", engine.execute("GET", "k2"))  # should be None (evicted)
# print("k3:", engine.execute("GET", "k3"))  # exists
# print("k4:", engine.execute("GET", "k4"))  # exists

# print("\n=== DELETE TEST ===")
# engine.execute("DEL", "k3")
# print("k3 after delete:", engine.execute("GET", "k3"))  # None

# print("\n=== ENGINE STILL RUNNING (ACTIVE EXPIRY THREAD) ===")
# print("Sleeping to ensure no crash...")
# time.sleep(2)

# print("\n=== DONE ===")




