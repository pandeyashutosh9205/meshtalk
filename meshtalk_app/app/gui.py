import queue
import threading
import tkinter as tk

from meshtalk_app.network import discovery
from meshtalk_app.network.events import events

POLL_MS = 100  


class MeshTalkApp:
    def __init__(self, root):
        self.root = root
        root.title("MeshTalk")
        root.geometry("800x500")

        self.header = tk.Label(root, text="starting...", anchor="w")
        self.header.pack(fill="x", padx=8, pady=4)

        body = tk.Frame(root)
        body.pack(fill="both", expand=True, padx=8)

        left = tk.Frame(body)
        left.pack(side="left", fill="y")
        tk.Label(left, text="Peers online", anchor="w").pack(fill="x")
        self.peer_list = tk.Listbox(left, width=34, exportselection=False)
        self.peer_list.pack(fill="y", expand=True)

        right = tk.Frame(body)
        right.pack(side="left", fill="both", expand=True, padx=(8, 0))
        tk.Label(right, text="Chat", anchor="w").pack(fill="x")
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

        self.peer_ids = []  

        discovery.start_network()
        self.header.config(
            text=f"You: {discovery.PEER_NAME} ({discovery.PEER_ID})   tcp port {discovery.TCP_PORT}"
        )
        self.poll_events()

    def selected_peer_id(self):
        sel = self.peer_list.curselection()
        if not sel:
            return None
        return self.peer_ids[sel[0]]

    def refresh_peers(self):
        keep = self.selected_peer_id()
        snap = discovery.get_peers_snapshot()
        self.peer_list.delete(0, tk.END)
        self.peer_ids = list(snap.keys())
        for i, (pid, info) in enumerate(snap.items()):
            self.peer_list.insert(tk.END, f"{info['name']}  ({pid})  {info['ip']}")
            if pid == keep:
                self.peer_list.selection_set(i)

    def append_chat(self, line):
        self.chat.config(state="normal")
        self.chat.insert(tk.END, line + "\n")
        self.chat.config(state="disabled")
        self.chat.see(tk.END)

    def on_send(self):
        text = self.entry.get().strip()
        if not text:
            return
        pid = self.selected_peer_id()
        if pid is None:
            self.status.config(text="select a peer first")
            return
        name = discovery.get_peers_snapshot().get(pid, {}).get("name", pid)
        threading.Thread(target=discovery.send_message, args=(pid, text), daemon=True).start()
        self.append_chat(f"You -> {name} ({pid}): {text}")
        self.entry.delete(0, tk.END)
        self.status.config(text="")

    def poll_events(self):
        while True:
            try:
                e = events.get_nowait()
            except queue.Empty:
                break
            if e["kind"] in ("peer_joined", "peer_left"):
                self.refresh_peers()
            elif e["kind"] == "chat":
                self.append_chat(f"{e['sender_name']} ({e['sender_id']}): {e['text']}")
            elif e["kind"] == "log":
                self.status.config(text=e["text"])
        self.root.after(POLL_MS, self.poll_events)


def main():
    root = tk.Tk()
    MeshTalkApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
