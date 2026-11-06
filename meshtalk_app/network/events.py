import queue

events = queue.Queue()


def emit(kind, **data):
    events.put({"kind": kind, **data})