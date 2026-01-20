# Active Expiry thread

import threading
import time
import random


class ActiveExpiry:
    def __init__(self,store,interval=1,sample_size=5):
        self.store = store
        self.interval = interval
        self.sample_size = sample_size
        self._running = False
        self._thread = None

    def _run(self):
        while self._running:
            keys=list(self.store._data.keys())
            for key in random.sample(keys,min(len(keys),self.sample_size)):
                self.store.cleanup_if_expired(key)
            time.sleep(self.interval)
    
    def start(self):
        if not self._running:
            self._running=True
            self._thread=threading.Thread(target=self._run,daemon=True)
            self._thread.start()
    def stop(self):
        if self._running:
            self._running=False


