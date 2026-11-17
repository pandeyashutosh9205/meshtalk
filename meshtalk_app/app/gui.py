import queue
import threading
import time
import tkinter as tk

from meshtalk_app.network import discovery
from meshtalk_app.network.events import events

POLL_MS = 100   # how often the GUI checks the event queue

TYPING_EVERY = 2.0   # send at most one "typing" hint per conversation this often
TYPING_TTL = 4.0     # forget a "typing" hint if no newer one arrives in this long
IGNORED_KEYS = {"Return", "Shift_L", "Shift_R", "Control_L", "Control_R",
                "Alt_L", "Alt_R", "Caps_Lock", "Tab"}   # these don't count as typing

EVERYONE = "everyone"   # reserved conversation id for the Everyone channel
GROUP_PREFIX = "g:"     # group conversations are stored as "g:<group id>"
                        # (peer ids are plain hex, so they can never start with "g:")

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


def new_msg(kind, text, who="", msg_id=None, total=0):
    """One chat entry. Outgoing messages also track who they reached / who confirmed."""
    return {
        "kind": kind,            # "me", "them", "failed" or "system"
        "text": text,
        "stamp": time.strftime("%H:%M"),
        "who": who,
        "msg_id": msg_id,
        "total": total,          # how many recipients this message was sent to
        "reached": set(),        # peers whose connection accepted the bytes
        "acked": set(),          # peers that confirmed with an ACK
        "failed": [],            # (name, peer_id, reason)
    }


