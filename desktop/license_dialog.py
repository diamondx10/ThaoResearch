"""Tkinter dialog for entering the license key at startup."""
from __future__ import annotations

from desktop import theme
import tkinter as tk
from tkinter import ttk
from typing import Optional


class LicenseDialog:
    """Modal-style dialog prompting for a license key.

    Usage:
        dlg = LicenseDialog()
        key = dlg.show()  # blocks until user submits/cancels
    """

    def __init__(self, parent: Optional[tk.Misc] = None):
        self.result: Optional[str] = None
        self.window = tk.Toplevel(parent) if parent else tk.Tk()
        self.window.title("ThaoResearch - License")
        self.window.geometry("420x230")
        self.window.resizable(False, False)
        self._build_ui()
        # Make modal
        self.window.transient(parent) if parent else None
        self.window.grab_set() if parent else None

    def _build_ui(self):
        # Title
        title = ttk.Label(
            self.window,
            text="Enter your license key",
            font=theme.font("title"),
        )
        title.pack(pady=(20, 5))

        subtitle = ttk.Label(
            self.window,
            text="A valid license is required to use ThaoResearch.",
            font=theme.font("body_sm"),
            foreground="#666",
        )
        subtitle.pack(pady=(0, 15))

        # Input
        input_frame = ttk.Frame(self.window)
        input_frame.pack(pady=5)
        ttk.Label(input_frame, text="License key:").pack(side="left", padx=(0, 10))
        self.entry = ttk.Entry(input_frame, width=20)
        self.entry.pack(side="left")
        self.entry.focus()

        # Status
        self.status_label = ttk.Label(self.window, text="", foreground=theme.COLORS["danger"])
        self.status_label.pack(pady=10)

        # Buttons
        button_frame = ttk.Frame(self.window)
        button_frame.pack(pady=10)
        ttk.Button(button_frame, text="Submit", command=self._on_submit, width=12).pack(
            side="left", padx=5
        )
        ttk.Button(button_frame, text="Cancel", command=self._on_cancel, width=12).pack(
            side="left", padx=5
        )

        # Bindings
        self.window.bind("<Return>", lambda _e: self._on_submit())
        self.window.bind("<Escape>", lambda _e: self._on_cancel())

    def _on_submit(self):
        license_key = self.entry.get().strip()
        if not license_key:
            self.status_label.config(text="Please enter a license key.")
            return
        self.result = license_key
        self.window.destroy()

    def _on_cancel(self):
        self.result = None
        self.window.destroy()

    def show(self) -> Optional[str]:
        """Block until the user submits or cancels. Return the entered key (str) or None."""
        try:
            self.window.mainloop()
        finally:
            try:
                self.window.destroy()
            except tk.TclError:
                pass
        return self.result
