"""
Smart Ordered Chat Server — Application Semantics Edition
=========================================================
Application semantics are expressed through:
  - Named types / dataclasses instead of bare dicts
  - Explicit state transitions (ClientSession lifecycle)
  - Lock-protected shared state (no silent races)
  - root.after() for all GUI updates (thread safety)
  - Named domain operations instead of inline logic
"""

import socket
import threading
import time
import tkinter as tk
from tkinter import scrolledtext
from datetime import datetime
from dataclasses import dataclass, field
from typing import Dict, Set, Optional, Tuple
import random

# Semantic: how long we wait for a missing ID before giving up on it.
# The Arduino sends every 3s, so ~2 intervals is long enough to be sure
# the message isn't just arriving out of order.
GAP_TIMEOUT_SECONDS = 6.5

# ========================================================
# DOMAIN TYPES
# ========================================================

@dataclass
class ChatMessage:
    """
    A parsed, validated chat message.
    Invariant: msg_id > 0, user is non-empty, text is non-empty.
    """
    msg_id: int
    user: str
    text: str
    raw: str

    @staticmethod
    def parse(raw: str) -> Optional["ChatMessage"]:
        """
        Precondition:  raw matches "ID:N|USER:X|MSG:Y"
        Postcondition: returns ChatMessage or None (never raises)
        """
        try:
            parts = raw.strip().split("|")
            msg_id = int(parts[0].split(":")[1])
            user   = parts[1].split(":")[1]
            text   = parts[2].split(":")[1]
            if msg_id <= 0 or not user or not text:
                return None
            return ChatMessage(msg_id=msg_id, user=user, text=text, raw=raw)
        except Exception:
            return None


@dataclass
class OrderingState:
    """
    Tracks message sequence ordering across all clients.
    Invariant: expected_id is always the next ID we need to deliver in order.
    """
    expected_id: int = 1
    buffer: Dict[int, ChatMessage] = field(default_factory=dict)
    seen_ids: Set[int] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)
    gap_opened_at: Optional[float] = None   # monotonic timestamp, or None if no gap open

    def is_duplicate(self, msg_id: int) -> bool:
        return msg_id in self.seen_ids

    def record_and_deliver(self, msg: ChatMessage) -> Tuple[list, Optional[Tuple[int, int]]]:
        """
        Precondition:  msg is valid and not a duplicate
        Postcondition: returns (deliverable, gap_abandoned)
          - deliverable: messages ready to deliver in order; expected_id advances accordingly
          - gap_abandoned: None, or (gave_up_on_id, resumed_at_id) if a stale gap
            was abandoned so buffered messages could finally be released
        """
        self.seen_ids.add(msg.msg_id)
        self.buffer[msg.msg_id] = msg

        if msg.msg_id > self.expected_id and self.gap_opened_at is None:
            self.gap_opened_at = time.monotonic()

        gap_abandoned = self._maybe_abandon_stale_gap()

        deliverable = []
        while self.expected_id in self.buffer:
            deliverable.append(self.buffer.pop(self.expected_id))
            self.expected_id += 1

        if deliverable and not self.buffer:
            self.gap_opened_at = None

        return deliverable, gap_abandoned

    def _maybe_abandon_stale_gap(self) -> Optional[Tuple[int, int]]:
        """
        Semantic: a gap open longer than GAP_TIMEOUT_SECONDS means the missing
        message is never coming (e.g. the simulated lost packet). Give up on it
        and resume delivery from the next message we actually have, instead of
        buffering everything after it forever.
        Postcondition: returns None, or (gave_up_on_id, resumed_at_id)
        """
        if self.gap_opened_at is None or not self.buffer:
            return None
        if time.monotonic() - self.gap_opened_at < GAP_TIMEOUT_SECONDS:
            return None

        gave_up_on = self.expected_id
        self.expected_id = min(self.buffer)
        self.gap_opened_at = None
        return gave_up_on, self.expected_id

    def is_gap_detected(self, msg_id: int) -> bool:
        """Semantic: a gap means we received a future ID before the expected one."""
        return msg_id > self.expected_id


