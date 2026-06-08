# gps_tracker.py
# GPS Follow Me — single unified application
# GUI + GPS reader + KML HTTP server all in one process
# No subprocess required — safe for PyInstaller/Nuitka compilation
#
# Usage (development): python gps_tracker.py
# Usage (compiled):    double-click GPS Tracker.exe

import tkinter as tk
from tkinter import messagebox, filedialog
import subprocess
import threading
import queue
import time
import sys
import os
from http.server import HTTPServer, BaseHTTPRequestHandler

import ctypes

import config

# ============================================================
#  DPI AWARENESS
#  Prevents Windows from upscaling the window on high-DPI
#  displays, which causes a blurry/fuzzy appearance.
# ============================================================
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-monitor DPI aware
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()   # Fallback for older Windows
    except Exception:
        pass

# ============================================================
#  SHARED GPS STATE
#  Written by the GPS reader thread, read by the KML handler.
#  Protected by a lock so both threads access it safely.
# ============================================================

_gps_lock = threading.Lock()
_position  = {"lat": 0.0, "lon": 0.0, "alt": 0.0, "valid": False}


def _set_position(lat, lon, alt):
    with _gps_lock:
        _position["lat"]   = lat
        _position["lon"]   = lon
        _position["alt"]   = alt
        _position["valid"] = True


def _get_position():
    with _gps_lock:
        return dict(_position)


# ============================================================
#  KML BUILDER
# ============================================================

def _build_kml(vehicle_name):
    pos = _get_position()
    if not pos["valid"]:
        return """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>GPS Tracker</name>
    <Placemark>
      <name>Waiting for GPS fix...</name>
      <Point><coordinates>0,0,0</coordinates></Point>
    </Placemark>
  </Document>
</kml>"""

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>GPS Tracker</name>
    <Style id="vehicle">
      <IconStyle>
        <scale>1.2</scale>
        <Icon>
          <href>http://maps.google.com/mapfiles/kml/shapes/arrow.png</href>
        </Icon>
        <color>ff0000ff</color>
      </IconStyle>
      <LabelStyle><scale>1.0</scale></LabelStyle>
    </Style>
    <Placemark>
      <name>{vehicle_name}</name>
      <styleUrl>#vehicle</styleUrl>
      <Point>
        <altitudeMode>clampToGround</altitudeMode>
        <coordinates>{pos["lon"]:.6f},{pos["lat"]:.6f},{pos["alt"]:.1f}</coordinates>
      </Point>
    </Placemark>
  </Document>
