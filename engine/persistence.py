# this is implementation for pesistance(survive process restart )
import threading
import time
class AOFLogger:
    def __init__(self,filename="appendonly.aof"):
        self.filename=filename
        self._lock=threading.Lock()

    def log(self,command,*args):
        line=" ".join([command]+list(map(str,args)))+"\n"
        with self._lock:
            with open(self.filename,"a") as f:
                f.write(line)

class AOFReplayer:
    def __init__(self,engine,filename="appendonly.aof"):
        self.engine=engine
        self.filename=filename

    def replay(self):
        try:
            with open(self.filename,"r") as f:
                for line in f:
                    parts=line.strip().split()
                    if not parts:
                        continue
                    command=parts[0]
                    args=parts[1:]
                    self.engine.execute(command,*args)
                    
        except FileNotFoundError:
            pass
        
