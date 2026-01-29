# These tests are well chosen
#They validate observable behavior, not implementation
# They will continue to pass even as the system grows
import time
from engine.engine import Engine

def test_set_get():
    e = Engine()
    e.execute("SET", "a", "1")
    assert e.execute("GET", "a")["value"] == "1"
    # assert is not magic and it is not a testing framework.
    # It is a runtime check that enforces an assumption.
    # assert condition
    # If condition is True → nothing happens
    # If condition is False → Python raises:AssertionError
    # e.g =x = 5
    # assert x > 0      # passes
    # assert x < 0      # raises AssertionError


def test_overwrite():
    e = Engine()
    e.execute("SET", "a", "1")
    e.execute("SET", "a", "2")
    assert e.execute("GET", "a")["value"] == "2"

def test_expiry():
    e = Engine()
    e.execute("SET", "a", "1")
    e.execute("EXPIRE", "a", 1)
    time.sleep(2)
    assert e.execute("GET", "a")["value"] is None

def test_ttl_no_key():
    e = Engine()
    assert e.execute("TTL", "x")["ttl"] == -2

def test_ttl_no_expiry():
    e = Engine()
    e.execute("SET", "a", "1")
    assert e.execute("TTL", "a")["ttl"] == -1

if __name__ == "__main__":
    print("Running engine tests...")
    test_set_get()
    test_overwrite()
    test_expiry()
    test_ttl_no_key()
    test_ttl_no_expiry()
    print("All engine tests passed ")
