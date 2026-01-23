import hashlib
import bisect

class ConsistentHashRing:
    def __init__(self,nodes,replicas=100):
        self.replicas=replicas
        self.ring={}
        self.sorted_keys=[]
        for node in nodes:
            self.add_node(node)
        
    def _hash(self,key):
        return int(hashlib.md5(key.encode()).hexdigest(),16)
    
    def add_node(self,node):
        for i in range(self.replicas):
            h=self._hash(f"{node}-{i}")
            self.ring[h]=node
            self.sorted_keys.append(h)
        self.sorted_keys.sort()

    def get_node(self,key):
        if not self.ring:
            return None
        h=self._hash(key)
        idx=bisect.bisect(self.sorted_keys,h)
        if idx==len(self.sorted_keys):
            idx=0
        return self.ring[self.sorted_keys[idx]]
    