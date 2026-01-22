# Encode / Decode layer

import json
def decode_request(line:str):
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None
    
def encode_response(response:dict):
    return json.dumps(response)+"\n"
