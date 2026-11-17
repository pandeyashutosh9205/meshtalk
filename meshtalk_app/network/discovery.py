import socket
import threading
import time
import json
import uuid
import struct

from meshtalk_app.network.events import emit, events

BROADCAST_PORT = 50000
ANNOUNCE_INTERVAL = 3   # seconds between announcements
PEER_TIMEOUT = 10       # drop a peer if not heard from for this long
MAX_FRAME = 1_000_000   # reject any single message larger than ~1 MB

PEER_ID = uuid.uuid4().hex[:8]
PEER_NAME = socket.gethostname()

peers = {}              # peer_id -> {"name", "ip", "tcp_port", "last_seen"}
peers_lock = threading.Lock()
groups = {}             # group_id -> {"name", "members", "owner"}
groups_lock = threading.Lock()
TCP_PORT = None         # set at startup once the OS gives us a free port


def create_tcp_server():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(('', 0))   # port 0 = "OS, pick any free port for me"
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
        emit("peer_joined", peer_id=peer_id, name=name, ip=ip, tcp_port=tcp_port)


def prune_peers():
    now = time.monotonic()
    left = []
    with peers_lock:
        stale = [pid for pid, info in peers.items()
                 if now - info["last_seen"] > PEER_TIMEOUT]
        for pid in stale:
            left.append((pid, peers.pop(pid)))
    for pid, info in left:
        emit("peer_left", peer_id=pid, name=info["name"], ip=info["ip"])


def get_peers_snapshot():
    """Return a copy of the peer table, safe to use outside the lock."""
    with peers_lock:
        return {pid: dict(info) for pid, info in peers.items()}


def announce_loop():
    while True:
        send_discovery_broadcast()
        prune_peers()
        time.sleep(ANNOUNCE_INTERVAL)