@dataclass
class ClientSession:
    """
    Represents one connected client's lifecycle.
    Semantic: a session is either ACTIVE or CLOSED — nothing in between.
    """
    conn: socket.socket
    addr: tuple
    color: str
    is_active: bool = True

    def close(self):
        """Postcondition: is_active is False, socket is closed."""
        self.is_active = False
        try:
            self.conn.close()
        except OSError:
            pass

    def send(self, message: str) -> bool:
        """
        Precondition:  is_active is True
        Postcondition: returns True if sent, False if session is now dead.
        """
        if not self.is_active:
            return False
        try:
            self.conn.sendall((message + "\n").encode())
            return True
        except OSError:
            self.close()
            return False

    def receive(self) -> Optional[bytes]:
        """Returns raw bytes or None if the connection is closed."""
        try:
            data = self.conn.recv(1024)
            return data if data else None
        except OSError:
            return None


# ========================================================
# SHARED SERVER STATE  (all mutations protected by a lock)
# ========================================================

class ServerState:
    """
    Single source of truth for all mutable server data.
    Invariant: all fields accessed only while holding self.lock.
    """
    def __init__(self):
        self.lock = threading.Lock()
        self.sessions: list[ClientSession] = []
        self.packet_count: int = 0
        self.ordering = OrderingState()

    def add_session(self, session: ClientSession):
        with self.lock:
            self.sessions.append(session)

    def remove_session(self, session: ClientSession):
        with self.lock:
            if session in self.sessions:
                self.sessions.remove(session)

    def increment_packets(self) -> int:
        with self.lock:
            self.packet_count += 1
            return self.packet_count

    def active_count(self) -> int:
        with self.lock:
            return sum(1 for s in self.sessions if s.is_active)

    def broadcast(self, message: str, exclude: Optional[ClientSession] = None):
        """Semantic: deliver a message to every currently-active client except `exclude`."""
        with self.lock:
            dead = []
            for session in self.sessions:
                if session is exclude:
                    continue
                if not session.send(message):
                    dead.append(session)
            for d in dead:
                self.sessions.remove(d)

    def handle_incoming(self, msg: ChatMessage) -> dict:
        """
        Core ordering logic — returns a result dict describing what happened.
        Precondition:  msg is valid (not None)
        Postcondition: ordering state is updated; result describes events
        """
        with self.lock:
            result = {
                "is_duplicate": False,
                "gap_detected": False,
                "missing_id": None,
                "delivered": [],
                "gap_abandoned": None,   # (gave_up_on_id, resumed_at_id) or None
            }

            if self.ordering.is_duplicate(msg.msg_id):
                result["is_duplicate"] = True
                return result

            if self.ordering.is_gap_detected(msg.msg_id):
                result["gap_detected"] = True
                result["missing_id"] = self.ordering.expected_id

            result["delivered"], result["gap_abandoned"] = self.ordering.record_and_deliver(msg)
            return result


# ========================================================
# GUI
# ========================================================

COLORS = ["#00FF9C", "#00E5FF", "#FF4CFF", "#FFD700",
          "#FF6B6B", "#FFFFFF", "#00FF00"]

root = tk.Tk()
root.title("🔥 Smart Ordered Chat Server")
root.geometry("900x650")
root.configure(bg="#0d0d0d")

tk.Label(root, text="APPLICATION LAYER CHAT SERVER",
         bg="#0d0d0d", fg="#00FF9C",
         font=("Consolas", 18, "bold")).pack(pady=10)

chat_box = scrolledtext.ScrolledText(
    root, wrap=tk.WORD, font=("Consolas", 11),
    bg="#000000", fg="#00FF9C", insertbackground="white"
)
chat_box.pack(padx=10, pady=10, fill=tk.BOTH, expand=True)

status_frame = tk.Frame(root, bg="#0d0d0d")
status_frame.pack(fill=tk.X)

online_label = tk.Label(status_frame, text="Online Clients: 0",
                        bg="#0d0d0d", fg="#FFD700",
                        font=("Consolas", 12, "bold"))
online_label.pack(side=tk.LEFT, padx=10)

packet_label = tk.Label(status_frame, text="Packets: 0",
                        bg="#0d0d0d", fg="#00E5FF",
                        font=("Consolas", 12, "bold"))
packet_label.pack(side=tk.RIGHT, padx=10)

entry = tk.Entry(root, font=("Consolas", 13),
                 bg="#1a1a1a", fg="white", insertbackground="white")
