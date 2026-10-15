import asyncio
import base64
import ipaddress
import json
import queue
import ssl
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from urllib.parse import urlencode

import websockets


# Default target. You can change this in the app's TV IP field.
DEFAULT_TV_IP = "192.168.1.6"
TV_PORT = 8002

# Samsung stores pairing tokens per TV IP in this file.
TOKEN_FILE = Path.home() / ".samsung_tv_remote.json"

# This is the friendly name sent by the app during the WebSocket handshake.
# It is just a client name, NOT a TLS certificate or security credential.
CLIENT_NAME = "NotAComputer"

# Samsung TVs commonly use a self-signed certificate on this local interface.
# This allows the encrypted TLS connection but DOES NOT verify the TV's identity.
TLS_CONTEXT = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
TLS_CONTEXT.check_hostname = False
TLS_CONTEXT.verify_mode = ssl.CERT_NONE


REMOTE_KEYS = [
    ("Power", "KEY_POWER"),
    ("Source", "KEY_SOURCE"),
    ("Home", "KEY_HOME"),
    ("Volume −", "KEY_VOLDOWN"),
    ("Mute", "KEY_MUTE"),
    ("Volume +", "KEY_VOLUP"),
    ("Channel −", "KEY_CHDOWN"),
    ("Guide", "KEY_GUIDE"),
    ("Channel +", "KEY_CHUP"),
    ("▲", "KEY_UP"),
    ("◀", "KEY_LEFT"),
    ("OK", "KEY_ENTER"),
    ("▶", "KEY_RIGHT"),
    ("▼", "KEY_DOWN"),
    ("Back", "KEY_RETURN"),
    ("Menu", "KEY_MENU"),
    ("Exit", "KEY_EXIT"),
    ("Info", "KEY_INFO"),
    ("Play / Pause", "KEY_PLAY"),
    ("Stop", "KEY_STOP"),
    ("Rewind", "KEY_REWIND"),
    ("Fast-forward", "KEY_FF"),
]


