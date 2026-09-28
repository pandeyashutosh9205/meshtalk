import socket
import threading
import time

BROADCAST_PORT = 50000
BROADCAST_MESSAGE = b"MESHTALK_HELLO"
ANNOUNCE_INTERVAL = 3  

def send_discovery_broadcast():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.sendto(BROADCAST_MESSAGE, ('255.255.255.255', BROADCAST_PORT))
    sock.close()

def announce_loop():
    while True:
        send_discovery_broadcast()
        print(f"[announce] sent broadcast")
        time.sleep(ANNOUNCE_INTERVAL)

def listen_for_broadcasts():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('', BROADCAST_PORT))
    print(f"[listen] listening on port {BROADCAST_PORT}...")

    while True:
        data, addr = sock.recvfrom(1024)
        print(f"[listen] received {data} from {addr}")

def start_peer():
    listener_thread = threading.Thread(target=listen_for_broadcasts, daemon=True)
    listener_thread.start()

    announce_loop()

if __name__ == "__main__":
    start_peer()