entry.pack(fill=tk.X, padx=10, pady=10)


# ========================================================
# GUI OPERATIONS  (always called via root.after — thread-safe)
# ========================================================

def gui_log(message: str, color: str = "white"):
    """Semantic: append a timestamped event to the visible chat log."""
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {message}\n"
    chat_box.insert(tk.END, line)
    start = f"end-{len(line)}c"
    chat_box.tag_add(color, start, "end-1c")
    chat_box.tag_config(color, foreground=color)
    chat_box.yview(tk.END)


def gui_update_status(active: int, packets: int):
    online_label.config(text=f"Online Clients: {active}")
    packet_label.config(text=f"Packets: {packets}")


# Thread-safe wrappers — worker threads call these, never gui_log directly.
def log(message: str, color: str = "white"):
    root.after(0, gui_log, message, color)

def update_status(active: int, packets: int):
    root.after(0, gui_update_status, active, packets)


# ========================================================
# SERVER LOGIC
# ========================================================

HOST = "0.0.0.0"
PORT = 5000

server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

server_state = ServerState()


def handle_client(session: ClientSession):
    """
    Manages the full lifecycle of one client connection.
    Precondition:  session.is_active is True
    Postcondition: session.is_active is False; socket is closed
    """
    log(f"🟢 CONNECTED: {session.addr}", session.color)

    while session.is_active:
        raw = session.receive()
        if raw is None:
            break

        count = server_state.increment_packets()
        update_status(server_state.active_count(), count)

        text = raw.decode(errors="replace").strip()
        msg = ChatMessage.parse(text)

        if msg is None:
            log(f"⚠ PARSE ERROR: {text}", "#FF4444")
            continue

        log(f"[{msg.user}] ID={msg.msg_id} → {msg.text}", session.color)

        # Semantic: acknowledge receipt of THIS message directly to its sender,
        # per the ACK:ID:<n>|STATUS:OK protocol defined in section 3.2.
        session.send(f"ACK:ID:{msg.msg_id}|STATUS:OK")

        result = server_state.handle_incoming(msg)

        if result["is_duplicate"]:
            log(f"⚠ DUPLICATE ID: {msg.msg_id}", "#FF00FF")
            continue

        if result["gap_detected"]:
            log(f"⚠ MISSING MESSAGE ID: {result['missing_id']}", "#FF4444")

        if result["gap_abandoned"]:
            gave_up_on, resumed_at = result["gap_abandoned"]
            log(f"⏭ GAVE UP ON ID {gave_up_on} — RESUMING FROM {resumed_at}", "#FFA500")

        for delivered in result["delivered"]:
            log(f"✔ ORDERED DELIVERY: ID={delivered.msg_id}", "#00FF00")

        # Semantic: re-broadcast the raw message to every OTHER connected client.
        server_state.broadcast(text, exclude=session)

    session.close()
    server_state.remove_session(session)
    log(f"🔴 DISCONNECTED: {session.addr}", "#888888")
    update_status(server_state.active_count(), server_state.ordering.expected_id - 1)


def start_server():
    server_socket.bind((HOST, PORT))
    server_socket.listen(5)
    log(f"🚀 SERVER STARTED ON PORT {PORT}", "#FFD700")

    while True:
        conn, addr = server_socket.accept()
        session = ClientSession(
            conn=conn,
            addr=addr,
            color=random.choice(COLORS)
        )
        server_state.add_session(session)
        update_status(server_state.active_count(),
                      server_state.ordering.expected_id - 1)
        threading.Thread(target=handle_client, args=(session,), daemon=True).start()


# ========================================================
# GUI SEND (server → all clients)
# ========================================================

def send_server_message():
    """Semantic: the server operator is broadcasting a message to all clients."""
    text = entry.get().strip()
    if not text:
        return
    full_msg = f"ID:0|USER:SERVER|MSG:{text}"
    log(f"[SERVER] {text}", "#FFD700")
    server_state.broadcast(full_msg)
    entry.delete(0, tk.END)


tk.Button(root, text="SEND MESSAGE", command=send_server_message,
          bg="#FFD700", fg="black",
          font=("Consolas", 12, "bold"), height=2).pack(pady=10)

# ========================================================
# START
# ========================================================

threading.Thread(target=start_server, daemon=True).start()
root.mainloop()