def read_saved_tokens():
    """Read saved per-TV tokens. Invalid or missing files are treated as empty."""
    try:
        data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        tokens = data.get("tokens", {})
        return tokens if isinstance(tokens, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def save_token(ip, token):
    """Persist one TV's token without overwriting tokens for other TVs."""
    tokens = read_saved_tokens()
    tokens[ip] = token
    try:
        TOKEN_FILE.write_text(
            json.dumps({"tokens": tokens}, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        raise OSError(f"Could not save pairing token to {TOKEN_FILE}: {exc}") from exc


class SamsungRemoteApp:
    def __init__(self, root):
        self.root = root
        self.root.title("DefinitelyNotTheNSA Remote")
        self.root.minsize(360, 480)
        self.root.geometry("390x590")

        self.ip_var = tk.StringVar(value=DEFAULT_TV_IP)
        self.status_var = tk.StringVar(value="Disconnected")
        self.note_var = tk.StringVar(value=f"Target: {DEFAULT_TV_IP}:{TV_PORT}")

        self.ui_queue = queue.Queue()
        self.loop = asyncio.new_event_loop()
        self.ws = None
        self.connection_task = None
        self.loop_thread = threading.Thread(
            target=self._run_event_loop,
            name="SamsungRemoteAsyncio",
            daemon=True,
        )
        self.loop_thread.start()

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(100, self._poll_ui_queue)
        # Try the default address on startup.
        self.root.after(250, self.connect)

    def _run_event_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        ttk.Label(
            outer,
            text="Samsung TV Remote",
            font=("Segoe UI", 16, "bold"),
        ).pack(anchor="center", pady=(0, 8))

        connection = ttk.Frame(outer)
        connection.pack(fill="x", pady=(0, 8))
        ttk.Label(connection, text="TV IP:").pack(side="left")
        ttk.Entry(connection, textvariable=self.ip_var).pack(
            side="left", fill="x", expand=True, padx=6
        )
        ttk.Button(connection, text="Connect", command=self.connect).pack(side="left")

        ttk.Label(outer, textvariable=self.status_var).pack(anchor="w")
        ttk.Label(outer, textvariable=self.note_var).pack(anchor="w", pady=(0, 8))

        remote = ttk.Frame(outer)
        remote.pack(fill="both", expand=True)

        # Three-column layout, with navigation controls in a familiar diamond.
        for column in range(3):
            remote.columnconfigure(column, weight=1)

        layout = {
            "Power": (0, 0),
            "Source": (0, 1),
            "Home": (0, 2),
            "Volume −": (1, 0),
            "Mute": (1, 1),
            "Volume +": (1, 2),
            "Channel −": (2, 0),
            "Guide": (2, 1),
            "Channel +": (2, 2),
            "▲": (3, 1),
            "◀": (4, 0),
            "OK": (4, 1),
            "▶": (4, 2),
            "▼": (5, 1),
            "Back": (6, 0),
            "Menu": (6, 1),
            "Exit": (6, 2),
            "Info": (7, 1),
            "Play / Pause": (8, 0),
            "Stop": (8, 1),
            "Rewind": (9, 0),
            "Fast-forward": (9, 2),
        }

        key_by_label = dict(REMOTE_KEYS)
        for label, (row, column) in layout.items():
            button = ttk.Button(
                remote,
                text=label,
                command=lambda key=key_by_label[label]: self.send_key(key),
            )
            button.grid(
                row=row,
                column=column,
                sticky="nsew",
                padx=3,
                pady=3,
                ipadx=2,
                ipady=5,
            )

        ttk.Label(
            outer,
            text="Tip: first connection may ask for permission on the TV.",
            wraplength=350,
        ).pack(anchor="w", pady=(8, 0))

    def _post_status(self, message):
        """Queue a status update so Tkinter is only touched on its own thread."""
        self.ui_queue.put(("status", message))

    def _poll_ui_queue(self):
        try:
            while True:
                kind, message = self.ui_queue.get_nowait()
                if kind == "status":
                    self.status_var.set(message)
        except queue.Empty:
            pass

        if self.root.winfo_exists():
            self.root.after(100, self._poll_ui_queue)

    def connect(self):
        raw_ip = self.ip_var.get().strip()
        try:
            ip = str(ipaddress.IPv4Address(raw_ip))
        except ipaddress.AddressValueError:
            self._post_status("Enter a valid IPv4 address.")
            return

        self.ip_var.set(ip)
        self.note_var.set(f"Target: {ip}:{TV_PORT}")
        self._post_status(f"Connecting to {ip}…")

        token = read_saved_tokens().get(ip)
        asyncio.run_coroutine_threadsafe(self._start_connection(ip, token), self.loop)

    async def _start_connection(self, ip, token):
        # Stop an earlier connection attempt/receive loop before replacing it.
        old_task = self.connection_task
        if old_task and not old_task.done():
            old_task.cancel()
            try:
                await old_task
            except (asyncio.CancelledError, Exception):
                pass

        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None

        self.connection_task = asyncio.create_task(self._connection_loop(ip, token))

    async def _connection_loop(self, ip, token):
        encoded_name = base64.b64encode(CLIENT_NAME.encode("utf-8")).decode("ascii")
        params = {"name": encoded_name}
        if token:
            params["token"] = str(token)

        url = (
            f"wss://{ip}:{TV_PORT}/api/v2/channels/"
            f"samsung.remote.control?{urlencode(params)}"
        )

        try:
            async with websockets.connect(
                url,
                ssl=TLS_CONTEXT,
                open_timeout=8,
                close_timeout=2,
                ping_interval=20,
                ping_timeout=20,
                max_size=1_000_000,
            ) as ws:
                self.ws = ws
                self._post_status(f"Connected to {ip}. Waiting for TV…")

                async for raw_message in ws:
                    try:
                        message = json.loads(raw_message)
                    except (json.JSONDecodeError, TypeError):
                        continue

                    event = message.get("event", "")
                    params = message.get("data", {})
                    if not isinstance(params, dict):
                        params = {}

                    # Samsung sends a token in the channel-connect event on
                    # TVs that support token-based pairing.
                    received_token = params.get("token")
                    if received_token:
                        try:
                            save_token(ip, received_token)
                            self._post_status(f"Paired with {ip}; token saved locally.")
                        except OSError as exc:
                            self._post_status(f"Connected, but token save failed: {exc}")
                    elif event == "ms.channel.connect":
                        self._post_status(f"Connected to {ip}.")

                    elif event == "ms.channel.unauthorized":
                        self._post_status(
                            "TV rejected the connection. Allow the prompt on the TV, "
                            "then press Connect again."
                        )

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._post_status(f"Connection failed: {type(exc).__name__}: {exc}")
        finally:
            if self.ws is not None:
                self.ws = None
            self._post_status("Disconnected.")

    def send_key(self, key):
        asyncio.run_coroutine_threadsafe(self._send_key(key), self.loop)

    async def _send_key(self, key):
        ws = self.ws
        if ws is None:
            self._post_status("Not connected. Press Connect first.")
            return

        payload = {
            "method": "ms.remote.control",
            "params": {
                "Cmd": "Click",
                "DataOfCmd": key,
                "Option": "false",
                "TypeOfRemote": "SendRemoteKey",
            },
        }

        try:
            await ws.send(json.dumps(payload))
            self._post_status(f"Sent {key}.")
        except Exception as exc:
            self._post_status(f"Could not send {key}: {exc}")

    def close(self):
        async def shutdown():
            task = self.connection_task
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            if self.ws is not None:
                try:
                    await self.ws.close()
                except Exception:
                    pass
                self.ws = None
            self.loop.stop()

        try:
            asyncio.run_coroutine_threadsafe(shutdown(), self.loop)
        except RuntimeError:
            pass
        self.root.destroy()


def main():
    root = tk.Tk()
    SamsungRemoteApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