class MeshTalkApp:
    def __init__(self, root):
        self.root = root
        root.title("MeshTalk")
        root.geometry("900x580")
        root.minsize(680, 420)
        root.configure(bg=CHAT_BG)

        self.conversations = {}       # conversation id -> list of message dicts
        self.outgoing = {}            # msg_id -> (conversation id, message dict) while sending
        self.unread = {}              # conversation id -> number of unread messages
        self.names = {}               # peer_id -> last known name
        self.groups = {}              # group_id -> {"name", "members", "owner"} (cached)
        self.online_ids = set()       # peer ids currently in the peer table
        self.current_peer = EVERYONE  # open conversation id
        self.row_ids = [EVERYONE]     # row i in the listbox <-> conversation id row_ids[i]

        self.typing = {}              # conversation id -> {sender_id: (name, expires_at)}
        self.last_typing_sent = {}    # conversation id -> when we last announced typing
        self.sound_on = True
        self.last_sound = 0.0

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
        self.sound_label = tk.Label(bar, text="Sound on", bg=SIDEBAR_BG,
                                    fg=SIDEBAR_MUTED, font=(FONT, 9), cursor="hand2")
        self.sound_label.pack(side="right", padx=(0, 16))
        self.sound_label.bind("<Button-1>", lambda event: self.toggle_sound())

    def build_sidebar(self, parent):
        side = tk.Frame(parent, bg=SIDEBAR_BG, width=270)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)

        self.peers_label = tk.Label(side, text="People on this network", anchor="w",
                                    bg=SIDEBAR_BG, fg=SIDEBAR_MUTED, font=(FONT, 9))
        self.peers_label.pack(fill="x", padx=16, pady=(10, 6))

        # the button is packed before the list so it keeps its space at the bottom
        new_btn = tk.Button(
            side, text="New group", font=(FONT_BOLD, 10), bg=SIDEBAR_SELECT,
            fg="white", activebackground=ACCENT, activeforeground="white",
            relief="flat", bd=0, pady=8, cursor="hand2", command=self.open_group_dialog)
        new_btn.pack(side="bottom", fill="x", padx=12, pady=12)
        new_btn.bind("<Enter>", lambda event: new_btn.config(bg=ACCENT))
        new_btn.bind("<Leave>", lambda event: new_btn.config(bg=SIDEBAR_SELECT))

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
        self.entry.bind("<KeyRelease>", self.on_key)
        send_btn = tk.Button(
            send_row, text="Send", font=(FONT_BOLD, 10), bg=ACCENT, fg="white",
            activebackground=ACCENT_DARK, activeforeground="white",
            relief="flat", bd=0, padx=20, cursor="hand2", command=self.on_send)
        send_btn.pack(side="left", fill="y", padx=(8, 0))
        send_btn.bind("<Enter>", lambda event: send_btn.config(bg=ACCENT_DARK))
        send_btn.bind("<Leave>", lambda event: send_btn.config(bg=ACCENT))

        # packed right after the send row, so it sits just above it; always one line
        # tall (even when empty), so the layout never jumps when it fills in
        self.typing_label = tk.Label(pane, text="", anchor="w", bg=CHAT_BG, fg=MUTED,
                                     font=(FONT, 9, "italic"))
        self.typing_label.pack(side="bottom", fill="x", padx=16)

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
        # delivery status text that follows the time under your own bubbles
        self.chat.tag_configure("tick_sent", foreground=MUTED, font=small)
        self.chat.tag_configure("tick_done", foreground=ACCENT, font=(FONT_BOLD, 8))
        self.chat.tag_configure("system", foreground=MUTED, font=(FONT, 9),
                                justify="center", spacing1=12)
        self.chat.tag_configure("empty", foreground=MUTED, font=(FONT, 11),
                                justify="center", spacing1=80)

    # ---------- sidebar list ----------

    def refresh_peers(self):
        snap = discovery.get_peers_snapshot()
        self.groups = discovery.get_groups_snapshot()
        self.online_ids = set(snap)

        self.peer_list.delete(0, tk.END)
        self.row_ids = ([EVERYONE]
                        + [GROUP_PREFIX + gid for gid in self.groups]
                        + list(snap.keys()))

        for i, rid in enumerate(self.row_ids):
            if rid == EVERYONE:
                label = "Everyone"
            elif rid.startswith(GROUP_PREFIX):
                label = f"Group: {self.groups[rid[len(GROUP_PREFIX):]]['name']}"
            else:
                info = snap[rid]
                self.names[rid] = info["name"]
                label = f"{info['name']}  {rid}"
            n = self.unread.get(rid, 0)
            suffix = f"    {n} new" if n else ""
            self.peer_list.insert(tk.END, f"  {label}{suffix}")
            if n:
                self.peer_list.itemconfig(i, fg=UNREAD_TEXT)
            if rid == self.current_peer:
                self.peer_list.selection_set(i)

        if not snap:
            # placeholder row: not a real conversation, so it must not look selectable
            row = len(self.row_ids)
            self.peer_list.insert(tk.END, "  Looking for peers...")
            self.peer_list.itemconfig(row, fg=SIDEBAR_MUTED,
                                      selectbackground=SIDEBAR_BG,
                                      selectforeground=SIDEBAR_MUTED)
        self.peers_label.config(text=f"People on this network ({len(snap)})")
        self.update_header()

    def on_select(self, event):
        sel = self.peer_list.curselection()
        if not sel or sel[0] >= len(self.row_ids):
            return   # nothing selected, or the placeholder row
        self.current_peer = self.row_ids[sel[0]]
        self.unread[self.current_peer] = 0
        self.status.config(text="")
        self.render_chat()
        self.refresh_peers()
        self.update_typing_label()
        self.entry.focus_set()

    def update_header(self):
        cid = self.current_peer
        if cid == EVERYONE:
            n = len(self.online_ids)
            self.chat_title.config(text="Everyone")
            if n:
                word = "person" if n == 1 else "people"
                self.chat_sub.config(text=f"Your message goes to {n} {word} online")
            else:
                self.chat_sub.config(text="Nobody else is online yet")
        elif cid.startswith(GROUP_PREFIX):
            group = self.groups.get(cid[len(GROUP_PREFIX):])
            if group is None:
                self.chat_title.config(text="Group")
                self.chat_sub.config(text="")
                return
            parts = []
            for m in group["members"]:
                if m == discovery.PEER_ID:
                    parts.append("You")
                    continue
                name = self.names.get(m)
                label = f"{name} ({m})" if name else m
                if m not in self.online_ids:
                    label += " (offline)"
                parts.append(label)
            members = ", ".join(parts)
            if len(members) > 90:
                members = members[:90] + "..."
            self.chat_title.config(text=group["name"])
            self.chat_sub.config(text=members)
        else:
            name = self.names.get(cid, cid)
            self.chat_title.config(text=name)
            if cid in self.online_ids:
                self.chat_sub.config(text=f"{cid}, online now")
            else:
                self.chat_sub.config(
                    text=f"{cid}, offline. Messages cannot be delivered")

    # ---------- chat area ----------

    def delivery_status(self, msg):
        """Text and tag for the line under one of your own bubbles."""
        total, reached, acked = msg["total"], len(msg["reached"]), len(msg["acked"])
        if total == 1:
            if acked:
                return "✓✓ Delivered", "tick_done"
            if reached:
                return "✓ Sent", "tick_sent"
            return "Sending...", "tick_sent"
        if acked == total:
            return "✓✓ Delivered to all", "tick_done"
        if acked:
            return f"✓ Delivered to {acked} of {total}", "tick_sent"
        if reached:
            return "✓ Sent", "tick_sent"
        return "Sending...", "tick_sent"

    def draw(self, msg):
        kind, stamp = msg["kind"], msg["stamp"]
        if kind == "system":
            self.chat.insert(tk.END, msg["text"], "system")
            self.chat.insert(tk.END, "\n")
            return

        if kind == "me":
            bubble_tag = "me"
        elif kind == "them":
            bubble_tag = "them"
        else:
            bubble_tag = "failed"
        self.chat.insert(tk.END, f"  {msg['text']}  ", bubble_tag)   # spaces = padding
        self.chat.insert(tk.END, "\n")

        if kind == "me":
            self.chat.insert(tk.END, f"{stamp}  ", "me_time")
            status, tick_tag = self.delivery_status(msg)
            self.chat.insert(tk.END, status, tick_tag)
        elif kind == "them":
            label = f"{msg['who']}, {stamp}" if msg["who"] else stamp
            self.chat.insert(tk.END, label, "them_time")
        else:
            self.chat.insert(tk.END, f"Not delivered, {stamp}", "failed_time")
        self.chat.insert(tk.END, "\n")

    def render_chat(self, keep_scroll=False):
        if keep_scroll:
            first, last = self.chat.yview()
            at_bottom = last >= 0.999
        self.chat.config(state="normal")
        self.chat.delete("1.0", tk.END)
        msgs = self.conversations.get(self.current_peer, [])
        if not msgs:
            self.chat.insert(tk.END, "No messages yet. Say hello.", "empty")
        for msg in msgs:
            self.draw(msg)
        self.chat.config(state="disabled")
        if keep_scroll and not at_bottom:
            self.chat.yview_moveto(first)    # don't yank someone reading old messages
        else:
            self.chat.see(tk.END)

    def redraw_if_open(self, conv_id):
        if conv_id == self.current_peer:
            self.render_chat(keep_scroll=True)

    def add_message(self, conv_id, msg):
        msgs = self.conversations.setdefault(conv_id, [])
        msgs.append(msg)
        if conv_id == self.current_peer:
            if len(msgs) == 1:
                self.render_chat()   # replaces the "No messages yet" hint
            else:
                self.chat.config(state="normal")
                self.draw(msg)
                self.chat.config(state="disabled")
                self.chat.see(tk.END)
        elif msg["kind"] == "them":
            self.unread[conv_id] = self.unread.get(conv_id, 0) + 1
            self.refresh_peers()

        # beep for incoming messages you can't currently see
        if msg["kind"] == "them" and (conv_id != self.current_peer
                                      or self.root.focus_displayof() is None):
            self.play_sound()

    def on_send(self):
        text = self.entry.get().strip()
        if not text:
            return
        cid = self.current_peer
        if cid == EVERYONE:
            target, args = discovery.send_to_everyone, (text,)
        elif cid.startswith(GROUP_PREFIX):
            target, args = discovery.send_group_message, (cid[len(GROUP_PREFIX):], text)
        else:
            target, args = discovery.send_message, (cid, text)
        threading.Thread(target=target, args=args, daemon=True).start()
        self.entry.delete(0, tk.END)
        self.status.config(text="")

    # ---------- typing indicator ----------

    def typing_targets(self):
        """Who should hear that we're typing in the open conversation."""
        cid = self.current_peer
        if cid == EVERYONE:
            return list(self.online_ids), "everyone", None
        if cid.startswith(GROUP_PREFIX):
            gid = cid[len(GROUP_PREFIX):]
            group = self.groups.get(gid)
            if group is None:
                return [], "group", gid
            return [m for m in group["members"] if m != discovery.PEER_ID], "group", gid
        return [cid], "direct", None

    def on_key(self, event):
        if event.keysym in IGNORED_KEYS:
            return
        if not self.entry.get().strip():
            return                        # an empty box is not "typing"
        cid = self.current_peer
        now = time.monotonic()
        if now - self.last_typing_sent.get(cid, 0.0) < TYPING_EVERY:
            return                        # throttle: one hint per conversation per 2 s
        self.last_typing_sent[cid] = now
        targets, scope, gid = self.typing_targets()
        if targets:
            threading.Thread(target=discovery.send_typing,
                             args=(targets, scope, gid), daemon=True).start()

    def on_typing_event(self, e):
        if e["scope"] == "direct":
            conv = e["sender_id"]
        elif e["scope"] == "group":
            conv = GROUP_PREFIX + e["group_id"]
        else:
            conv = EVERYONE
        self.typing.setdefault(conv, {})[e["sender_id"]] = (
            e["sender_name"], time.monotonic() + TYPING_TTL)

    def clear_typing(self, conv, sender_id):
        self.typing.get(conv, {}).pop(sender_id, None)

    def update_typing_label(self):
        now = time.monotonic()
        who = self.typing.get(self.current_peer, {})
        for sid in [s for s, (_, expires) in who.items() if expires < now]:
            del who[sid]
        names = [name for name, _ in who.values()]
        if not names:
            text = ""
        elif len(names) == 1:
            text = f"{names[0]} is typing..."
        elif len(names) == 2:
            text = f"{names[0]} and {names[1]} are typing..."
        else:
            text = f"{len(names)} people are typing..."
        if text != self.typing_label.cget("text"):
            self.typing_label.config(text=text)

    # ---------- sound ----------

    def toggle_sound(self):
        self.sound_on = not self.sound_on
        self.sound_label.config(text="Sound on" if self.sound_on else "Sound off")

    def play_sound(self):
        now = time.monotonic()
        if not self.sound_on or now - self.last_sound < 1.0:
            return                        # muted, or too soon after the last beep
        self.last_sound = now
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONASTERISK)   # plays without blocking
        except Exception:
            self.root.bell()              # non-Windows fallback

    # ---------- new group dialog ----------

    def open_group_dialog(self):
        snap = discovery.get_peers_snapshot()

        dlg = tk.Toplevel(self.root)
        dlg.title("New group")
        dlg.configure(bg="white")
        dlg.transient(self.root)
        dlg.resizable(False, False)

        tk.Label(dlg, text="Group name", anchor="w", bg="white", fg=TEXT,
                 font=(FONT_BOLD, 10)).pack(fill="x", padx=16, pady=(14, 4))
        name_entry = tk.Entry(dlg, font=(FONT, 11), relief="flat", bd=0, width=34,
                              highlightthickness=1, highlightbackground=BORDER,
                              highlightcolor=ACCENT)
        name_entry.pack(fill="x", padx=16, ipady=6)

        tk.Label(dlg, text="Add people", anchor="w", bg="white", fg=TEXT,
                 font=(FONT_BOLD, 10)).pack(fill="x", padx=16, pady=(12, 4))

        picks = {}
        if not snap:
            tk.Label(dlg, text="Nobody else is online yet. Start MeshTalk on another "
                     "device or window first.", wraplength=300, justify="left",
                     bg="white", fg=MUTED, font=(FONT, 10)).pack(padx=16, pady=4)
        for pid, info in snap.items():
            var = tk.BooleanVar(value=False)
            picks[pid] = var
            tk.Checkbutton(dlg, text=f"{info['name']}  {pid}", variable=var,
                           anchor="w", bg="white", fg=TEXT, font=(FONT, 11),
                           activebackground="white", selectcolor="white"
                           ).pack(fill="x", padx=16)

        error = tk.Label(dlg, text="", anchor="w", bg="white", fg=FAIL_FG,
                         font=(FONT, 9))
        error.pack(fill="x", padx=16, pady=(8, 0))

        def create():
            name = name_entry.get().strip()[:40]
            members = [pid for pid, var in picks.items() if var.get()]
            if not name:
                error.config(text="Give the group a name")
                return
            if not members:
                error.config(text="Pick at least one person")
                return
            threading.Thread(target=discovery.create_group, args=(name, members),
                             daemon=True).start()
            dlg.destroy()

        tk.Button(dlg, text="Create group", font=(FONT_BOLD, 10), bg=ACCENT, fg="white",
                  activebackground=ACCENT_DARK, activeforeground="white",
                  relief="flat", bd=0, pady=8, cursor="hand2", command=create
                  ).pack(fill="x", padx=16, pady=14)
        name_entry.bind("<Return>", lambda event: create())
        name_entry.focus_set()

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
                self.clear_typing(e["sender_id"], e["sender_id"])
                self.add_message(e["sender_id"], new_msg("them", e["text"]))
            elif kind == "group_chat":
                self.clear_typing(EVERYONE, e["sender_id"])
                who = f"{e['sender_name']} ({e['sender_id']})"
                self.add_message(EVERYONE, new_msg("them", e["text"], who=who))
            elif kind == "group_msg":
                self.clear_typing(GROUP_PREFIX + e["group_id"], e["sender_id"])
                who = f"{e['sender_name']} ({e['sender_id']})"
                self.add_message(GROUP_PREFIX + e["group_id"],
                                 new_msg("them", e["text"], who=who))
            elif kind == "typing":
                self.on_typing_event(e)
            elif kind == "out_start":
                self.on_out_start(e)
            elif kind == "out_reach":
                self.on_out_progress(e, "reached")
            elif kind == "out_ack":
                self.on_out_progress(e, "acked")
            elif kind == "out_fail":
                rec = self.outgoing.get(e["msg_id"])
                if rec:
                    rec[1]["failed"].append((e["name"], e["peer_id"], e["reason"]))
            elif kind == "out_done":
                self.on_out_done(e)
            elif kind == "group_created":
                self.handle_group_created(e)
            elif kind == "log":
                self.status.config(text=e["text"])
        self.update_typing_label()
        self.root.after(POLL_MS, self.poll_events)

    def on_out_start(self, e):
        if e["scope"] == "direct":
            conv = e["peer_id"]
        elif e["scope"] == "group":
            conv = GROUP_PREFIX + e["group_id"]
        else:
            conv = EVERYONE
        msg = new_msg("me", e["text"], msg_id=e["msg_id"], total=e["total"])
        self.outgoing[e["msg_id"]] = (conv, msg)
        self.add_message(conv, msg)

    def on_out_progress(self, e, field):
        rec = self.outgoing.get(e["msg_id"])
        if rec is None:
            return
        conv, msg = rec
        msg[field].add(e["peer_id"])
        self.redraw_if_open(conv)

    def on_out_done(self, e):
        rec = self.outgoing.pop(e["msg_id"], None)
        if rec is None:
            return
        conv, msg = rec
        missed = ", ".join(f"{n} ({p})" for n, p, _ in msg["failed"])
        if not msg["reached"]:
            msg["kind"] = "failed"           # nobody got it: turn the bubble red
            if msg["total"] == 1 and msg["failed"]:
                name, _, reason = msg["failed"][0]
                self.status.config(text=f"Could not reach {name}: {reason}")
            else:
                self.status.config(text=f"Could not reach anyone: {missed}")
        elif msg["failed"]:
            self.status.config(text=f"Could not reach: {missed}")
        self.redraw_if_open(conv)

    def handle_group_created(self, e):
        cid = GROUP_PREFIX + e["group_id"]
        owner = e["members"][0]              # the creator always puts themselves first
        by_me = owner == discovery.PEER_ID
        if by_me:
            self.current_peer = cid          # open the group you just made
            self.unread[cid] = 0
        self.refresh_peers()                 # first, so the group and names are known
        if by_me:
            text = f"You created {e['name']}"
        else:
            text = f"{self.names.get(owner, owner)} added you to {e['name']}"
        self.add_message(cid, new_msg("system", text))
        if e["failed"]:
            self.status.config(text="Group created, but could not invite: "
                               + ", ".join(e["failed"]))


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