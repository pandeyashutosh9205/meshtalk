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

ACK_TIMEOUT = 2    # seconds to wait for the receiver's acknowledgement
MAX_ACK = 1024     # an acknowledgement is tiny; refuse anything bigger


def handle_group_create(msg):
    """Returns True if the invite was accepted, so the sender gets an ack."""
    gid = msg.get("group_id")
    name = msg.get("group_name")
    members = msg.get("members")
    if not gid or not name or not isinstance(members, list):
        emit("log", text="ignored malformed group invite")
        return False

    name = str(name)[:40]                       # cap sizes so a peer can't flood us
    members = [str(m) for m in members][:50]

    if PEER_ID not in members or msg.get("from") not in members:
        return False                            # not for us, or the creator isn't a member

    with groups_lock:
        if gid in groups:
            return True                         # already have it; a repeat invite changes nothing
        groups[gid] = {"name": name, "members": members, "owner": msg.get("from")}
    emit("group_created", group_id=gid, name=name, members=members, failed=[])
    return True


def handle_group_msg(msg):
    """Returns True if the message was accepted, so the sender gets an ack."""
    gid = msg.get("group_id")
    with groups_lock:
        group = groups.get(gid)
    if group is None:
        emit("log", text="ignored message for an unknown group")
        return False
    if msg.get("from") not in group["members"]:
        emit("log", text=f"ignored group message from non-member {msg.get('from')}")
        return False
    emit("group_msg", group_id=gid, sender_id=msg.get("from"),
         sender_name=msg.get("from_name"), text=msg.get("text"))
    return True


def send_ack(conn, msg_id):
    body = json.dumps({"type": "ACK", "from": PEER_ID, "msg_id": msg_id}).encode()
    try:
        conn.sendall(struct.pack("!I", len(body)) + body)
    except OSError:
        pass    # the sender already gave up and closed the connection


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
                return

            mtype = msg.get("type")
            accepted = False
            if mtype == "GROUP_CREATE":
                accepted = handle_group_create(msg)
            elif mtype == "GROUP_MSG":
                accepted = handle_group_msg(msg)
            elif mtype in ("CHAT", "GROUP"):
                kind = "group_chat" if mtype == "GROUP" else "chat"
                emit(kind, sender_id=msg.get("from"), sender_name=msg.get("from_name"),
                     msg_id=msg.get("msg_id"), text=msg.get("text"))
                accepted = True

            if accepted and msg.get("msg_id"):
                send_ack(conn, msg["msg_id"])


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


def _wait_for_ack(sock, msg_id):
    """Read one ACK frame from the connection. True only if it confirms msg_id."""
    try:
        sock.settimeout(ACK_TIMEOUT)
        header = recv_exact(sock, 4)
        if header is None:
            return False
        (length,) = struct.unpack("!I", header)
        if length > MAX_ACK:
            return False
        body = recv_exact(sock, length)
        if body is None:
            return False
        reply = json.loads(body.decode())
        return (isinstance(reply, dict) and reply.get("type") == "ACK"
                and reply.get("msg_id") == msg_id)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return False


def _deliver(info, envelope, on_sent=None):
    """Send one framed message to one peer.
    Returns (error, acked): error is None once the bytes were written,
    and acked is True if the receiver confirmed it."""
    body = json.dumps(envelope).encode()
    frame = struct.pack("!I", len(body)) + body
    try:
        with socket.create_connection((info["ip"], info["tcp_port"]), timeout=3) as s:
            s.sendall(frame)
            if on_sent:
                on_sent()                       # lets the GUI show the first tick now
            return None, _wait_for_ack(s, envelope["msg_id"])
    except OSError as e:
        return str(e), False


def _fan_out(scope, envelope, targets, peer_id=None, group_id=None):
    """Send one envelope to several peers and report progress as events.
    targets is a list of (peer_id, info or None, display name)."""
    msg_id = envelope["msg_id"]
    emit("out_start", scope=scope, peer_id=peer_id, group_id=group_id,
         msg_id=msg_id, text=envelope["text"], total=len(targets))

    for pid, info, name in targets:
        if info is None:
            emit("out_fail", msg_id=msg_id, peer_id=pid, name=name,
                 reason="peer is no longer online")
            continue
        error, acked = _deliver(
            info, envelope,
            on_sent=lambda p=pid: emit("out_reach", msg_id=msg_id, peer_id=p))
        if error is not None:
            emit("out_fail", msg_id=msg_id, peer_id=pid, name=name, reason=error)
        elif acked:
            emit("out_ack", msg_id=msg_id, peer_id=pid)

    emit("out_done", msg_id=msg_id)


def send_message(peer_id, text):
    with peers_lock:
        info = peers.get(peer_id)
    name = info["name"] if info else peer_id
    _fan_out("direct", _make_envelope("CHAT", text), [(peer_id, info, name)],
             peer_id=peer_id)


def send_to_everyone(text):
    snap = get_peers_snapshot()
    targets = [(pid, info, info["name"]) for pid, info in snap.items()]
    if not targets:
        emit("log", text="Nobody else is online, so no one received that")
        return
    _fan_out("everyone", _make_envelope("GROUP", text), targets)


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
        error, _ = _deliver(info, envelope)
        if error is not None:
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

    targets = []
    for pid in group["members"]:
        if pid == PEER_ID:
            continue
        info = snap.get(pid)
        targets.append((pid, info, info["name"] if info else pid))
    if not targets:
        emit("log", text="there is nobody else in this group")
        return
    _fan_out("group", envelope, targets, group_id=group_id)

def start_network():
    """Start all background network threads. Returns immediately."""
    global TCP_PORT
    tcp_server = create_tcp_server()
    TCP_PORT = tcp_server.getsockname()[1]

    threading.Thread(target=tcp_accept_loop, args=(tcp_server,), daemon=True).start()
    threading.Thread(target=listen_for_broadcasts, daemon=True).start()
    threading.Thread(target=announce_loop, daemon=True).start()
