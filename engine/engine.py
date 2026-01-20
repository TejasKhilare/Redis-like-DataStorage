# Command Dispatcher

from engine.store import Store
from engine.ttl import ActiveExpiry

class Engine:
    def __init__(self):
        self.store=Store(capacity=3)
        self.expiry = ActiveExpiry(self.store)
        self.expiry.start()

    def execute(self,command,*args):
        command=command.upper()
        try:
            if command=="SET":
                key,value=args
                self.store.set(key,value)
                return {"status":"Ok"}
            elif command=="GET":
                key,=args
                value=self.store.get(key)
                return {"status":"Ok","value":value}
            elif command=="DEL":
                key,=args
                deleted=self.store.delete(key)
                return {"status":"Ok","deleted":deleted}
            elif command=="EXPIRE":
                key,seconds=args
                seconds=int(seconds)
                success=self.store.expire(key,seconds)
                return {"status":"Ok","success":success}
            elif command=="TTL":
                key,=args
                remaining_time=self.store.ttl(key)
                return {"status":"Ok","ttl":remaining_time}
            else:
                return {"status": "error", "message": "unknown command"}
        except ValueError:
            return {"status": "error", "message": "invalid arguments"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

 