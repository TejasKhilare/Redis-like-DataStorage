# engine/persistence.py
# Append-Only File (AOF) persistence using JSON records

import threading
import json

class AOFLogger:
    def __init__(self, filename="appendonly.aof"):
        self.filename = filename
        self._lock = threading.Lock()

    def log(self, command, *args):
        record = {
            "command": command,
            "args": args
        }
        with self._lock:
            with open(self.filename, "a") as f:
                f.write(json.dumps(record) + "\n")


class AOFReplayer:
    def __init__(self, engine, filename="appendonly.aof"):
        self.engine = engine
        self.filename = filename

    def replay(self):
        try:
            with open(self.filename, "r") as f:
                for line in f:
                    record = json.loads(line)
                    command = record["command"]
                    args = record["args"]
                    self.engine.execute(command, *args)
        except FileNotFoundError:
            pass
