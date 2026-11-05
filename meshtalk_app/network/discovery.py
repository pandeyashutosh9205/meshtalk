import socket
import threading
import time
import json
import uuid
import struct

BROADCAST_PORT = 50000
ANNOUNCE_INTERVAL = 3  
PEER_TIMEOUT = 10    

PEER_ID = uuid.uuid4().hex[:8]
PEER_NAME = socket.gethostname()

peers = {}             
peers_lock = threading.Lock()
TCP_PORT = None        


def create_tcp_server():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(('', 0))   
    server.listen()
    return server


def build_message():
    return json.dumps({
        "type": "HELLO",
        "id": PEER_ID,
        "name": PEER_NAME,
        "tcp_port": TCP_PORT,
    }).encode()


def send_discovery_broadcast():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.sendto(build_message(), ('255.255.255.255', BROADCAST_PORT))
    sock.close()


def update_peer(peer_id, name, ip, tcp_port):
    with peers_lock:
        is_new = peer_id not in peers
        peers[peer_id] = {
            "name": name,
            "ip": ip,
            "tcp_port": tcp_port,
            "last_seen": time.monotonic(),
        }
    if is_new:
        print(f"[peers] + joined: {peer_id} ({name}) at {ip}:{tcp_port}")


def prune_peers():
    now = time.monotonic()
    with peers_lock:
        stale = [pid for pid, info in peers.items()
                 if now - info["last_seen"] > PEER_TIMEOUT]
        for pid in stale:
            info = peers.pop(pid)
            print(f"[peers] - left: {pid} ({info['name']}) at {info['ip']}")


def print_peer_table():
    with peers_lock:
        print(f"[peers] online now: {len(peers)}")
        for pid, info in peers.items():
            age = time.monotonic() - info["last_seen"]
            print(f"         {pid}  {info['name']}  {info['ip']}:{info['tcp_port']}  (seen {age:.1f}s ago)")


def announce_loop():
    while True:
        send_discovery_broadcast()
        prune_peers()
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

        update_peer(msg.get("id"), msg.get("name"), addr[0], msg.get("tcp_port"))


MAX_FRAME = 1_000_000

def recv_exact(conn, n):
    """Read exactly n bytes, or return None if the peer closes early."""
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def handle_client(conn, addr):
    with conn:
        while True:
            header = recv_exact(conn, 4)
            if header is None:
                return                      # peer closed the connection
            (length,) = struct.unpack("!I", header)

            if length > MAX_FRAME:
                print(f"[tcp] rejected oversized frame ({length} bytes) from {addr[0]}")
                return

            body = recv_exact(conn, length)
            if body is None:
                return

            try:
                msg = json.loads(body.decode())
            except (json.JSONDecodeError, UnicodeDecodeError):
                print(f"[tcp] malformed message from {addr[0]}")
                return

            print(f"[chat] {msg.get('from_name')} ({msg.get('from')}): {msg.get('text')}")

def tcp_accept_loop(server):
    while True:
        conn, addr = server.accept()
        threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()


def send_message(peer_id, text):
    with peers_lock:
        info = peers.get(peer_id)

    if info is None:
        print(f"[send] unknown peer {peer_id}")
        return False

    envelope = {
        "type": "CHAT",
        "from": PEER_ID,
        "from_name": PEER_NAME,
        "msg_id": uuid.uuid4().hex[:8],
        "text": text,
    }
    body = json.dumps(envelope).encode()
    frame = struct.pack("!I", len(body)) + body

    try:
        with socket.create_connection((info["ip"], info["tcp_port"]), timeout=3) as s:
            s.sendall(frame)
        return True
    except OSError as e:
        print(f"[send] failed to reach {peer_id}: {e}")
        return False

def command_loop():
    print("commands: peers | send <peer_id> <text> | quit")
    while True:
        line = input("> ").strip()
        if line == "peers":
            print_peer_table()
        elif line.startswith("send "):
            parts = line.split(" ", 2)
            if len(parts) < 3:
                print("usage: send <peer_id> <text>")
                continue
            send_message(parts[1], parts[2])
        elif line == "quit":
            break
        elif line:
            print("unknown command")


def start_peer():
    global TCP_PORT
    tcp_server = create_tcp_server()
    TCP_PORT = tcp_server.getsockname()[1]
    threading.Thread(target=tcp_accept_loop, args=(tcp_server,), daemon=True).start()

    print(f"[peer] my id is {PEER_ID}, name {PEER_NAME}, tcp port {TCP_PORT}")

    threading.Thread(target=listen_for_broadcasts, daemon=True).start()
    threading.Thread(target=announce_loop, daemon=True).start()

    command_loop()


if __name__ == "__main__":
    start_peer()