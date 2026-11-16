import queue
import tkinter as tk

from meshtalk_app.network import discovery
from meshtalk_app.network.events import events

POLL_MS = 100   # how often the GUI checks the event queue


class MeshTalkApp:
    def __init__(self, root):
        self.root = root
        root.title("MeshTalk")
        root.geometry("700x450")

        self.header = tk.Label(root, text="starting...", anchor="w")
        self.header.pack(fill="x", padx=8, pady=4)

        tk.Label(root, text="Peers online", anchor="w").pack(fill="x", padx=8)
        self.peer_list = tk.Listbox(root, height=10)
        self.peer_list.pack(fill="both", expand=True, padx=8, pady=4)

        self.status = tk.Label(root, text="", anchor="w")
        self.status.pack(fill="x", padx=8, pady=4)

        self.peer_ids = []   # row i in the listbox <-> peer id peer_ids[i]

        discovery.start_network()
        self.header.config(
            text=f"You: {discovery.PEER_NAME} ({discovery.PEER_ID})   tcp port {discovery.TCP_PORT}"
        )
        self.poll_events()

    def refresh_peers(self):
        snap = discovery.get_peers_snapshot()
        self.peer_list.delete(0, tk.END)
        self.peer_ids = list(snap.keys())
        for pid, info in snap.items():
            self.peer_list.insert(tk.END, f"{info['name']}  ({pid})  {info['ip']}")

    def poll_events(self):
        while True:
            try:
                e = events.get_nowait()
            except queue.Empty:
                break
            if e["kind"] in ("peer_joined", "peer_left"):
                self.refresh_peers()
            elif e["kind"] == "log":
                self.status.config(text=e["text"])
        self.root.after(POLL_MS, self.poll_events)


def main():
    root = tk.Tk()
    MeshTalkApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()