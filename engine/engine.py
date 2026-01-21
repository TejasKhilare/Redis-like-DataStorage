# Command Dispatcher

from engine.store import Store
from engine.ttl import ActiveExpiry
from engine.persistence import AOFLogger,AOFReplayer
import time

class Engine:
    def __init__(self):
        self.is_replaying = True
        self.store=Store(capacity=3)
        self.expiry = ActiveExpiry(self.store)
        self.expiry.start()

        self.aof=AOFLogger()
        self.replayer=AOFReplayer(self)
        self.replayer.replay()#recovery on startup
        self.is_replaying = False

    def execute(self,command,*args):
        command=command.upper()
        
        if command in {"SET","DEL"}:
            if not self.is_replaying:
                self.aof.log(command,*args)

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
                expiry_ts=int(time.time())+seconds #for storing absolute timestamp in log
                if not self.is_replaying:
                    self.aof.log("EXPIREAT",key,expiry_ts)
                success=self.store.expire(key,expiry_ts)
                return {"status":"Ok","success":success}
            elif command=="TTL":
                key,=args
                remaining_time=self.store.ttl(key)
                return {"status":"Ok","ttl":remaining_time}
            elif command == "EXPIREAT":
                key, expiry_ts = args
                expiry_ts = int(expiry_ts)
    # if already expired at replay time → drop key
                if expiry_ts <= time.time():
                      self.store.delete(key)
                      return {"status": "Ok"}
                self.store.expire_at(key, expiry_ts)
                return {"status": "Ok"}

            else:
                return {"status": "error", "message": "unknown command"}
        except ValueError:
            return {"status": "error", "message": "invalid arguments"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

 