</kml>"""


# ============================================================
#  HTTP SERVER
#  Runs in its own thread. Serves position.kml to Google Earth.
# ============================================================

class _KMLHandler(BaseHTTPRequestHandler):

    # vehicle_name is injected by the server thread before starting
    vehicle_name = "My Vehicle"

    def do_GET(self):
        kml = _build_kml(self.__class__.vehicle_name)
        encoded = kml.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.google-earth.kml+xml")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, fmt, *args):
        # Route HTTP log through the app message queue if available
        if hasattr(self.__class__, "_msg_queue") and self.__class__._msg_queue:
            self.__class__._msg_queue.put(("http", None))


class _ReusableHTTPServer(HTTPServer):
    """SO_REUSEADDR — releases the port immediately on shutdown."""
    allow_reuse_address = True


# ============================================================
#  NMEA PARSER
# ============================================================

def _parse_gpgga(sentence):
    try:
        parts = sentence.split(",")
        if len(parts) < 10:
            return None
        if parts[6] in ("0", ""):
            return None
        raw_lat = parts[2]
        if not raw_lat:
            return None
        lat = float(raw_lat[:2]) + float(raw_lat[2:]) / 60.0
        if parts[3] == "S":
            lat = -lat
        raw_lon = parts[4]
        if not raw_lon:
            return None
        lon = float(raw_lon[:3]) + float(raw_lon[3:]) / 60.0
        if parts[5] == "W":
            lon = -lon
        alt = float(parts[9]) if parts[9] else 0.0
        return lat, lon, alt
    except (ValueError, IndexError):
        return None


# ============================================================
#  TRACKING ENGINE
#  Manages the GPS reader thread and HTTP server thread.
#  Communicates with the GUI via a thread-safe message queue.
# ============================================================

class TrackingEngine:
    """
    Owns all backend threads. Created fresh on each Start Tracking click
    and fully torn down on Stop Tracking. Sends status messages to the
    GUI via msg_queue using simple string tags:
        ("status", "waiting")
        ("status", "running")
        ("position", "00.00000, 00.00000  Alt: 0000.0m")
        ("http",   None)
        ("error",  "message text")
        ("stopped", None)
    """

    def __init__(self, cfg, msg_queue):
        self.cfg       = cfg
        self.msg_queue = msg_queue
        self._stop_evt  = threading.Event()  # Signals all threads to exit
        self._gpsbabel  = None               # GPSBabel subprocess handle
        self._server    = None               # HTTP server instance
        self._t_gps     = None               # GPS reader thread (named ref)
        self._t_http    = None               # HTTP server thread (named ref)

    # ---- public ------------------------------------------------

    def start(self):
        """Launches GPS reader and HTTP server threads."""
        self._t_gps = threading.Thread(target=self._gps_reader, daemon=True)
        self._t_gps.start()

        self._t_http = threading.Thread(target=self._http_server, daemon=True)
        self._t_http.start()

    def stop(self):
        """
        Signals all threads to stop and cleans up GPSBabel.
        Called from the GUI's do_stop background thread — never the main thread.
        """
        self._stop_evt.set()

        # Kill GPSBabel subprocess — also unblocks the readline() in _gps_reader
        if self._gpsbabel is not None:
            try:
                self._gpsbabel.terminate()
                self._gpsbabel.wait(timeout=3)
            except Exception:
                try:
                    self._gpsbabel.kill()
                except Exception:
                    pass
            self._gpsbabel = None

        # Force-kill any orphaned gpsbabel.exe on the system
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", "gpsbabel.exe", "/T"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
        except Exception:
            pass

        # Shut down the HTTP server and explicitly close the socket
        # server_close() releases the port immediately on Windows
        if self._server is not None:
            try:
                self._server.shutdown()   # Stops serve_forever() loop
                self._server.server_close()  # Releases the socket — Bug 1 fix
            except Exception:
                pass
            self._server = None

        # Wait for both threads to fully exit (timeout so stop() never hangs)
        # Bug 3 fix: _threads list replaced with named refs so we can join them
        if self._t_gps is not None:
            self._t_gps.join(timeout=5)
            self._t_gps = None
        if self._t_http is not None:
            self._t_http.join(timeout=5)
            self._t_http = None

    # ---- private -----------------------------------------------

    def _put(self, tag, value=None):
        """Thread-safe message send to the GUI queue."""
        self.msg_queue.put((tag, value))

    def _kill_existing_gpsbabel(self):
        """Clear any leftover gpsbabel.exe before opening the USB device."""
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", "gpsbabel.exe"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            time.sleep(3)  # Wait for Windows to release the USB handle
        except Exception:
            pass

    def _gps_reader(self):
        """
        Background thread.
        Kills orphaned GPSBabel, then loops reading NMEA from a fresh
        GPSBabel subprocess, updating shared position state and notifying
        the GUI on each valid fix.
        """
        self._put("status", "waiting")
        self._kill_existing_gpsbabel()

        gpsbabel_path = self.cfg.get(
            "gpsbabel_path",
            r"C:\Program Files\GPSBabel\gpsbabel.exe"
        )
        cmd = [gpsbabel_path, "-T", "-i", "garmin", "-f", "usb:",
               "-o", "nmea", "-F", "-"]

        not_found_retries = 0  # Bug #6 fix: counts consecutive FileNotFoundError hits

        while not self._stop_evt.is_set():
            try:
                self._gpsbabel = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW
                )

                # Bug 2 fix: don't use "for line in stdout" — it blocks
                # forever if GPSBabel hangs. Instead read with a timeout
                # loop so _stop_evt can interrupt it reliably.
                while not self._stop_evt.is_set():
                    # readline() still blocks but GPSBabel termination
                    # (done in stop()) closes the pipe, which unblocks it.
                    # The _stop_evt check after catches any edge cases.
                    line = self._gpsbabel.stdout.readline()
                    if not line:
                        # Empty string means pipe closed — GPSBabel exited
                        break
                    line = line.strip()
                    if line.startswith("$GPGGA"):
                        result = _parse_gpgga(line)
                        if result:
                            _set_position(*result)
                            self._put(
                                "position",
                                f"{result[0]:.5f}, {result[1]:.5f}"
                                f"  Alt: {result[2]:.1f}m"
                            )

                self._gpsbabel.wait()
                self._gpsbabel = None

                if self._stop_evt.is_set():
                    break

                self._put("error", "GPSBabel stopped — retrying in 3s...")
                time.sleep(3)

            except FileNotFoundError:
                not_found_retries += 1
                if not_found_retries >= 3:
                    # Give up — emit "crashed" with a specific message so the
                    # GUI shows it instead of the generic "stopped unexpectedly"
                    # text, then exit the thread cleanly without a second emit.
                    self._put(
                        "crashed",
                        f"GPSBabel not found after {not_found_retries} attempts.\n"
                        f"Path: {gpsbabel_path}\n"
                        "Please check Settings."
                    )
                    return
                self._put(
                    "error",
                    f"GPSBabel not found at:\n{gpsbabel_path}\n"
                    f"Check Settings. (Attempt {not_found_retries}/3)"
                )
                time.sleep(5)
            except Exception as e:
                self._put("error", f"GPS error: {e}")
                time.sleep(3)

        # Only emit "crashed" if the loop exited WITHOUT stop() being called.
        # If stop() set the event, the GUI already knows — no message needed.
        # Bug 4 fix: was always emitting "stopped" which caused a race where
        # a normal shutdown could trigger the "stopped unexpectedly" UI state.
        if not self._stop_evt.is_set():
            self._put("crashed", None)

    def _http_server(self):
        """
        Background thread.
        Starts a local HTTP server that serves position.kml to Google Earth.
        """
        port = self.cfg.get("port", 8080)
        vehicle_name = self.cfg.get("vehicle_name", "My Vehicle")

        # Create a per-session handler subclass so class-level attributes
        # are never shared or clobbered between concurrent TrackingEngine
        # instances (e.g. during a fast stop/start). Bug #1 fix.
        class _SessionKMLHandler(_KMLHandler):
            pass
        _SessionKMLHandler.vehicle_name = vehicle_name
        _SessionKMLHandler._msg_queue   = self.msg_queue

        try:
            self._server = _ReusableHTTPServer(("localhost", port), _SessionKMLHandler)
            # serve_forever loops until shutdown() is called from stop()
            self._server.serve_forever()
        except OSError as e:
            self._put("error", f"Port {port} in use. Change port in Settings.")
        except Exception as e:
            self._put("error", f"HTTP server error: {e}")


# ============================================================
#  GUI
# ============================================================

class GPSTrackerApp:

    def __init__(self, root):
        self.root       = root
        self.engine     = None              # TrackingEngine instance
        self.msg_queue  = queue.Queue()     # Engine → GUI messages
        self.session_id = 0                 # Invalidates stale queue messages
        self._ge_connected = False          # True after first GE poll — suppresses repeat messages
        self._closing      = False          # True when the window-close button was clicked

        # Load config on startup
        self.cfg = config.load()

        # ---- DPI scaling ----
        # With DPI awareness enabled, tkinter works in true physical pixels.
        # We detect the scale factor and multiply window dimensions so the
        # layout stays the same physical size on all displays.
        try:
            dpi_scale = self.root.winfo_fpixels('1i') / 96.0
        except Exception:
            dpi_scale = 1.0
        self._dpi_scale = dpi_scale

        # ---- Window ----
        self.root.title("GPS Tracker")
        w = int(320 * dpi_scale)
        h = int(300 * dpi_scale)
        self.root.geometry(f"{w}x{h}")
        self.root.resizable(False, False)
        self.root.configure(bg="#f5f5f5")

        # ---- Title bar ----
        title_frame = tk.Frame(self.root, bg="#1a1a2e", height=56)
        title_frame.pack(fill="x")
        title_frame.pack_propagate(False)

        tk.Label(
            title_frame,
            text="  \u25cb  GPS Follow Me",
            font=("Segoe UI", 13, "bold"),
            fg="#ffffff", bg="#1a1a2e", anchor="w"
        ).pack(side="left", padx=16, pady=14)

        tk.Button(
            title_frame,
            text="Settings",
            font=("Segoe UI", 9),
            fg="#aaaacc", bg="#1a1a2e",
            activebackground="#2a2a4e", activeforeground="#ffffff",
            relief="flat", cursor="hand2", bd=0,
            command=self.open_settings
        ).pack(side="right", padx=12, pady=16)

        # ---- Footer button (packed before top content) ----
        self.link_btn = tk.Button(
            self.root,
            text="\u2197 Open in Google Earth",
            font=("Segoe UI", 9, "bold"),
            fg="#bbbbbb", bg="#f5f5f5",
            activeforeground="#1f6391",
            relief="flat", cursor="arrow", state="disabled",
            command=self.generate_network_link
        )
        self.link_btn.pack(side="bottom", pady=10)

        # ---- Status indicator ----
        status_frame = tk.Frame(self.root, bg="#f5f5f5")
        status_frame.pack(pady=(20, 4))

        self.status_dot = tk.Label(
            status_frame, text="\u25cf",
            font=("Segoe UI", 18), fg="#cccccc", bg="#f5f5f5"
        )
        self.status_dot.pack(side="left", padx=(0, 6))

        self.status_label = tk.Label(
            status_frame, text="Not running",
            font=("Segoe UI", 11), fg="#888888", bg="#f5f5f5"
        )
        self.status_label.pack(side="left")

        # ---- Log line ----
        self.log_label = tk.Label(
            self.root, text="",
            font=("Segoe UI", 9), fg="#aaaaaa", bg="#f5f5f5", wraplength=int(290 * dpi_scale)
        )
        self.log_label.pack(pady=(2, 0))

        # ---- Start / Stop buttons ----
        btn_frame = tk.Frame(self.root, bg="#f5f5f5")
        btn_frame.pack(pady=22)

        self.start_btn = tk.Button(
            btn_frame,
            text="Start Tracking",
            font=("Segoe UI", 12, "bold"),
            fg="#ffffff", bg="#1a7a4a",
            activebackground="#155c38", activeforeground="#ffffff",
            relief="flat", width=14, height=2, cursor="hand2",
            command=self.start_tracking
        )
        self.start_btn.grid(row=0, column=0, padx=(0, 4))

        self.stop_btn = tk.Button(
            btn_frame,
            text="Stop Tracking",
            font=("Segoe UI", 12, "bold"),
            fg="#ffffff", bg="#cccccc",
            activebackground="#aaaaaa", activeforeground="#ffffff",
            relief="flat", width=14, height=2, cursor="hand2",
            state="disabled",
            command=self.stop_tracking
        )
        self.stop_btn.grid(row=0, column=1, padx=(4, 0))

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._poll_queue()

    # ---- Settings window ---------------------------------------

    def open_settings(self):
        if self.engine is not None:
            messagebox.showwarning(
                "Tracking Active",
                "Please stop tracking before changing settings.",
                parent=self.root
            )
            return

        win = tk.Toplevel(self.root)
        win.title("Settings")
        win.geometry(f"{int(400 * self._dpi_scale)}x{int(240 * self._dpi_scale)}")
        win.resizable(False, False)
        win.configure(bg="#f5f5f5")
        win.grab_set()

        pad = {"padx": 16, "pady": 6}

        # GPSBabel path
        tk.Label(win, text="GPSBabel Path", font=("Segoe UI", 9, "bold"),
                 bg="#f5f5f5", anchor="w").pack(fill="x", **pad)

        path_frame = tk.Frame(win, bg="#f5f5f5")
        path_frame.pack(fill="x", padx=16)

        gpsbabel_var = tk.StringVar(value=self.cfg.get("gpsbabel_path", ""))
        tk.Entry(path_frame, textvariable=gpsbabel_var,
                 font=("Segoe UI", 9), relief="solid", bd=1
                 ).pack(side="left", fill="x", expand=True)

        def browse():
            chosen = filedialog.askopenfilename(
                parent=win, title="Select gpsbabel.exe",
                filetypes=[("Executable", "gpsbabel.exe"), ("All files", "*.*")]
            )
            if chosen:
                gpsbabel_var.set(chosen.replace("/", "\\"))

        tk.Button(path_frame, text="Browse…", font=("Segoe UI", 9),
                  relief="flat", bg="#e0e0e0", cursor="hand2",
                  command=browse).pack(side="left", padx=(6, 0))

        # Vehicle name
        tk.Label(win, text="Vehicle Name", font=("Segoe UI", 9, "bold"),
                 bg="#f5f5f5", anchor="w").pack(fill="x", **pad)
        name_var = tk.StringVar(value=self.cfg.get("vehicle_name", ""))
        tk.Entry(win, textvariable=name_var, font=("Segoe UI", 9),
                 relief="solid", bd=1).pack(fill="x", padx=16)

        # Port
        tk.Label(win, text="KML Server Port", font=("Segoe UI", 9, "bold"),
                 bg="#f5f5f5", anchor="w").pack(fill="x", **pad)
        port_var = tk.StringVar(value=str(self.cfg.get("port", 8080)))
        tk.Entry(win, textvariable=port_var, font=("Segoe UI", 9),
                 relief="solid", bd=1, width=10).pack(anchor="w", padx=16)

        def save_settings():
            try:
                port_val = int(port_var.get())
                if not (1024 <= port_val <= 65535):
                    raise ValueError
            except ValueError:
                messagebox.showerror(
                    "Invalid Port",
                    "Port must be a number between 1024 and 65535.",
                    parent=win
                )
                return

            if not os.path.isfile(gpsbabel_var.get()):
                if not messagebox.askyesno(
                    "Path Not Found",
                    f"GPSBabel was not found at:\n{gpsbabel_var.get()}\n\n"
                    "Save anyway?",
                    parent=win
                ):
                    return

            new_cfg = {
                "gpsbabel_path": gpsbabel_var.get(),
                "vehicle_name":  name_var.get().strip() or "My Vehicle",
                "port":          port_val
            }
            try:
                config.save(new_cfg)
                self.cfg = new_cfg
                self.log_label.config(
                    text="Settings saved — restart tracking to apply.",
                    fg="#e67e22"
                )
                win.destroy()
            except Exception as e:
                messagebox.showerror("Save Failed", str(e), parent=win)

        btn_row = tk.Frame(win, bg="#f5f5f5")
        btn_row.pack(pady=14)

        tk.Button(btn_row, text="Save", font=("Segoe UI", 10, "bold"),
                  fg="#ffffff", bg="#1a7a4a", activebackground="#155c38",
                  activeforeground="#ffffff", relief="flat", width=10,
                  cursor="hand2", command=save_settings
                  ).pack(side="left", padx=6)

        tk.Button(btn_row, text="Cancel", font=("Segoe UI", 10),
                  fg="#555555", bg="#e0e0e0", activebackground="#cccccc",
                  relief="flat", width=10, cursor="hand2",
                  command=win.destroy).pack(side="left", padx=6)

    # ---- Tracking control --------------------------------------

    def start_tracking(self):
        if self.engine is not None:
            return

        # Reload config so latest saved settings are used
        self.cfg = config.load()

        self.session_id += 1
        current_session = self.session_id
        self._ge_connected = False          # Reset for new session

        # Create a fresh engine with a tagged queue wrapper
        # so stale messages from a previous session are discarded
        tagged_queue = _TaggedQueue(self.msg_queue, current_session)
        self.engine = TrackingEngine(self.cfg, tagged_queue)
        self.engine.start()

        self.set_status("waiting", "Waiting for fix...")
        self.log_label.config(text="Starting GPSBabel...", fg="#aaaaaa")
        self.start_btn.config(state="disabled", bg="#aaaaaa")
        self.stop_btn.config(
            state="normal", bg="#c0392b",
            activebackground="#922b21", cursor="hand2"
        )

    def stop_tracking(self):
        if self.engine is None:
            return

        engine = self.engine
        self.engine = None
        self.session_id += 1  # Invalidate any queued messages from this session

        self.set_status("stopped", "Not running")
        self.log_label.config(
            text="Stopping — cleaning up GPSBabel...", fg="#aaaaaa"
        )
        self.start_btn.config(state="disabled", bg="#aaaaaa")
        self.stop_btn.config(state="disabled", bg="#cccccc")
        self.link_btn.config(state="disabled", fg="#bbbbbb", cursor="arrow")

        # Stop the engine in a background thread so the GUI stays responsive
        def do_stop():
            engine.stop()
            self.root.after(0, self._after_stop)

        threading.Thread(target=do_stop, daemon=True).start()

    def _after_stop(self):
        if self._closing:
            self.root.destroy()
            return
        self.log_label.config(text="Tracking stopped.", fg="#aaaaaa")
        self.start_btn.config(state="normal", bg="#1a7a4a")

    # ---- Queue polling -----------------------------------------

    def _poll_queue(self):
        """
        Runs on the main thread every 200ms.
        Reads (session_id, tag, value) tuples from the queue.
        Discards any message whose session_id is stale.
        """
        try:
            while True:
                session_id, tag, value = self.msg_queue.get_nowait()

                if session_id != self.session_id:
                    continue  # Stale message — discard

                if tag == "position":
                    self.set_status("running", "Tracking active")
                    self.log_label.config(
                        text=f"\u25cf  Position updated: {value}",
                        fg="#1a7a4a"
                    )
                    self.link_btn.config(
                        state="normal", fg="#2980b9", cursor="hand2"
                    )

                elif tag == "status" and value == "waiting":
                    self.set_status("waiting", "Waiting for fix...")
                    self.log_label.config(
                        text="Waiting for satellite fix...", fg="#e67e22"
                    )

                elif tag == "http":
                    # Only show once per session — Google Earth polls every
                    # 3 seconds so without this the label flickers constantly
                    if not self._ge_connected:
                        self._ge_connected = True
                        self.log_label.config(
                            text="\u25ce  Google Earth connected", fg="#2980b9"
                        )

                elif tag == "error":
                    self.log_label.config(text=value, fg="#c0392b")

                elif tag == "crashed":
                    # GPS reader exited without stop() being called —
                    # genuine unexpected failure. Reset the UI.
                    # Bug 4 fix: renamed from "stopped" to "crashed" so normal
                    # shutdown no longer triggers this branch.
                    # Bug #6 fix: value now carries an optional custom message
                    # (e.g. from the FileNotFoundError give-up path) so the
                    # label shows the real reason instead of the generic text.
                    if self.engine is not None:
                        self.engine = None
                        self.set_status("stopped", "Not running")
                        self.log_label.config(
                            text=value or "Tracking stopped unexpectedly.",
                            fg="#c0392b"
                        )
                        self.start_btn.config(state="normal", bg="#1a7a4a")
                        self.stop_btn.config(state="disabled", bg="#cccccc")
                        self.link_btn.config(
                            state="disabled", fg="#bbbbbb", cursor="arrow"
                        )

        except queue.Empty:
            pass

        self.root.after(200, self._poll_queue)

    # ---- Network link ------------------------------------------

    def generate_network_link(self):
        port = self.cfg.get("port", 8080)
        kml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <NetworkLink>
    <name>Live Vehicle Position</name>
    <Link>
      <href>http://localhost:{port}/position.kml</href>
      <refreshMode>onInterval</refreshMode>
      <refreshInterval>3</refreshInterval>
    </Link>
  </NetworkLink>
</kml>"""
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        file_path = os.path.join(desktop, "GPS_Live_Tracker.kml")
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(kml_content)
            self.log_label.config(
                text="\u25ce  Launching Google Earth...", fg="#2980b9"
            )
            os.startfile(file_path)
        except Exception as e:
            self.log_label.config(
                text=f"Failed to create link: {e}", fg="#c0392b"
            )

    # ---- Helpers -----------------------------------------------

    def set_status(self, state, text):
        colors = {
            "running": "#1a7a4a",
            "waiting": "#e67e22",
            "stopped": "#cccccc"
        }
        self.status_dot.config(fg=colors.get(state, "#cccccc"))
        self.status_label.config(text=text, fg=colors.get(state, "#888888"))

    def on_close(self):
        # Bug #3 fix: the old 800ms timer races with the 10s engine stop timeout.
        # Instead, set a flag and let _after_stop call root.destroy() when done.
        self._closing = True
        if self.engine is None:
            self.root.destroy()   # Nothing running — safe to destroy immediately.
        else:
            self.stop_tracking()  # Background thread will call _after_stop → destroy.


# ============================================================
#  TAGGED QUEUE WRAPPER
#  Stamps every message with the current session_id so the GUI
#  can discard stale messages from previous tracking sessions.
# ============================================================

class _TaggedQueue:
    def __init__(self, q, session_id):
        self._q          = q
        self._session_id = session_id

    def put(self, msg):
        tag, value = msg
        self._q.put((self._session_id, tag, value))


# ============================================================

if __name__ == "__main__":
    root = tk.Tk()
    app = GPSTrackerApp(root)
    root.mainloop()