def listen_for_broadcasts():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('', BROADCAST_PORT))
    emit("log", text=f"listening for discovery on UDP port {BROADCAST_PORT}")

    while True:
        data, addr = sock.recvfrom(1024)

        try:
            msg = json.loads(data.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            emit("log", text=f"ignored malformed packet from {addr[0]}")
            continue

        if msg.get("id") == PEER_ID:
            continue

        update_peer(msg.get("id"), msg.get("name"), addr[0], msg.get("tcp_port"))


def recv_exact(conn, n):
    """Read exactly n bytes, or return None if the peer closes early."""
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf

def handle_group_create(msg):
    gid = msg.get("group_id")
    name = msg.get("group_name")
    members = msg.get("members")
    if not gid or not name or not isinstance(members, list):
        emit("log", text="ignored malformed group invite")
        return

    name = str(name)[:40]                       # cap sizes so a peer can't flood us
    members = [str(m) for m in members][:50]

    if PEER_ID not in members or msg.get("from") not in members:
        return                                  # not for us, or the creator isn't a member

    with groups_lock:
        if gid in groups:
            return                              # never let a repeat invite rewrite a group
        groups[gid] = {"name": name, "members": members, "owner": msg.get("from")}
    emit("group_created", group_id=gid, name=name, members=members, failed=[])


def handle_group_msg(msg):
    gid = msg.get("group_id")
    with groups_lock:
        group = groups.get(gid)
    if group is None:
        emit("log", text="ignored message for an unknown group")
        return
    if msg.get("from") not in group["members"]:
        emit("log", text=f"ignored group message from non-member {msg.get('from')}")
        return
    emit("group_msg", group_id=gid, sender_id=msg.get("from"),
         sender_name=msg.get("from_name"), text=msg.get("text"))

def handle_client(conn, addr):
    with conn:
        while True:
            header = recv_exact(conn, 4)
            if header is None:
                return
            (length,) = struct.unpack("!I", header)

            if length > MAX_FRAME:
                emit("log", text=f"rejected oversized frame ({length} bytes) from {addr[0]}")
                return

            body = recv_exact(conn, length)
            if body is None:
                return

            try:
                msg = json.loads(body.decode())
            except (json.JSONDecodeError, UnicodeDecodeError):
                emit("log", text=f"malformed message from {addr[0]}")
                return

            if not isinstance(msg, dict):
                return                          # valid JSON, but not a message object

            mtype = msg.get("type")
            if mtype == "GROUP_CREATE":
                handle_group_create(msg)
            elif mtype == "GROUP_MSG":
                handle_group_msg(msg)
            else:
                kind = "group_chat" if mtype == "GROUP" else "chat"
                emit(kind, sender_id=msg.get("from"), sender_name=msg.get("from_name"),
                     msg_id=msg.get("msg_id"), text=msg.get("text"))


def tcp_accept_loop(server):
    while True:
        conn, addr = server.accept()
        threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()

def _make_envelope(kind, text):
    return {
        "type": kind,
        "from": PEER_ID,
        "from_name": PEER_NAME,
        "msg_id": uuid.uuid4().hex[:8],
        "text": text,
    }


def _deliver(info, envelope):
    """Send one framed message to one peer. Returns None on success, or an error string."""
    body = json.dumps(envelope).encode()
    frame = struct.pack("!I", len(body)) + body
    try:
        with socket.create_connection((info["ip"], info["tcp_port"]), timeout=3) as s:
            s.sendall(frame)
        return None
    except OSError as e:
        return str(e)


def send_message(peer_id, text):
    with peers_lock:
        info = peers.get(peer_id)

    if info is None:
        emit("send_failed", peer_id=peer_id, name=peer_id, text=text,
             reason="peer is no longer online")
        return False

    error = _deliver(info, _make_envelope("CHAT", text))
    if error is None:
        emit("sent", peer_id=peer_id, name=info["name"], text=text)
        return True

    emit("send_failed", peer_id=peer_id, name=info["name"], text=text, reason=error)
    return False


def send_to_everyone(text):
    snap = get_peers_snapshot()
    envelope = _make_envelope("GROUP", text)   # one message, one msg_id, for everybody
    delivered, failed = [], []
    for pid, info in snap.items():
        if _deliver(info, envelope) is None:
            delivered.append(pid)
        else:
            failed.append(f"{info['name']} ({pid})")
    emit("group_sent", text=text, delivered=delivered, failed=failed)


def get_groups_snapshot():
    with groups_lock:
        return {gid: dict(g) for gid, g in groups.items()}


def create_group(name, member_ids):
    gid = uuid.uuid4().hex[:8]
    members = [PEER_ID] + [m for m in member_ids if m != PEER_ID]
    with groups_lock:
        groups[gid] = {"name": name, "members": members, "owner": PEER_ID}

    snap = get_peers_snapshot()
    failed = []
    for pid in members[1:]:
        info = snap.get(pid)
        if info is None:
            failed.append(pid)
            continue
        envelope = _make_envelope("GROUP_CREATE", "")
        envelope.update({"group_id": gid, "group_name": name, "members": members})
        if _deliver(info, envelope) is not None:
            failed.append(f"{info['name']} ({pid})")

    emit("group_created", group_id=gid, name=name, members=members, failed=failed)
    return gid


def send_group_message(group_id, text):
    with groups_lock:
        group = groups.get(group_id)
    if group is None:
        emit("log", text="that group no longer exists")
        return

    snap = get_peers_snapshot()
    envelope = _make_envelope("GROUP_MSG", text)
    envelope["group_id"] = group_id

    delivered, failed = [], []
    for pid in group["members"]:
        if pid == PEER_ID:
            continue
        info = snap.get(pid)
        if info is None:
            failed.append(pid)                  # member is offline
        elif _deliver(info, envelope) is None:
            delivered.append(pid)
        else:
            failed.append(f"{info['name']} ({pid})")

    emit("group_msg_sent", group_id=group_id, text=text,
         delivered=delivered, failed=failed)
def start_network():
    """Start all background network threads. Returns immediately."""
    global TCP_PORT
    tcp_server = create_tcp_server()
    TCP_PORT = tcp_server.getsockname()[1]

    threading.Thread(target=tcp_accept_loop, args=(tcp_server,), daemon=True).start()
    threading.Thread(target=listen_for_broadcasts, daemon=True).start()
    threading.Thread(target=announce_loop, daemon=True).start()




def print_events():
    while True:
        e = events.get()
        kind = e["kind"]
        if kind == "peer_joined":
            print(f"[peers] + joined: {e['peer_id']} ({e['name']}) at {e['ip']}:{e['tcp_port']}")
        elif kind == "peer_left":
            print(f"[peers] - left: {e['peer_id']} ({e['name']}) at {e['ip']}")
        elif kind == "chat":
            print(f"[chat] {e['sender_name']} ({e['sender_id']}): {e['text']}")
        elif kind == "log":
            print(f"[log] {e['text']}")


def command_loop():
    print("commands: peers | send <peer_id> <text> | quit")
    while True:
        line = input("> ").strip()
        if line == "peers":
            snap = get_peers_snapshot()
            print(f"[peers] online now: {len(snap)}")
            for pid, info in snap.items():
                age = time.monotonic() - info["last_seen"]
                print(f"         {pid}  {info['name']}  {info['ip']}:{info['tcp_port']}  (seen {age:.1f}s ago)")
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


if __name__ == "__main__":
    start_network()
    print(f"[peer] my id is {PEER_ID}, name {PEER_NAME}, tcp port {TCP_PORT}")
    threading.Thread(target=print_events, daemon=True).start()
    command_loop()