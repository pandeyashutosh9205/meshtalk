import socket


BROADCAST_PORT=50000
BROADCAST_MESSAGE = b"MESHTALK_HELLO"


def send_discovery_broadcast():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    sock.sendto(BROADCAST_MESSAGE, ('255.255.255.255', BROADCAST_PORT))
    print(f"Sent broadcast: {BROADCAST_MESSAGE} to port {BROADCAST_PORT}")

    sock.close()


def listen_for_broadcasts():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    sock.bind(('', BROADCAST_PORT))

    print(f"Listening for broadcasts on port {BROADCAST_PORT}...")

    while True:
        data, addr = sock.recvfrom(1024)
        print(f"Received: {data} from {addr}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "listen":
        listen_for_broadcasts()
    else:
        send_discovery_broadcast()

