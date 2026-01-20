# Data + metaData container

import time

class Entry:
    def __init__(self,value,expiry=None):
        self.value=value
        self.expiry=expiry
        self.created_at=time.time()
    
    def is_expired(self):
        if self.expiry==None:
            return False
        return time.time()>=self.expiry