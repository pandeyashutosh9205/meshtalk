import queue
import threading
import time
import tkinter as tk

from meshtalk_app.network import discovery
from meshtalk_app.network.events import events

POLL_MS = 100   # how often the GUI checks the event queue

EVERYONE = "everyone"   # reserved conversation id for the group channel
                        # (real peer ids are hex, so they can never equal this)

# ---------- design tokens: change the look of the whole app here ----------
FONT = "Segoe UI"
FONT_BOLD = "Segoe UI Semibold"

SIDEBAR_BG = "#1B2A33"
SIDEBAR_TEXT = "#D5E0E6"
SIDEBAR_MUTED = "#8DA2AE"
SIDEBAR_SELECT = "#2C4452"
UNREAD_TEXT = "#6FE0D0"

CHAT_BG = "#EDF1F3"
TEXT = "#1E2B33"
MUTED = "#5A6B75"
BORDER = "#CBD5DB"

ACCENT = "#1C7C8A"
ACCENT_DARK = "#155F6A"
ME_BG = ACCENT
THEM_BG = "#FFFFFF"
FAIL_BG = "#FBE3E0"
FAIL_FG = "#9A2A1F"


class MeshTalkApp:
    def __init__(self, root):
        self.root = root
        root.title("MeshTalk")
        root.geometry("900x580")
        root.minsize(680, 420)
        root.configure(bg=CHAT_BG)

        self.conversations = {}      # conversation id -> list of (kind, text, time, who)
        self.unread = {}             # conversation id -> number of unread messages
        self.names = {}              # peer_id -> last known name
        self.current_peer = EVERYONE # open conversation: EVERYONE or a peer id
        self.peer_ids = [EVERYONE]   # row i in the listbox <-> id peer_ids[i]

        self.build_top_bar()

        self.status = tk.Label(root, text="", anchor="w", bg=CHAT_BG, fg=MUTED,
                               font=(FONT, 9))
        self.status.pack(side="bottom", fill="x", padx=12, pady=(0, 6))

        body = tk.Frame(root, bg=CHAT_BG)
        body.pack(fill="both", expand=True)
        self.build_sidebar(body)
        self.build_chat_pane(body)

        discovery.start_network()
        self.id_label.config(text=f"{discovery.PEER_NAME} ({discovery.PEER_ID})")
        self.port_label.config(text=f"tcp port {discovery.TCP_PORT}")
        self.refresh_peers()
        self.render_chat()
        self.poll_events()

    # ---------- layout ----------

    def build_top_bar(self):
        bar = tk.Frame(self.root, bg=SIDEBAR_BG)
        bar.pack(side="top", fill="x")
        tk.Label(bar, text="MeshTalk", bg=SIDEBAR_BG, fg="white",
                 font=(FONT_BOLD, 14)).pack(side="left", padx=16, pady=10)
        self.port_label = tk.Label(bar, bg=SIDEBAR_BG, fg=SIDEBAR_MUTED, font=(FONT, 9))
        self.port_label.pack(side="right", padx=(0, 16))
        self.id_label = tk.Label(bar, bg=SIDEBAR_BG, fg=SIDEBAR_TEXT, font=(FONT, 10))
        self.id_label.pack(side="right", padx=(0, 10))

    def build_sidebar(self, parent):
        side = tk.Frame(parent, bg=SIDEBAR_BG, width=270)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)

        self.peers_label = tk.Label(side, text="People on this network", anchor="w",
                                    bg=SIDEBAR_BG, fg=SIDEBAR_MUTED, font=(FONT, 9))
        self.peers_label.pack(fill="x", padx=16, pady=(10, 6))

        self.peer_list = tk.Listbox(
            side, bg=SIDEBAR_BG, fg=SIDEBAR_TEXT, font=(FONT, 12),
            selectbackground=SIDEBAR_SELECT, selectforeground="white",
            activestyle="none", borderwidth=0, highlightthickness=0,
            exportselection=False)
        self.peer_list.pack(fill="both", expand=True, padx=8)
        self.peer_list.bind("<<ListboxSelect>>", self.on_select)

    def build_chat_pane(self, parent):
        pane = tk.Frame(parent, bg=CHAT_BG)
        pane.pack(side="left", fill="both", expand=True)

        head = tk.Frame(pane, bg="white")
        head.pack(side="top", fill="x")
        self.chat_title = tk.Label(head, text="", anchor="w", bg="white", fg=TEXT,
                                   font=(FONT_BOLD, 13))
        self.chat_title.pack(fill="x", padx=16, pady=(10, 0))
        self.chat_sub = tk.Label(head, text="", anchor="w", bg="white", fg=MUTED,
                                 font=(FONT, 9))
        self.chat_sub.pack(fill="x", padx=16, pady=(0, 10))
        tk.Frame(pane, bg=BORDER, height=1).pack(side="top", fill="x")

        # the send row is packed before the chat area so it keeps its space
        send_row = tk.Frame(pane, bg=CHAT_BG)
        send_row.pack(side="bottom", fill="x", padx=12, pady=10)
        self.entry = tk.Entry(
            send_row, font=(FONT, 11), relief="flat", bd=0, bg="white", fg=TEXT,
            insertbackground=TEXT, highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT)
        self.entry.pack(side="left", fill="x", expand=True, ipady=8)
        self.entry.bind("<Return>", lambda event: self.on_send())
        send_btn = tk.Button(
            send_row, text="Send", font=(FONT_BOLD, 10), bg=ACCENT, fg="white",
            activebackground=ACCENT_DARK, activeforeground="white",
            relief="flat", bd=0, padx=20, cursor="hand2", command=self.on_send)
        send_btn.pack(side="left", fill="y", padx=(8, 0))
        send_btn.bind("<Enter>", lambda event: send_btn.config(bg=ACCENT_DARK))
        send_btn.bind("<Leave>", lambda event: send_btn.config(bg=ACCENT))

        text_frame = tk.Frame(pane, bg=CHAT_BG)
        text_frame.pack(side="top", fill="both", expand=True)
        self.chat = tk.Text(text_frame, state="disabled", wrap="word", bg=CHAT_BG,
                            fg=TEXT, font=(FONT, 11), borderwidth=0,
                            highlightthickness=0, padx=0, pady=8, cursor="arrow")
        scroll = tk.Scrollbar(text_frame, command=self.chat.yview)
        self.chat.config(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.chat.pack(side="left", fill="both", expand=True)
        self.configure_tags()

    def configure_tags(self):
        bubble = (FONT, 11)
        small = (FONT, 8)
        right = dict(justify="right", lmargin1=140, lmargin2=140, rmargin=16)
        left = dict(justify="left", lmargin1=16, lmargin2=16, rmargin=140)

        self.chat.tag_configure("me", background=ME_BG, foreground="white",
                                font=bubble, spacing1=10, **right)
        self.chat.tag_configure("me_time", foreground=MUTED, font=small,
                                spacing1=2, **right)
        self.chat.tag_configure("them", background=THEM_BG, foreground=TEXT,
                                font=bubble, spacing1=10, **left)
        self.chat.tag_configure("them_time", foreground=MUTED, font=small,
                                spacing1=2, **left)
        self.chat.tag_configure("failed", background=FAIL_BG, foreground=FAIL_FG,
                                font=bubble, spacing1=10, **right)
        self.chat.tag_configure("failed_time", foreground=FAIL_FG, font=small,
                                spacing1=2, **right)
        self.chat.tag_configure("empty", foreground=MUTED, font=(FONT, 11),
                                justify="center", spacing1=80)

    # ---------- peer list ----------

    def refresh_peers(self):
        snap = discovery.get_peers_snapshot()
        self.peer_list.delete(0, tk.END)
        self.peer_ids = [EVERYONE] + list(snap.keys())

        for i, pid in enumerate(self.peer_ids):
            if pid == EVERYONE:
                label = "Everyone"
            else:
                info = snap[pid]
                self.names[pid] = info["name"]
                label = f"{info['name']}  {pid}"
            n = self.unread.get(pid, 0)
            suffix = f"    {n} new" if n else ""
            self.peer_list.insert(tk.END, f"  {label}{suffix}")
            if n:
                self.peer_list.itemconfig(i, fg=UNREAD_TEXT)
            if pid == self.current_peer:
                self.peer_list.selection_set(i)

        if not snap:
            # placeholder row: not a real conversation, so it must not look selectable
            row = len(self.peer_ids)
            self.peer_list.insert(tk.END, "  Looking for peers...")
            self.peer_list.itemconfig(row, fg=SIDEBAR_MUTED,
                                      selectbackground=SIDEBAR_BG,
                                      selectforeground=SIDEBAR_MUTED)
        self.peers_label.config(text=f"People on this network ({len(snap)})")
        self.update_header()

    def on_select(self, event):
        sel = self.peer_list.curselection()
        if not sel or sel[0] >= len(self.peer_ids):
            return   # nothing selected, or the placeholder row
        self.current_peer = self.peer_ids[sel[0]]
        self.unread[self.current_peer] = 0
        self.status.config(text="")
        self.render_chat()
        self.refresh_peers()
        self.entry.focus_set()

    def update_header(self):
        if self.current_peer is None:
            self.chat_title.config(text="No chat open")
            self.chat_sub.config(text="Pick someone from the list to start chatting")
            return
        if self.current_peer == EVERYONE:
            n = len(self.peer_ids) - 1
            self.chat_title.config(text="Everyone")
            if n:
                word = "person" if n == 1 else "people"
                self.chat_sub.config(text=f"Your message goes to {n} {word} online")
            else:
                self.chat_sub.config(text="Nobody else is online yet")
            return
        name = self.names.get(self.current_peer, self.current_peer)
        self.chat_title.config(text=name)
        if self.current_peer in self.peer_ids:
            self.chat_sub.config(text=f"{self.current_peer}, online now")
        else:
            self.chat_sub.config(
                text=f"{self.current_peer}, offline. Messages cannot be delivered")

    # ---------- chat area ----------

    def draw(self, kind, text, stamp, who=""):
        label = f"{who}, {stamp}" if who else stamp
        if kind == "me":
            tag, time_tag, caption = "me", "me_time", label
        elif kind == "them":
            tag, time_tag, caption = "them", "them_time", label
        else:
            tag, time_tag, caption = "failed", "failed_time", f"Not delivered, {stamp}"
        self.chat.insert(tk.END, f"  {text}  ", tag)   # spaces act as bubble padding
        self.chat.insert(tk.END, "\n")
        self.chat.insert(tk.END, caption, time_tag)
        self.chat.insert(tk.END, "\n")

    def render_chat(self):
        self.chat.config(state="normal")
        self.chat.delete("1.0", tk.END)
        if self.current_peer is None:
            self.chat.insert(tk.END, "Pick someone from the list to start chatting.", "empty")
        else:
            msgs = self.conversations.get(self.current_peer, [])
            if not msgs:
                self.chat.insert(tk.END, "No messages yet. Say hello.", "empty")
            for kind, text, stamp, who in msgs:
                self.draw(kind, text, stamp, who)
        self.chat.config(state="disabled")
        self.chat.see(tk.END)

    def add_message(self, conv_id, kind, text, who=""):
        stamp = time.strftime("%H:%M")
        msgs = self.conversations.setdefault(conv_id, [])
        msgs.append((kind, text, stamp, who))
        if conv_id == self.current_peer:
            if len(msgs) == 1:
                self.render_chat()   # replaces the "No messages yet" hint
            else:
                self.chat.config(state="normal")
                self.draw(kind, text, stamp, who)
                self.chat.config(state="disabled")
                self.chat.see(tk.END)
        elif kind == "them":
            self.unread[conv_id] = self.unread.get(conv_id, 0) + 1
            self.refresh_peers()

    def on_send(self):
        text = self.entry.get().strip()
        if not text:
            return
        if self.current_peer is None:
            self.status.config(text="Select a conversation first")
            return
        if self.current_peer == EVERYONE:
            target, args = discovery.send_to_everyone, (text,)
        else:
            target, args = discovery.send_message, (self.current_peer, text)
        threading.Thread(target=target, args=args, daemon=True).start()
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
                self.names.setdefault(e["sender_id"], e["sender_name"])
                self.add_message(e["sender_id"], "them", e["text"])
            elif kind == "group_chat":
                who = f"{e['sender_name']} ({e['sender_id']})"
                self.add_message(EVERYONE, "them", e["text"], who)
            elif kind == "sent":
                self.add_message(e["peer_id"], "me", e["text"])
            elif kind == "group_sent":
                d, f = len(e["delivered"]), len(e["failed"])
                if d == 0 and f == 0:
                    self.status.config(text="Nobody else is online, so no one received that")
                elif d == 0:
                    self.add_message(EVERYONE, "failed", e["text"])
                    self.status.config(text="Could not reach anyone: " + ", ".join(e["failed"]))
                else:
                    note = f"Delivered to {d}" + (f", failed for {f}" if f else "")
                    self.add_message(EVERYONE, "me", e["text"], note)
                    if f:
                        self.status.config(text="Could not reach: " + ", ".join(e["failed"]))
            elif kind == "send_failed":
                self.add_message(e["peer_id"], "failed", e["text"])
                self.status.config(text=f"Could not reach {e['name']}: {e['reason']}")
            elif kind == "log":
                self.status.config(text=e["text"])
        self.root.after(POLL_MS, self.poll_events)


def main():
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)   # sharp text on high-DPI screens
    except Exception:
        pass
    root = tk.Tk()
    MeshTalkApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()