#In-memory Storage level.....

from engine.entry import Entry
from engine.eviction import LRUEviction
import time

class Store:
    def __init__(self,capacity=100):
        self._data={}
        self.eviction=LRUEviction(capacity)

    def cleanup_if_expired(self,key):
        entry=self._data.get(key)
        if entry and entry.is_expired():
            self._data.pop(key, None)
            self.eviction.remove(key)
            return True
        return False
    
    def set(self,key,value):
        self._data[key]=Entry(value)
        self.eviction.insert(key)
        evicted=self.eviction.evict_if_needed()
        if evicted:
            self._data.pop(evicted,None)
        return True
    
    def get(self,key):
        if key not in self._data:
            return None
        if self.cleanup_if_expired(key):
            return None
        self.eviction.access(key)
        return self._data[key].value
    
    def delete(self,key):
        existed= self._data.pop(key,None) is not None
        self.eviction.remove(key)
        return existed
    
    def expire(self,key,seconds):
        if key not in self._data:
            return False
        if self.cleanup_if_expired(key):
            return False
        expiry_time=time.time()+seconds
        self._data[key].expiry=expiry_time
        return True
    
    def ttl(self,key):
        if key not in self._data:
            return -2  # key does not exist
        if self.cleanup_if_expired(key):
            return -2
        entry = self._data[key]
        if entry.expiry is None:
            return -1  # no expiry
        remaining = int(entry.expiry - time.time())
        return max(0, remaining)




    
    



