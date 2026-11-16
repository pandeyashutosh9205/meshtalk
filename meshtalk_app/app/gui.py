import queue
import threading
import tkinter as tk

from meshtalk_app.network import discovery
from meshtalk_app.network.events import events

POLL_MS = 100   # how often the GUI checks the event queue


class MeshTalkApp:
    def __init__(self, root):
        self.root = root
        root.title("MeshTalk")
        root.geometry("800x500")

        self.conversations = {}   # peer_id -> list of chat lines
        self.unread = {}          # peer_id -> number of unread messages
        self.current_peer = None  # peer_id of the open chat, or None
        self.peer_ids = []        # row i in the listbox <-> peer id peer_ids[i]

        self.header = tk.Label(root, text="starting...", anchor="w")
        self.header.pack(fill="x", padx=8, pady=4)

        body = tk.Frame(root)
        body.pack(fill="both", expand=True, padx=8)

        # left side: peer list
        left = tk.Frame(body)
        left.pack(side="left", fill="y")
        tk.Label(left, text="Peers online", anchor="w").pack(fill="x")
        self.peer_list = tk.Listbox(left, width=30, exportselection=False)
        self.peer_list.pack(fill="y", expand=True)
        self.peer_list.bind("<<ListboxSelect>>", self.on_select)

        # right side: chat title, chat area, send box
        right = tk.Frame(body)
        right.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self.chat_title = tk.Label(right, text="Select a peer to start chatting", anchor="w")
        self.chat_title.pack(fill="x")
        self.chat = tk.Text(right, state="disabled", wrap="word")
        self.chat.pack(fill="both", expand=True)

        send_row = tk.Frame(right)
        send_row.pack(fill="x", pady=4)
        self.entry = tk.Entry(send_row)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", lambda event: self.on_send())
        tk.Button(send_row, text="Send", command=self.on_send).pack(side="left", padx=(4, 0))

        self.status = tk.Label(root, text="", anchor="w")
        self.status.pack(fill="x", padx=8, pady=4)

        discovery.start_network()
        self.header.config(
            text=f"You: {discovery.PEER_NAME} ({discovery.PEER_ID})   tcp port {discovery.TCP_PORT}"
        )
        self.poll_events()

    # ---------- peer list ----------

    def refresh_peers(self):
        snap = discovery.get_peers_snapshot()
        self.peer_list.delete(0, tk.END)
        self.peer_ids = list(snap.keys())
        for i, (pid, info) in enumerate(snap.items()):
            n = self.unread.get(pid, 0)
            badge = f"   [{n}]" if n else ""
            self.peer_list.insert(tk.END, f"{info['name']}  ({pid}){badge}")
            if pid == self.current_peer:
                self.peer_list.selection_set(i)

    def on_select(self, event):
        sel = self.peer_list.curselection()
        if not sel:
            return
        self.current_peer = self.peer_ids[sel[0]]
        self.unread[self.current_peer] = 0
        name = discovery.get_peers_snapshot().get(self.current_peer, {}).get("name", "?")
        self.chat_title.config(text=f"Chat with {name} ({self.current_peer})")
        self.status.config(text="")
        self.render_chat()
        self.refresh_peers()

    # ---------- chat area ----------

    def render_chat(self):
        self.chat.config(state="normal")
        self.chat.delete("1.0", tk.END)
        for line in self.conversations.get(self.current_peer, []):
            self.chat.insert(tk.END, line + "\n")
        self.chat.config(state="disabled")
        self.chat.see(tk.END)

    def add_line(self, peer_id, line):
        self.conversations.setdefault(peer_id, []).append(line)
        if peer_id == self.current_peer:
            self.chat.config(state="normal")
            self.chat.insert(tk.END, line + "\n")
            self.chat.config(state="disabled")
            self.chat.see(tk.END)
        else:
            self.unread[peer_id] = self.unread.get(peer_id, 0) + 1
            self.refresh_peers()

    def on_send(self):
        text = self.entry.get().strip()
        if not text:
            return
        if self.current_peer is None:
            self.status.config(text="select a peer first")
            return
        threading.Thread(target=discovery.send_message,
                         args=(self.current_peer, text), daemon=True).start()
        self.entry.delete(0, tk.END)
        self.status.config(text="")

    # ---------- events from the network threads ----------

    def poll_events(self):
        while True:
            try:
                e = events.get_nowait()
            except queue.Empty:
                break
            kind = e["kind"]
            if kind in ("peer_joined", "peer_left"):
                self.refresh_peers()
            elif kind == "chat":
                self.add_line(e["sender_id"], f"{e['sender_name']}: {e['text']}")
            elif kind == "sent":
                self.add_line(e["peer_id"], f"You: {e['text']}")
            elif kind == "send_failed":
                self.add_line(e["peer_id"], f"!! NOT DELIVERED: {e['text']}")
                self.status.config(text=f"failed to reach {e['name']}: {e['reason']}")
            elif kind == "log":
                self.status.config(text=e["text"])
        self.root.after(POLL_MS, self.poll_events)


def main():
    root = tk.Tk()
    MeshTalkApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()