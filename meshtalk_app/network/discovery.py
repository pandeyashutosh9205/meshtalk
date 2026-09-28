import socket
import threading
import time
import json
import uuid

BROADCAST_PORT = 50000
ANNOUNCE_INTERVAL = 3  

PEER_ID = uuid.uuid4().hex[:8]   
PEER_NAME = socket.gethostname()

def build_message():
    return json.dumps({
        "type": "HELLO",
        "id": PEER_ID,
        "name": PEER_NAME,
    }).encode()

def send_discovery_broadcast():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.sendto(build_message(), ('255.255.255.255', BROADCAST_PORT))
    sock.close()

def announce_loop():
    while True:
        send_discovery_broadcast()
        print("[announce] sent broadcast")
        time.sleep(ANNOUNCE_INTERVAL)

def listen_for_broadcasts():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('', BROADCAST_PORT))
    print(f"[listen] listening on port {BROADCAST_PORT}...")

    while True:
        data, addr = sock.recvfrom(1024)

        try:
            msg = json.loads(data.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            print(f"[listen] ignored malformed packet from {addr}")
            continue

        if msg.get("id") == PEER_ID:
            continue  

        print(f"[listen] peer {msg.get('id')} ({msg.get('name')}) at {addr[0]}")

def start_peer():
    print(f"[peer] my id is {PEER_ID}, name {PEER_NAME}")

    listener_thread = threading.Thread(target=listen_for_broadcasts, daemon=True)
    listener_thread.start()

    announce_loop()

if __name__ == "__main__":
    start_peer()