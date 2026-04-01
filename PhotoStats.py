#!/usr/bin/env python3
"""
PhotoStats
A Tkinter desktop app that connects to a Piwigo instance and reports
on photos added to albums within a selected date range.
Last-run date is persisted to a local JSON file.
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import json
import os
import re
import sys
import threading
import requests
from datetime import datetime, date, timedelta
from pathlib import Path
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import matplotlib.ticker as mticker

# ── Config / persistence ────────────────────────────────────────────────────

STATE_FILE = Path(".") / "PhotoStats State.json"

# Params file lives next to the script
PARAMS_FILE = Path(".") / "PhotoStats Params.json"

REQUIRED_PARAMS = ("url", "username", "password")

def load_params() -> dict:
    """Load connection parameters from PhotoStats Params.json."""
    if not PARAMS_FILE.exists():
        raise FileNotFoundError(
            f"Parameters file not found: {PARAMS_FILE}\n\n"
            "Please create PhotoStats Params.json next to this script with:\n"
            '{\n'
            '  "url": "https://your-piwigo-site.example.com",\n'
            '  "username": "your-username-here",\n'
            '  "password": "your-password-here",\n'
            '  "verify_ssl": false\n'
            '}'
        )
    with open(PARAMS_FILE) as f:
        params = json.load(f)
    missing = [k for k in REQUIRED_PARAMS if not params.get(k)]
    if missing:
        raise ValueError(f"Missing required fields in PhotoStats Params.json: {', '.join(missing)}")
    return params

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── Piwigo API helpers ───────────────────────────────────────────────────────

class PiwigoClient:
    def __init__(self, base_url: str, username: str, password: str, verify_ssl: bool = True):
        url = base_url.strip().rstrip("/")
        if url.startswith("http://"):
            url = "https://" + url[7:]
        elif not url.startswith("https://"):
            url = "https://" + url
        self.base_url = url
        self.api_url = f"{self.base_url}/ws.php?format=json"
        self.session = requests.Session()
        self.session.verify = verify_ssl
        if not verify_ssl:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self.username = username
        self.password = password

    def _call(self, method: str, params: dict = None) -> dict:
        payload = {"method": method}
        if params:
            payload.update(params)
        r = self.session.post(self.api_url, data=payload, timeout=30)
        r.raise_for_status()
        try:
            data = r.json()
        except ValueError:
            preview = r.text[:300].strip() if r.text else "(empty)"
            raise RuntimeError(
                f"The server did not return a valid response for '{method}'.\n\n"
                f"This usually means the URL is wrong, the server requires a different "
                f"protocol, or the API endpoint could not be reached.\n\n"
                f"URL: {self.api_url}\n"
                f"HTTP status: {r.status_code}\n"
                f"Response preview: {preview}"
            )
        if data.get("stat") != "ok":
            raise RuntimeError(data.get("message", "Unknown Piwigo API error"))
        return data.get("result", {})

    def login(self):
        self._call("pwg.session.login", {
            "username": self.username,
            "password": self.password
        })

    def logout(self):
        try:
            self._call("pwg.session.logout")
        except Exception:
            pass

    def get_albums(self) -> list:
        result = self._call("pwg.categories.getList", {
            "recursive": "true",
            "fullname": "true"
        })
        return result.get("categories", [])

    def get_images_for_album(self, album_id: int) -> list:
        """Fetch all images for a given album (handles pagination)."""
        images = []
        page = 0
        per_page = 200
        while True:
            result = self._call("pwg.categories.getImages", {
                "cat_id": album_id,
                "per_page": per_page,
                "page": page,
                "order": "date_available"
            })
            batch = result.get("images", [])
            images.extend(batch)
            paging = result.get("paging", {})
            count = int(paging.get("count", len(batch)))
            if count < per_page:
                break
            page += 1
        return images


def build_report(client: PiwigoClient, start_dt: datetime, end_dt: datetime,
                 progress_cb=None) -> list:
    """
    Returns list of dicts: {album_id, album_name, count, photos: [...]}
    A photo is counted in the album on the date it appears in that album
    (date_available field from the API).
    """
    albums = client.get_albums()
    rows = []
    total = len(albums)
    for idx, album in enumerate(albums):
        if progress_cb:
            progress_cb(idx, total, album.get("name", ""))
        album_id = int(album["id"])
        fullname = album.get("name", f"Album {album_id}")
        images = client.get_images_for_album(album_id)
        in_range = []
        for img in images:
            date_str = img.get("date_available") or img.get("date_creation") or ""
            if not date_str:
                continue
            try:
                # date_available is "YYYY-MM-DD HH:MM:SS" or "YYYY-MM-DD"
                dt = datetime.fromisoformat(date_str[:19])
            except ValueError:
                continue
            if start_dt <= dt <= end_dt:
                in_range.append({
                    "id": img.get("id"),
                    "name": img.get("name") or img.get("file", ""),
                    "date": date_str[:10],
                })
        if in_range:
            rows.append({
                "album_id": album_id,
                "album_name": fullname,
                "count": len(in_range),
                "photos": in_range,
            })
    if progress_cb:
        progress_cb(total, total, "Done")
    return sorted(rows, key=lambda x: x["count"], reverse=True)


# ── Window geometry helpers ──────────────────────────────────────────────────

def _window_is_on_a_monitor(hwnd: int) -> bool:
    """Return True if any part of the window is on a connected monitor."""
    try:
        import ctypes
        MONITOR_DEFAULTTONULL = 0
        return bool(ctypes.windll.user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONULL))
    except Exception:
        return True  # assume visible if the API fails


# ── Main Application ─────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.withdraw()  # stay hidden until fully built — prevents init flicker
        self.title("PhotoStats")
        self.geometry("820x750")
        self.minsize(640, 600)
        self.resizable(True, True)

        # State is loaded early so geometry is available for any prompt dialog
        self.state_data = load_state()

        # Resolve the startup position once so both the params dialog and the
        # main window use exactly the same location.
        self._win_x, self._win_y, self._win_w, self._win_h = self._resolve_startup_geometry()

        # Load params file; if missing or incomplete, prompt the user
        try:
            self.params = load_params()
        except (FileNotFoundError, ValueError):
            existing = {}
            if PARAMS_FILE.exists():
                try:
                    with open(PARAMS_FILE) as f:
                        existing = json.load(f)
                except Exception:
                    pass
            params = self._prompt_missing_params(existing)
            if params is None:
                self.destroy()
                return
            self.params = params

        self._report_data = []
        self._current_fig = None
        self._client = None

        self._build_ui()
        self.update_idletasks()
        self._restore_geometry()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.deiconify()  # show the fully-built window

    # ── Parameters dialog ────────────────────────────────────────────────

    def _prompt_missing_params(self, existing: dict):
        """Show a modal dialog to collect missing connection parameters.
        Saves the params file on confirmation. Returns the params dict,
        or None if the user cancelled."""
        out = [None]

        dlg = tk.Toplevel(self)
        dlg.title("PhotoStats — Connection Setup")
        dlg.resizable(False, False)
        dlg.grab_set()

        pad = {"padx": 8, "pady": 4}
        frm = ttk.Frame(dlg, padding=16)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="Piwigo URL:").grid(row=0, column=0, sticky="e", **pad)
        var_url = tk.StringVar(value=existing.get("url", ""))
        ttk.Entry(frm, textvariable=var_url, width=36).grid(row=0, column=1, sticky="w", **pad)

        ttk.Label(frm, text="Username:").grid(row=1, column=0, sticky="e", **pad)
        var_user = tk.StringVar(value=existing.get("username", ""))
        ttk.Entry(frm, textvariable=var_user, width=36).grid(row=1, column=1, sticky="w", **pad)

        ttk.Label(frm, text="Password:").grid(row=2, column=0, sticky="e", **pad)
        var_pwd = tk.StringVar(value=existing.get("password", ""))
        ttk.Entry(frm, textvariable=var_pwd, width=36, show="*").grid(row=2, column=1, sticky="w", **pad)

        var_ssl = tk.BooleanVar(value=existing.get("verify_ssl", False))
        ttk.Checkbutton(frm, text="Verify SSL certificate", variable=var_ssl).grid(
            row=3, column=1, sticky="w", **pad)

        def on_ok():
            url = var_url.get().strip()
            username = var_user.get().strip()
            password = var_pwd.get()
            missing = [k for k, v in [("url", url), ("username", username), ("password", password)] if not v]
            if missing:
                messagebox.showerror("Missing fields",
                                     f"Please fill in: {', '.join(missing)}", parent=dlg)
                return
            params = {"url": url, "username": username,
                      "password": password, "verify_ssl": var_ssl.get()}
            with open(PARAMS_FILE, "w") as f:
                json.dump(params, f, indent=2)
            out[0] = params
            dlg.destroy()

        def on_cancel():
            dlg.destroy()

        btn_row = ttk.Frame(frm)
        btn_row.grid(row=4, column=0, columnspan=2, pady=(8, 0))
        ttk.Button(btn_row, text="OK", command=on_ok, width=10).pack(side="left", padx=4)
        ttk.Button(btn_row, text="Cancel", command=on_cancel, width=10).pack(side="left", padx=4)

        dlg.bind("<Return>", lambda _: on_ok())
        dlg.bind("<Escape>", lambda _: on_cancel())

        # Centre the dialog within the area the main window will occupy,
        # using the position resolved once at startup.
        dlg.update_idletasks()
        dlg_w = dlg.winfo_reqwidth()
        dlg_h = dlg.winfo_reqheight()
        x = self._win_x + (self._win_w - dlg_w) // 2
        y = self._win_y + (self._win_h - dlg_h) // 2
        dlg.geometry(f"+{x}+{y}")

        self.wait_window(dlg)
        return out[0]

    # ── UI Construction ──────────────────────────────────────────────────

    def _build_ui(self):
        # ── Top: connection frame (username only; url/password/ssl in params file) ──
        conn_frame = ttk.LabelFrame(self, text="Piwigo Connection", padding=8)
        conn_frame.pack(fill="x", padx=10, pady=(10, 4))

        ttk.Label(conn_frame, text="Site:").grid(row=0, column=0, sticky="e", padx=4)
        site_label = ttk.Label(conn_frame, text=self.params.get("url", ""), foreground="steelblue")
        site_label.grid(row=0, column=1, sticky="w", padx=4)

        ttk.Label(conn_frame, text="User:").grid(row=0, column=2, sticky="e", padx=4)
        ttk.Label(conn_frame, text=self.params.get("username", ""), foreground="steelblue").grid(row=0, column=3, sticky="w", padx=4)

        ssl_state = "✅ SSL verified" if self.params.get("verify_ssl", True) else "⚠️ SSL verification off"
        ttk.Label(conn_frame, text=ssl_state, foreground="gray").grid(row=0, column=4, padx=12)

        conn_frame.columnconfigure(1, weight=1)

        # ── Button row ───────────────────────────────────────────────────
        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", padx=10, pady=(4, 0))

        self.btn_run = ttk.Button(btn_frame, text="▶  Run Report", command=self._run_report)
        self.btn_run.pack(side="left", padx=(0, 4))

        self.btn_save = ttk.Button(btn_frame, text="💾  Save Reports", command=self._save_reports, state="disabled")
        self.btn_save.pack(side="left", padx=4)

        self.btn_export = ttk.Button(btn_frame, text="⬇  Export CSV", command=self._export_csv, state="disabled")
        self.btn_export.pack(side="left", padx=4)

        ttk.Button(btn_frame, text="✕  Exit", command=self._on_close).pack(side="left", padx=4)

        # ── Date range frame ─────────────────────────────────────────────
        date_frame = ttk.LabelFrame(self, text="Date Range", padding=8)
        date_frame.pack(fill="x", padx=10, pady=4)

        last_run = self.state_data.get("last_run")
        default_start = last_run[:10] if last_run else (date.today() - timedelta(days=30)).isoformat()
        default_end = date.today().isoformat()

        ttk.Label(date_frame, text="From (YYYY-MM-DD):").grid(row=0, column=0, sticky="e", padx=4)
        self.var_start = tk.StringVar(value=default_start)
        ttk.Entry(date_frame, textvariable=self.var_start, width=14).grid(row=0, column=1, padx=4)

        ttk.Label(date_frame, text="To (YYYY-MM-DD):").grid(row=0, column=2, sticky="e", padx=4)
        self.var_end = tk.StringVar(value=default_end)
        ttk.Entry(date_frame, textvariable=self.var_end, width=14).grid(row=0, column=3, padx=4)

        if last_run:
            ttk.Label(date_frame, text=f"Last report run: {last_run[:16]}",
                      foreground="gray").grid(row=0, column=4, padx=12)

        # ── Progress ─────────────────────────────────────────────────────
        self.var_progress = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.var_progress, foreground="steelblue").pack(anchor="w", padx=12)
        self.progress_bar = ttk.Progressbar(self, mode="determinate")
        self.progress_bar.pack(fill="x", padx=10, pady=(0, 4))

        # ── Notebook: Table + Chart ──────────────────────────────────────
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        # Table tab
        tab_table = ttk.Frame(self.notebook)
        self.notebook.add(tab_table, text="📋  Table")

        cols = ("Album", "Photos Added")
        self.tree = ttk.Treeview(tab_table, columns=cols, show="headings", selectmode="browse")
        for col in cols:
            self.tree.heading(col, text=col, command=lambda c=col: self._sort_tree(c))
            self.tree.column(col, anchor="w" if col == "Album" else "center",
                             width=500 if col == "Album" else 120)
        vsb = ttk.Scrollbar(tab_table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        # Chart tab
        self.tab_chart = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_chart, text="📊  Chart")
        self._chart_placeholder()

    def _chart_placeholder(self):
        ttk.Label(self.tab_chart, text="Run a report to see the chart.",
                  foreground="gray").pack(expand=True)

    # ── Window geometry ───────────────────────────────────────────────────

    def _resolve_startup_geometry(self):
        """Parse the saved geometry string and return (x, y, w, h).

        Returns the saved values if present, otherwise sensible defaults.
        The position is not validated against connected monitors here —
        that happens later in _restore_geometry once the window is visible.
        """
        geom = self.state_data.get("geometry", "")
        normalised = geom.replace("+-", "-").replace("--", "+")
        m = re.fullmatch(r'(\d+)x(\d+)([+-]\d+)([+-]\d+)', normalised)
        if m:
            return int(m.group(3)), int(m.group(4)), int(m.group(1)), int(m.group(2))
        return 100, 100, 820, 750  # defaults when no geometry is saved

    def _restore_geometry(self):
        """Apply the pre-resolved startup position/size to the main window.

        Uses the values computed by _resolve_startup_geometry so that the
        position is determined in exactly one place.  If the window lands
        off all connected monitors (e.g. a previously-used monitor has been
        unplugged) it is snapped to the primary screen instead.
        """
        if not self.state_data.get("geometry"):
            return
        x, y, w, h = self._win_x, self._win_y, self._win_w, self._win_h
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.update_idletasks()

        # If the window is completely off all connected monitors, snap it home
        if not _window_is_on_a_monitor(self.winfo_id()):
            min_w, min_h = self.minsize()
            self.geometry(f"{max(w, min_w)}x{max(h, min_h)}+100+100")

    def _on_close(self):
        self.update_idletasks()
        # Save as "+{int}" format (same as _restore_geometry uses) so the saved
        # string is unambiguous regardless of what Tk emits on this platform.
        x, y = self.winfo_x(), self.winfo_y()
        w, h = self.winfo_width(), self.winfo_height()
        self.state_data["geometry"] = f"{w}x{h}+{x}+{y}"
        save_state(self.state_data)
        self.destroy()

    # ── Report execution ─────────────────────────────────────────────────

    def _run_report(self):
        url = self.params.get("url", "").strip()
        user = self.params.get("username", "")
        pwd = self.params.get("password", "")
        verify_ssl = self.params.get("verify_ssl", True)
        start_str = self.var_start.get().strip()
        end_str = self.var_end.get().strip()

        try:
            start_dt = datetime.fromisoformat(start_str)
        except ValueError:
            messagebox.showerror("Bad date", f"Invalid start date: {start_str}")
            return
        try:
            end_dt = datetime.fromisoformat(end_str + " 23:59:59")
        except ValueError:
            messagebox.showerror("Bad date", f"Invalid end date: {end_str}")
            return

        self.btn_run.config(state="disabled")
        self.btn_export.config(state="disabled")
        self.btn_save.config(state="disabled")
        self._clear_results()

        def worker():
            try:
                client = PiwigoClient(url, user, pwd, verify_ssl=verify_ssl)
                self._update_progress(0, 1, "Logging in…")
                client.login()

                run_start = datetime.now()

                def progress_cb(idx, total, name):
                    self.after(0, self._update_progress, idx, total,
                               f"Scanning album: {name} ({idx}/{total})")

                data = build_report(client, start_dt, end_dt, progress_cb)
                client.logout()

                # Save state (last run timestamp)
                self.state_data["last_run"] = run_start.isoformat()
                save_state(self.state_data)

                self.after(0, self._display_results, data)
            except Exception as e:
                self.after(0, messagebox.showerror, "Error", str(e))
                self.after(0, self.btn_run.config, {"state": "normal"})
                self.after(0, self._update_progress, 0, 1, "")

        threading.Thread(target=worker, daemon=True).start()

    def _update_progress(self, idx, total, msg):
        self.var_progress.set(msg)
        self.progress_bar["maximum"] = max(total, 1)
        self.progress_bar["value"] = idx

    def _clear_results(self):
        for row in self.tree.get_children():
            self.tree.delete(row)
        for w in self.tab_chart.winfo_children():
            w.destroy()
        if self._current_fig is not None:
            plt.close(self._current_fig)
            self._current_fig = None

    def _display_results(self, data: list):
        self._report_data = data
        self._populate_table(data)
        self._populate_chart(data)
        self.btn_run.config(state="normal")
        self.btn_export.config(state="normal")
        self.btn_save.config(state="normal")
        self._update_progress(1, 1,
            f"Report complete — {sum(r['count'] for r in data)} photos across {len(data)} albums.")

    # ── Table ────────────────────────────────────────────────────────────

    def _populate_table(self, data: list):
        self._sort_col = None
        for row in data:
            self.tree.insert("", "end", values=(row["album_name"], row["count"]))

    def _sort_tree(self, col):
        rows = [(self.tree.set(k, col), k) for k in self.tree.get_children("")]
        reverse = getattr(self, "_sort_reverse", False)
        if getattr(self, "_sort_col", None) == col:
            reverse = not reverse
        else:
            reverse = col == "Photos Added"  # default desc for counts
        self._sort_col = col
        self._sort_reverse = reverse
        try:
            rows.sort(key=lambda x: int(x[0]), reverse=reverse)
        except ValueError:
            rows.sort(key=lambda x: x[0].lower(), reverse=reverse)
        for i, (_, k) in enumerate(rows):
            self.tree.move(k, "", i)

    # ── Chart ────────────────────────────────────────────────────────────

    def _populate_chart(self, data: list):
        for w in self.tab_chart.winfo_children():
            w.destroy()

        if not data:
            ttk.Label(self.tab_chart, text="No data to display.", foreground="gray").pack(expand=True)
            return None

        # Show top 20 albums for readability
        top = data[:20]
        names = [r["album_name"] for r in top]
        counts = [r["count"] for r in top]

        # Shorten long names
        max_len = 35
        short_names = [n if len(n) <= max_len else "…" + n[-(max_len-1):] for n in names]

        fig, ax = plt.subplots(figsize=(9, max(4, len(top) * 0.38)))
        bars = ax.barh(short_names[::-1], counts[::-1], color="steelblue", edgecolor="white")
        ax.bar_label(bars, padding=4, fontsize=9)
        ax.set_xlabel("Photos Added")
        ax.set_title(f"Photos Added by Album\n({self.var_start.get()} → {self.var_end.get()})")
        ax.xaxis.set_major_locator(mticker.MaxNLocator(integer=True))
        fig.tight_layout()

        canvas = FigureCanvasTkAgg(fig, master=self.tab_chart)
        canvas.draw()
        canvas.get_tk_widget().pack(fill="both", expand=True)
        self._current_fig = fig

    # ── Save Reports ─────────────────────────────────────────────────────

    def _save_reports(self):
        if not self._report_data:
            return
        import csv
        stem = f"PhotoStats {self.var_start.get()} to {self.var_end.get()}"
        out_dir = Path(".")

        csv_path = out_dir / f"{stem}.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Album", "Photos Added", "Photo Name", "Date Added"])
            for row in self._report_data:
                for photo in row["photos"]:
                    writer.writerow([row["album_name"], row["count"],
                                     photo["name"], photo["date"]])

        png_path = out_dir / f"{stem}.png"
        if self._current_fig is not None:
            self._current_fig.savefig(png_path, dpi=150, bbox_inches="tight")
            messagebox.showinfo("Saved", f"Reports saved to:\n{csv_path}\n{png_path}")
        else:
            messagebox.showinfo("Saved", f"Report saved to:\n{csv_path}")

    # ── Export CSV ───────────────────────────────────────────────────────

    def _export_csv(self):
        if not self._report_data:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialfile=f"PhotoStats_{date.today().isoformat()}.csv"
        )
        if not path:
            return
        import csv
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Album", "Photos Added", "Photo Name", "Date Added"])
            for row in self._report_data:
                for photo in row["photos"]:
                    writer.writerow([row["album_name"], row["count"],
                                     photo["name"], photo["date"]])
        messagebox.showinfo("Exported", f"Report saved to:\n{path}")


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = App()
    app.mainloop()
