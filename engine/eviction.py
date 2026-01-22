# Evicts Least Recently used Key

from collections import OrderedDict

class LRUEviction:
    def __init__(self,capacity):
        self.capacity=capacity
        self.order=OrderedDict()
  
    def access(self,key):
        if key in self.order:
            self.order.move_to_end(key)

    def insert(self,key):
        self.order[key]=True
        self.order.move_to_end(key)

    def remove(self,key):
        self.order.pop(key,None)

    def evict_if_needed(self):
        if len(self.order)>self.capacity:
            self.order.popitem(last=False)[0]
        return None
