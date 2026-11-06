"""Settings window — tabbed Tkinter notebook with General, Sources, Commodities, Filters, About.

The Telegram tab was removed intentionally: TELEGRAM_BOT_TOKEN and
TELEGRAM_CHAT_ID are loaded from a private .env file at runtime and are
considered secrets. They are not exposed in the GUI.

Can be used as a standalone Toplevel (via class + show()) or as an
embeddable widget via `build_widget(parent)` that returns a frame
hosting the same controls.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

from core.telegram_bot import TelegramNotifier, validate_chat_id, validate_token  # noqa: F401  (kept for back-compat imports; tab removed)
from desktop.window_icon import apply_app_icon
from utils import autostart, i18n, license as license_mod
from desktop import theme
from utils.i18n import t
from utils import paths
from utils.paths import APP_ROOT

logger = logging.getLogger("thaoresearch.desktop.settings_window")


def _browse_path(path: Path):
    """Open a folder in the OS file explorer."""
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as e:
        logger.warning("Failed to open folder %s: %s", path, e)


class _WidgetState:
    """Holds all the StringVar/BooleanVar/Text widgets the settings form needs.

    Lets the caller (e.g. MainWindow) read live values without owning Tk vars.
    """

    # ------------------------------------------------------------------
    # Autosave plumbing
    # ------------------------------------------------------------------
    # ``autosave_enabled`` — user-controllable toggle (default True). When
    # False the debounce timers are detached and the user must click
    # "Save" (or close the window) to persist anything.
    #
    # ``autosave_after_id`` — id returned by ``root.after(...)`` for the
    # pending autosave; we cancel it on every fresh change so the file is
    # only written 500ms after the user stops typing/toggling.
    #
    # ``on_save`` — host callback (re-applies config to the scheduler,
    # notifier, etc.). Called after every successful autosave so the live
    # app picks up the new settings without requiring a restart.
    autosave_enabled: bool = True
    autosave_after_id: Optional[str] = None
    autosave_debounce_ms: int = 500


def _autosave_now(state, frame, config, config_path, on_save):
    """Persist the current widget values to ``init_config.yaml`` (atomic
    write) and call ``on_save`` so the live app picks up the changes.

    Called from a debounced timer; safe to call multiple times in a row
    (each call cancels any pending timer first). Swallows exceptions and
    logs them — autosave is best-effort, never fatal, never raises into
    Tk's event loop.
    """
    try:
        # Cancel any pending debounce timer — we're flushing now.
        try:
            if state.autosave_after_id:
                frame.after_cancel(state.autosave_after_id)
        except Exception:
            pass
        state.autosave_after_id = None

        # Refuse to autosave while the License-key field is being edited —
        # half-typed keys look like real ones and would clobber ``.env``
        # on next launch. ``state.license_key_dirty`` is set by the
        # trace callback below.
        if getattr(state, "license_key_dirty", False):
            logger.debug("Autosave skipped: license key is being edited")
            return

        # Gather + persist + reload-on-disk atomically. The trace wiring in
        # ``build_widget`` is deferred until all tabs are built, so by
        # the time an autosave fires we know every cross-tab reference
        # is a real widget (not None).
        sources_tree = state.sources_tree
        high_text = state.high_text
        black_text = state.black_text
        delta_enabled_var = state.delta_enabled_var
        delta_var = state.delta_var

        try:
            new_cfg = _gather_config(
                config, state, sources_tree, high_text, black_text,
                delta_enabled_var, delta_var,
            )
        except Exception as e:
            logger.warning("Autosave: gather failed: %s", e)
            return

        # Write to ``init_config.yaml`` next to the live ``config.yaml``.
        # If the file isn't writable (read-only media, perms), fall back
        # to the same path the manual Save button uses.
        target = _resolve_init_config_path(config_path)
        try:
            ok = _persist_config_to(new_cfg, target)
        except Exception as e:
            logger.warning("Autosave: persist failed: %s", e)
            return

        if ok:
            logger.info("Autosaved settings to %s", target)
            if on_save:
                try:
                    on_save(new_cfg)
                except Exception as e:
                    logger.warning("Autosave on_save callback failed: %s", e)
    except Exception as e:
        # Belt-and-suspenders — autosave must never crash Tk.
        logger.debug("Autosave swallowed: %s", e)


def _schedule_autosave(state, frame, config, config_path, on_save):
    """Debounced autosave trigger. Wire to every widget's StringVar/BooleanVar
    ``trace_add("write", ...)`` and to the treeview's edit handlers.

    Multiple calls in quick succession collapse into a single save — the
    user sees the file written 500ms after they stop interacting.
    """
    if not getattr(state, "autosave_enabled", True):
        return
    try:
        if state.autosave_after_id:
            frame.after_cancel(state.autosave_after_id)
    except Exception:
        pass
    state.autosave_after_id = frame.after(
        state.autosave_debounce_ms,
        lambda: _autosave_now(state, frame, config, config_path, on_save),
    )


def _resolve_init_config_path(config_path: Path) -> Path:
    """Return the path we autosave to. We co-locate with ``config.yaml``
    in the data/ folder alongside history.db and prices.db.
    ``init_config.yaml`` is the canonical name; we fall back to
    ``config.yaml`` if for some reason a sibling write is impossible.
    """
    try:
        sibling = config_path.parent / "init_config.yaml"
        return sibling
    except Exception:
        return config_path


class _EntryDialog:
    """Small modal dialog for adding/editing a single Sources/Commodities row."""

    def __init__(self, parent, title, fields, initial=None):
        """fields = list of (key, label, kind, options?) tuples.

        kind: 'str' | 'bool' | 'comma_list' | 'optional_int'
        initial: optional dict of pre-filled values (for edit).
        """
        self.result: Optional[dict] = None
        initial = initial or {}

        self.win = tk.Toplevel(parent)
        self.win.title(title)
        self.win.transient(parent)
        self.win.grab_set()
        self.win.resizable(False, False)

        self._vars: dict = {}
        body = ttk.Frame(self.win, padding=12)
        body.pack(fill="both", expand=True)

        for row, (key, label, kind, *opts) in enumerate(fields):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=4, padx=(0, 8))
            init_val = initial.get(key, "")
            if kind == "bool":
                var = tk.BooleanVar(value=bool(init_val))
                ttk.Checkbutton(body, variable=var).grid(row=row, column=1, sticky="w", pady=4)
            elif kind == "optional_int":
                var = tk.StringVar(value="" if init_val in (None, "") else str(init_val))
                ttk.Entry(body, textvariable=var, width=30).grid(
                    row=row, column=1, sticky="we", pady=4
                )
            elif kind == "choice":
                values = opts[0] if opts else []
                var = tk.StringVar(value=str(init_val) if init_val else (values[0] if values else ""))
                ttk.Combobox(body, textvariable=var, values=values, state="readonly", width=28).grid(
                    row=row, column=1, sticky="we", pady=4
                )
            elif kind == "comma_list":
                if isinstance(init_val, list):
                    init_val = ", ".join(str(x) for x in init_val)
                var = tk.StringVar(value=str(init_val))
                ttk.Entry(body, textvariable=var, width=40).grid(
                    row=row, column=1, sticky="we", pady=4
                )
            else:  # str
                var = tk.StringVar(value=str(init_val))
                ttk.Entry(body, textvariable=var, width=40).grid(
                    row=row, column=1, sticky="we", pady=4
                )
            self._vars[key] = (var, kind)

        body.columnconfigure(1, weight=1)

        btns = ttk.Frame(self.win, padding=(12, 0, 12, 12))
        btns.pack(fill="x")
        ttk.Button(btns, text=t("common.cancel"), command=self.win.destroy, width=10).pack(
            side="right", padx=4
        )
        ttk.Button(btns, text=t("common.ok"), command=self._on_ok, width=10).pack(
            side="right", padx=4
        )

        # Center on parent
        self.win.update_idletasks()
        try:
            px = parent.winfo_rootx()
            py = parent.winfo_rooty()
            pw = parent.winfo_width()
            ph = parent.winfo_height()
            w = self.win.winfo_width()
            h = self.win.winfo_height()
            self.win.geometry(f"+{px + (pw - w) // 2}+{py + (ph - h) // 2}")
        except Exception:
            pass

        parent.wait_window(self.win)

    def _on_ok(self):
        out: dict = {}
        for key, (var, kind) in self._vars.items():
            if kind == "bool":
                out[key] = bool(var.get())
            elif kind == "optional_int":
                raw = var.get().strip()
                if raw == "":
                    out[key] = None
                else:
                    try:
                        out[key] = int(raw)
                    except ValueError:
                        messagebox.showerror(
                            "Invalid value",
                            f"'{var.get()}' is not a valid integer.",
                            parent=self.win,
                        )
                        return
            elif kind == "comma_list":
                out[key] = [s.strip() for s in var.get().split(",") if s.strip()]
            else:
                out[key] = var.get().strip()
        self.result = out
        self.win.destroy()


def build_widget(
    parent: tk.Misc,
    config: dict,
    config_path: Path,
    on_save: Optional[Callable[[dict], None]] = None,
    on_change_license: Optional[Callable[[], None]] = None,
    show_outer_buttons: bool = True,
) -> ttk.Frame:
    """Build the settings form inside `parent`. Returns the frame.

    If `show_outer_buttons` is False, no Save/Cancel buttons are packed at the
    bottom — useful when embedding inside a Notebook where the host provides
    a single Save action. Call `frame.save_and_close()` afterwards.
    """
    frame = ttk.Frame(parent)

    notebook = ttk.Notebook(frame)
    notebook.pack(fill="both", expand=True, padx=12, pady=(12, 6))

    state = _WidgetState()

    # Pre-create placeholder attributes that ``_build_general_tab`` /
    # autosave will read. The actual Text/Tree widgets are created by
    # the tab builders below; setting ``None`` here means an autosave
    # triggered by a General-tab edit (before the user has opened
    # Sources/Filters) sees ``None`` rather than crashing with
    # AttributeError. The tab builders overwrite these with real widgets.
    state.sources_tree = None
    state.commodities_tree = None
    state.high_text = None
    state.black_text = None
    state.delta_enabled_var = None
    state.delta_var = None

    # -------------------------------------------------------------- General tab
    # Pass ``on_save=None`` here — the actual autosave trace is wired
    # by build_widget after every tab is built (see comment block there).
    _build_general_tab(notebook, config, state, config_path)

    # -------------------------------------------------------------- Sources tab
    sources_tree = _build_sources_tab(notebook, config)
    state.sources_tree = sources_tree

    # ---------------------------------------------------------- Commodities tab
    commodities_tree = _build_commodities_tab(notebook, config)
    state.commodities_tree = commodities_tree

    # ------------------------------------------------------------- Filters tab
    high_text, black_text, delta_enabled_var, delta_var = _build_filters_tab(notebook, config)
    # Stash on ``state`` so the autosave loop (called by traces wired
    # below) can find them. The placeholders we set earlier are now
    # overwritten with real widgets.
    state.high_text = high_text
    state.black_text = black_text
    state.delta_enabled_var = delta_enabled_var
    state.delta_var = delta_var

    # Telegram settings (token + chat_id) are loaded from .env at runtime.
    # They are secrets and intentionally NOT exposed in the Settings GUI.

    # ---------------------------------------------------------------- About tab
    _build_about_tab(notebook, config_path=config_path, on_change_license=on_change_license)

    # ------------------------------------------------------------------
    # Wire all autosave traces NOW (after every tab is built). The
    # builders above attach the StringVar/BooleanVar to ``state`` but
    # deliberately defer the ``trace_add("write", ...)`` until this
    # point — the cross-tab references in ``_gather_config`` are only
    # valid once every tab has been instantiated.
    # ------------------------------------------------------------------
    def _wire_trace(var):
        try:
            var.trace_add(
                "write",
                lambda *_a, _v=var: _schedule_autosave(
                    state, frame, config, config_path, on_save
                ),
            )
        except Exception:
            pass

    # General tab vars
    for _v in (
        getattr(state, "max_articles_var", None),
        getattr(state, "tz_var", None),
        getattr(state, "lang_var", None),
        getattr(state, "theme_var", None),
        getattr(state, "toast_var", None),
        getattr(state, "tg_var", None),
        getattr(state, "force_send_var", None),
        getattr(state, "autosave_var", None),
        getattr(state, "scheduler_mode_var", None),
        getattr(state, "fixed_interval_var", None),
        getattr(state, "adaptive_business_hours_var", None),
        getattr(state, "adaptive_evening_var", None),
        getattr(state, "adaptive_weekend_var", None),
        getattr(state, "adaptive_off_weekend_var", None),
        getattr(state, "adaptive_start_hour_var", None),
        getattr(state, "adaptive_end_hour_var", None),
    ):
        if _v is not None:
            _wire_trace(_v)

    # Filters tab vars
    if delta_enabled_var is not None:
        _wire_trace(delta_enabled_var)
    if delta_var is not None:
        _wire_trace(delta_var)

    # Re-bind Text-widget <<Modified>> handlers. The first-pass wiring
    # in _build_filters_tab passed frame=None so the bind was skipped;
    # do it again now that we have a frame.
    if high_text is not None:
        high_text.edit_modified(False)
        def _on_high_modified(_evt=None, _w=high_text):
            if not _w.edit_modified():
                return
            _w.edit_modified(False)
            _schedule_autosave(state, frame, config, config_path, on_save)
        high_text.bind("<<Modified>>", _on_high_modified, add="+")
    if black_text is not None:
        black_text.edit_modified(False)
        def _on_black_modified(_evt=None, _w=black_text):
            if not _w.edit_modified():
                return
            _w.edit_modified(False)
            _schedule_autosave(state, frame, config, config_path, on_save)
        black_text.bind("<<Modified>>", _on_black_modified, add="+")

    # License-key field trace (don't autosave while user is typing it).
    if getattr(state, "license_key_var", None) is not None:
        def _on_license_key_change(*_a):
            state.license_key_dirty = True
            try:
                if getattr(state, "license_key_dirty_after_id", None):
                    frame.after_cancel(state.license_key_dirty_after_id)
            except Exception:
                pass
            state.license_key_dirty_after_id = frame.after(
                1500, lambda: setattr(state, "license_key_dirty", False)
            )
        state.license_key_var.trace_add("write", _on_license_key_change)

    # Save / Cancel buttons
    if show_outer_buttons:
        bottom = ttk.Frame(frame, padding=(12, 6, 12, 12))
        bottom.pack(fill="x")

        def _on_cancel():
            # Flush any pending autosave first — the user may have edited
            # fields 100ms ago and the debounce hasn't fired yet.
            _autosave_now(state, frame, config, config_path, on_save)
            try:
                frame.winfo_toplevel().destroy()
            except Exception:
                pass

        ttk.Button(
            bottom, text=t("settings.btn_cancel"), command=_on_cancel, width=12
        ).pack(side="right", padx=4)

        def _do_save():
            # Cancel any pending autosave — we're saving NOW, no need to
            # write again 500ms later.
            try:
                if state.autosave_after_id:
                    frame.after_cancel(state.autosave_after_id)
                    state.autosave_after_id = None
            except Exception:
                pass
            new_cfg = _gather_config(
                config,
                state,
                sources_tree,
                high_text,
                black_text,
                delta_enabled_var,
                delta_var,
            )
            ok = _persist_config(new_cfg, config_path, state, frame.winfo_toplevel())
            if ok and on_save:
                try:
                    on_save(new_cfg)
                except Exception as e:
                    logger.warning("on_save callback failed: %s", e)

        ttk.Button(
            bottom, text=t("settings.btn_save"), command=_do_save, width=12
        ).pack(side="right", padx=4)

    # Expose references for _gather_config()
    state.sources_tree = sources_tree
    state.commodities_tree = commodities_tree

    # Expose save action for the host
    def save_and_close():
        new_cfg = _gather_config(
            config,
            state,
            sources_tree,
            high_text,
            black_text,
            delta_enabled_var,
            delta_var,
        )
        ok = _persist_config(new_cfg, config_path, state, frame.winfo_toplevel())
        if ok and on_save:
            try:
                on_save(new_cfg)
            except Exception as e:
                logger.warning("on_save callback failed: %s", e)
        return ok

    frame.save_and_close = save_and_close  # type: ignore[attr-defined]
    frame.reload = lambda: build_widget(parent, config, config_path, on_save=on_save, on_change_license=on_change_license, show_outer_buttons=show_outer_buttons)  # noqa: E501
    return frame


def _apply_license_state(state, tab):
    """Update the license status label + enable/disable the gated checkboxes.

    The two checkboxes (``enable_desktop_toast`` and
    ``enable_telegram_alert``) are only editable when a valid (and
    unexpired) license is installed.
    """
    status = license_mod.get_license_status()
    info = license_mod.get_license_info()
    if status == license_mod.STATUS_ACTIVATED and info:
        msg_key = "settings.license_status_activated"
        msg = t(msg_key, default="License activated")
        if info.get("valid_to"):
            msg = f"{msg} — {info['valid_to']}"
        color = theme.COLORS["success"]  # green
        gated_enabled = True
    elif status == license_mod.STATUS_EXPIRED:
        msg = t("settings.license_status_expired", default="License expired")
        if info and info.get("valid_to"):
            msg = f"{msg} ({info['valid_to']})"
        color = theme.COLORS["danger"]  # red
        gated_enabled = False
    else:
        msg = t("settings.license_status_not_activated", default="License not activated")
        color = theme.COLORS["danger"]
        gated_enabled = False

    try:
        state.license_status_var.set(msg)
        # ``configure`` may fail if the underlying widget was destroyed
        # (e.g. the parent notebook tab was closed mid-handler). Don't
        # let that propagate — the message has already been written to
        # ``license_status_var`` so the value is preserved; we just
        # skip the colour update.
        try:
            state.license_status_label.configure(foreground=color)
        except (tk.TclError, AttributeError):
            pass
    except tk.TclError:
        return

    state.gated_license_enabled = gated_enabled
    try:
        # ttk.Checkbutton uses ``state`` for the disabled flag. We want
        # the checkbox visual to clearly show it's locked.
        if gated_enabled:
            try:
                state.toast_check.configure(state="normal")
                state.tg_check.configure(state="normal")
            except (tk.TclError, AttributeError):
                pass
        else:
            try:
                state.toast_check.configure(state="disabled")
                state.tg_check.configure(state="disabled")
            except (tk.TclError, AttributeError):
                pass
            # Force both flags off when license missing — even if the
            # YAML still has ``true``, the runtime gating in main.py /
            # notifier already enforces this. Setting them to False keeps
            # the persisted config honest.
            try:
                state.toast_var.set(False)
                state.tg_var.set(False)
            except (tk.TclError, AttributeError):
                pass
    except tk.TclError:
        pass


def _on_submit_license(state, tab, on_save=None, config=None):
    """Submit handler for the license key entry in the General tab.

    Spec: any failure (empty, wrong length, invalid AES decode,
    expired key) collapses to a single error message —
    "License is not activated or invalid". On success the license is
    saved and the gated checkboxes unblock.

    Behaviour:
      - Success → fire ``on_save`` so the host can flip the notifier
        (Telegram + toast) on without waiting for the user to click
        Save in the Settings tab.
      - Failure (wrong key) → invalidate the previously-saved valid
        license (delete ``data/license.dat``). The user's mistake has
        a consequence: they must re-enter the correct key. This
        matches the spec: "if you enter wrong, user pays the price
        of losing validity until the correct key is entered."
    """
    from tkinter import messagebox

    key = state.license_key_var.get().strip()
    # One canonical failure message for input problems + AES / date
    # problems. We deliberately don't tell the user WHY the key failed
    # (wrong length vs. wrong ciphertext vs. expired) — same UX as
    # most commercial license dialogs.
    invalid_msg = t(
        "settings.license_submit_invalid",
        default="License is not activated or invalid",
    )

    if not key:
        messagebox.showerror(
            t("license.title", default="ThaoResearch - License"),
            invalid_msg,
        )
        return

    had_existing = license_mod.get_license_info() is not None
    success = False
    # Diagnostic — print the bytes of the entered key to logs so we can
    # tell at a glance if a hidden whitespace/encoding problem is
    # causing false "invalid" results from the dialog.
    logger.info(
        "License submit: key repr=%r len=%d had_existing=%s",
        key, len(key), had_existing,
    )
    try:
        # Always validate + write the freshly-entered key, regardless of
        # whether a license file already exists. ``save_license`` calls
        # the AES check itself and either returns the new metadata dict
        # or raises ``LicenseInvalidError`` / ``LicenseExpiredError``.
        # Using ``save_license`` for both first-time and overwrite
        # paths is the simplest correct behaviour: the latest submitted
        # key always wins.
        result = license_mod.save_license(key)
        logger.info(
            "License saved OK: valid_to=%s", result.get("valid_to"),
        )
        messagebox.showinfo(
            t("license.title", default="ThaoResearch - License"),
            t("settings.license_status_activated", default="License activated"),
        )
        success = True
    except (license_mod.LicenseInvalidError, license_mod.LicenseExpiredError, ValueError, Exception) as e:
        # Any validation failure — wrong length, bad AES, expired,
        # garbage payload — surfaces the same generic message. But we
        # DO log the underlying exception so the cause is traceable in
        # ``dist/logs/app.log`` instead of vanishing behind a generic
        # dialog box.
        logger.warning(
            "License submit failed: %s: %s", type(e).__name__, e,
        )
        messagebox.showerror(
            t("license.title", default="ThaoResearch - License"),
            invalid_msg,
        )

    # On failure with an existing valid license, remove the old file so
    # the previous valid key is no longer honored. The user must enter
    # the correct key to re-activate.
    if not success and had_existing:
        try:
            license_mod.clear_license()
            logger.warning(
                "Wrong license key entered — removed previously valid license."
            )
        except Exception as e:
            logger.warning("Failed to clear license after bad submission: %s", e)

    # Refresh UI state regardless of success/failure.
    _apply_license_state(state, tab)

    # On success, propagate the change to the running notifier
    # (rebuild Telegram if it was disabled at startup, flip the toast
    # flag, persist the YAML). Without this, the user would have to
    # leave Settings and reopen them — or restart the app — for the
    # two gated checkboxes to actually do anything.
    if success and on_save and config is not None:
        # Make sure the gated flags reflect the freshly-activated
        # license. If the user already had them ticked, leave them;
        # if the startup license-gate forced them off, flip them on.
        general = config.setdefault("general", {})
        general.setdefault("enable_desktop_toast", True)
        general.setdefault("enable_telegram_alert", True)
        try:
            on_save(dict(config))
        except Exception as e:
            logger.warning("on_save callback after license activation failed: %s", e)


def _show_license_info(parent):
    """Show the standard License Info dialog (key + valid_from + valid_to)."""
    from tkinter import messagebox

    info = license_mod.get_license_info()
    if not info:
        messagebox.showinfo(
            t("license.title", default="ThaoResearch - License"),
            t("settings.license_status_not_activated", default="License not activated"),
        )
        return

    status = license_mod.get_license_status()
    if status == license_mod.STATUS_EXPIRED:
        heading = t("settings.license_status_expired", default="License expired")
    elif status == license_mod.STATUS_INVALID:
        heading = t("settings.license_status_invalid", default="License not activated")
    else:
        heading = t("settings.license_status_activated", default="License activated")

    key = info.get("license_key", "?")
    valid_to = info.get("valid_to", "?")
    issued_at = info.get("issued_at", "?")
    # Keep only YYYY-MM-DD from issued_at — the spec asks for "Valid from"
    # in date format only.
    valid_from = (issued_at[:10] if isinstance(issued_at, str) and len(issued_at) >= 10 else "?")

    text = (
        f"{heading}\n\n"
        f"{t('settings.license_label_key', default='License key:')} {key}\n"
        f"{t('settings.license_label_valid_from', default='Valid from:')} {valid_from}\n"
        f"{t('settings.license_label_valid_to', default='Valid to:')} {valid_to}"
    )
    messagebox.showinfo(t("license.title", default="ThaoResearch - License"), text)


def _scrollable_page(notebook, title: str) -> ttk.Frame:
    """Add a vertically scrollable page to ``notebook`` and return its body.

    Tk notebook pages do not scroll: a page taller than the pane is simply
    clipped, with nothing on screen to say so. The General tab needs about
    955px and the pane offers roughly 650px in the default 980x700 window,
    so the bottom ~305px -- the whole Scheduler block and the Storage
    data-folder picker -- was unreachable unless the user maximized.

    Only this tab needs it. Sources and Commodities hold a Treeview packed
    with ``expand=True``; those stretch to the pane and bring their own
    scrollbars, and nesting them in a canvas would give them an unbounded
    height instead. Filters and About measure well under the limit.

    Returns the inner frame to grid into -- callers must not touch the
    canvas, which owns the scrolling.
    """
    outer = ttk.Frame(notebook)
    notebook.add(outer, text=title)

    canvas = tk.Canvas(
        outer,
        highlightthickness=0,
        borderwidth=0,
        background=theme.COLORS["bg"],
    )
    vbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=vbar.set)
    canvas.pack(side="left", fill="both", expand=True)

    body = ttk.Frame(canvas, padding=12)
    window_id = canvas.create_window((0, 0), window=body, anchor="nw")

    def _sync(_event=None):
        """Keep the scrollregion honest and hide the bar when it is idle."""
        canvas.configure(scrollregion=canvas.bbox("all"))
        needed = body.winfo_reqheight() > canvas.winfo_height()
        if needed and not vbar.winfo_ismapped():
            vbar.pack(side="right", fill="y")
        elif not needed and vbar.winfo_ismapped():
            vbar.pack_forget()
            canvas.yview_moveto(0.0)

    def _fit_width(event):
        # Without this the inner frame keeps its requested width and the
        # right-hand column ("restart to apply") sits outside the viewport.
        canvas.itemconfigure(window_id, width=event.width)
        _sync()

    body.bind("<Configure>", _sync)
    canvas.bind("<Configure>", _fit_width)

    def _on_wheel(event):
        if body.winfo_reqheight() <= canvas.winfo_height():
            return  # nothing to scroll; let the event through
        canvas.yview_scroll(-1 * (event.delta // 120), "units")

    # Bind while the pointer is over this page only. ``bind_all`` left
    # permanently in place would steal the wheel from every other widget
    # in the app, including the Treeviews on the sibling tabs.
    canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _on_wheel))
    canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

    return body


def _build_general_tab(notebook, config, state, config_path, on_save=None):
    tab = _scrollable_page(notebook, t("settings.tab_general"))

    general = config.get("general", {})
    scheduler = config.get("scheduler", {})
    adaptive = scheduler.get("adaptive") or {}

    # Note: autosave traces are wired by the caller (``build_widget``)
    # AFTER every tab is built — see the comment block there. We just
    # stash the StringVar/BooleanVar/Text widgets on ``state`` here.

    # NOTE: The old "Check interval (minutes)" row from `general.check_interval_minutes`
    # was removed intentionally. It was only ever a fallback value used by the fixed
    # scheduler mode and was confusingly placed in the General tab. The single source
    # of truth for the scheduler interval is now the "Scheduler" block below.

    ttk.Label(tab, text=t("settings.label_max_articles")).grid(row=0, column=0, sticky="w", pady=4)
    state.max_articles_var = tk.StringVar(value=str(general.get("max_articles_per_run", 10)))
    ttk.Spinbox(tab, from_=1, to=100, textvariable=state.max_articles_var, width=10).grid(
        row=0, column=1, sticky="w", pady=4
    )

    ttk.Label(tab, text=t("settings.label_timezone")).grid(row=1, column=0, sticky="w", pady=4)
    state.tz_var = tk.StringVar(value=general.get("timezone", "Asia/Ho_Chi_Minh"))
    ttk.Combobox(
        tab,
        textvariable=state.tz_var,
        values=[
            "Asia/Ho_Chi_Minh",
            "Asia/Seoul",
            "Asia/Tokyo",
            "America/New_York",
            "America/Chicago",
            "Europe/London",
            "UTC",
        ],
        width=25,
    ).grid(row=1, column=1, sticky="w", pady=4)

    ttk.Label(tab, text=t("settings.label_language")).grid(row=2, column=0, sticky="w", pady=4)
    state.lang_var = tk.StringVar(value=general.get("language", "en"))
    ttk.Combobox(
        tab,
        textvariable=state.lang_var,
        values=["en", "vi", "ko"],
        state="readonly",
        width=10,
    ).grid(row=2, column=1, sticky="w", pady=4)

    ttk.Label(tab, text=t("settings.label_appearance", default="Appearance:")).grid(
        row=3, column=0, sticky="w", pady=4
    )
    state.theme_var = tk.StringVar(value=general.get("theme", "system"))
    ttk.Combobox(
        tab,
        textvariable=state.theme_var,
        values=["system", "light", "dark"],
        state="readonly",
        width=10,
    ).grid(row=3, column=1, sticky="w", pady=4)
    ttk.Label(
        tab,
        text=t("settings.hint_restart_to_apply", default="restart to apply"),
        style="Caption.TLabel",
    ).grid(row=3, column=2, sticky="w", padx=(8, 0))

    state.toast_var = tk.BooleanVar(value=general.get("enable_desktop_toast", True))
    state.toast_check = ttk.Checkbutton(
        tab,
        text=t("settings.label_enable_toast"),
        variable=state.toast_var,
    )
    state.toast_check.grid(row=4, column=0, columnspan=2, sticky="w", pady=4)

    state.tg_var = tk.BooleanVar(value=general.get("enable_telegram_alert", True))
    state.tg_check = ttk.Checkbutton(
        tab,
        text=t("settings.label_enable_telegram"),
        variable=state.tg_var,
    )
    state.tg_check.grid(row=5, column=0, columnspan=2, sticky="w", pady=4)

    # "Force send (bypass duplicate check)" — when ticked, every article
    # that passes the filter is pushed to Telegram + desktop + toast, even
    # if it's already in the history table. Useful when the user wants
    # to re-share today's news with a Telegram group / channel. Default
    # is OFF (= honour the dedup table, the original behaviour).
    state.force_send_var = tk.BooleanVar(value=general.get("force_send_duplicates", False))
    state.force_send_check = ttk.Checkbutton(
        tab,
        text=t("settings.label_force_send_duplicates", default="Enable force send (bypass duplicate check)"),
        variable=state.force_send_var,
    )
    state.force_send_check.grid(row=6, column=0, columnspan=2, sticky="w", pady=4)

    # ------------------------------------------------------------ Init config block
    # Autosave: every other field on this page is wired through the
    # post-build trace step in ``build_widget``. The autosave toggle
    # itself is **not** wired through that path because toggling it ON
    # is exactly the action that controls whether autosave fires —
    # wiring the toggle to the same handler would create a no-op loop.
    # The trace below only updates ``state.autosave_enabled`` and
    # persists nothing by itself.
    state.autosave_var = tk.BooleanVar(value=general.get("autosave_settings", True))
    state.autosave_check = ttk.Checkbutton(
        tab,
        text=t("settings.label_autosave_settings", default="Autosave settings (init_config.yaml)"),
        variable=state.autosave_var,
    )
    state.autosave_check.grid(row=7, column=0, columnspan=2, sticky="w", pady=4)

    # Reflect the user's choice into the autosave gate. We don't write to
    # disk here directly — the toggle is wired separately (below) so the
    # change is picked up by the next normal field change and saved as
    # part of that flush.
    def _on_autosave_toggle(*_a):
        state.autosave_enabled = bool(state.autosave_var.get())
        logger.debug("Autosave toggled: %s", state.autosave_enabled)
    state.autosave_var.trace_add("write", _on_autosave_toggle)
    # Initialize gate from the loaded config
    state.autosave_enabled = bool(state.autosave_var.get())

    state.autostart_var = tk.BooleanVar(value=autostart.get_autostart_status()[0])
    # Autostart changes are applied via _persist_config (autostart module),
    # not via autosave, so we wire a manual-save-only trace here.
    ttk.Checkbutton(tab, text=t("settings.label_enable_autostart"), variable=state.autostart_var).grid(
        row=8, column=0, columnspan=2, sticky="w", pady=4
    )

    # ------------------------------------------------------------ License block
    # Rows here are consecutive on purpose. ``grid`` silently stacks two
    # widgets that claim the same cell, so a duplicated row number is not
    # an error -- it renders as overlapping text. This block used to put
    # its separator on row 12 (under the "view license" button) and its
    # heading on row 13 (under the scheduler separator, which struck the
    # word "License" through), while the autostart checkbox shared row 7
    # with the autosave one. Keep the numbering below gap-free and in
    # visual order so a stray reuse is obvious.
    #
    # NOTE: config_path is displayed in the About tab for clarity.
    ttk.Separator(tab, orient="horizontal").grid(row=9, column=0, columnspan=2, sticky="we", pady=12)
    ttk.Label(tab, text=t("settings.license_block_title"), font=theme.font("subheading")).grid(
        row=10, column=0, columnspan=2, sticky="w", pady=(0, 4)
    )

    # Status line — coloured. Lives on its own row so it can wrap if the
    # localized message is long.
    state.license_status_var = tk.StringVar(value="")
    state.license_status_label = ttk.Label(
        tab,
        textvariable=state.license_status_var,
        foreground=theme.COLORS["fg_muted"],
        wraplength=420,
        justify="left",
    )
    state.license_status_label.grid(row=11, column=0, columnspan=2, sticky="w", pady=(0, 6))

    ttk.Label(tab, text=t("settings.label_license_key")).grid(row=12, column=0, sticky="w", pady=4)
    license_input_frame = ttk.Frame(tab)
    license_input_frame.grid(row=12, column=1, sticky="w", pady=4)
    state.license_key_var = tk.StringVar()
    # Autosave trace is wired by build_widget AFTER every tab is
    # instantiated — we just stash the StringVar here.
    state.license_entry = ttk.Entry(license_input_frame, textvariable=state.license_key_var, width=36)
    state.license_entry.pack(side="left")
    # Submit button label changes depending on activation state.
    state.license_submit_btn = ttk.Button(
        license_input_frame,
        text=t("settings.btn_submit_license"),
        command=lambda: _on_submit_license(state, tab, on_save=on_save, config=config),
        width=14,
    )
    state.license_submit_btn.pack(side="left", padx=(8, 0))

    state.license_view_btn = ttk.Button(
        tab,
        text=t("settings.btn_view_license_info"),
        command=lambda: _show_license_info(tab),
        width=22,
    )
    state.license_view_btn.grid(row=13, column=1, sticky="w", pady=4)

    # Apply initial license state — gates the 2 checkboxes above and
    # updates the status label.
    _apply_license_state(state, tab)

    # ------------------------------------------------------------ Scheduler block
    ttk.Separator(tab, orient="horizontal").grid(row=14, column=0, columnspan=2, sticky="we", pady=12)
    ttk.Label(
        tab,
        text=t("settings.scheduler_block_title", default="Scheduler"),
        font=theme.font("subheading"),
    ).grid(row=15, column=0, columnspan=2, sticky="w", pady=(0, 4))

    ttk.Label(tab, text=t("settings.label_scheduler_mode", default="Mode:")).grid(
        row=16, column=0, sticky="w", pady=4
    )
    state.scheduler_mode_var = tk.StringVar(value=scheduler.get("mode", "fixed"))
    scheduler_mode_combo = ttk.Combobox(
        tab,
        textvariable=state.scheduler_mode_var,
        values=["adaptive", "fixed"],
        state="readonly",
        width=15,
    )
    scheduler_mode_combo.grid(row=16, column=1, sticky="w", pady=4)

    # -------------------- Fixed interval sub-block (only when mode = fixed) ----
    state.fixed_interval_label = ttk.Label(
        tab, text=t("settings.label_fixed_interval", default="Fixed interval (minutes):")
    )
    state.fixed_interval_label.grid(row=17, column=0, sticky="w", pady=4)
    state.fixed_interval_var = tk.StringVar(value=str(scheduler.get("fixed_interval_minutes", 1)))
    state.fixed_interval_spin = ttk.Spinbox(
        tab, from_=1, to=1440, textvariable=state.fixed_interval_var, width=10
    )
    state.fixed_interval_spin.grid(row=17, column=1, sticky="w", pady=4)

    # -------------------- Adaptive sub-block (only when mode = adaptive) -------
    ttk.Separator(tab, orient="horizontal").grid(row=18, column=0, columnspan=2, sticky="we", pady=(8, 4))

    state.adaptive_widgets = []  # rows 19.. we'll attach widgets to enable/disable together

    def _add_adaptive_row(row, label_text, var, kind="int", **kwargs):
        """
        kind: 'int' for Spinbox (1..1440), 'bool' for Checkbutton, 'hour' for Spinbox (0..23).
        Returns the (label, widget) tuple to be enabled/disabled together.
        """
        lbl = ttk.Label(tab, text=label_text)
        lbl.grid(row=row, column=0, sticky="w", pady=4)
        if kind == "bool":
            w = ttk.Checkbutton(tab, variable=var)
        elif kind == "hour":
            w = ttk.Spinbox(tab, from_=0, to=23, textvariable=var, width=10, **kwargs)
        else:
            w = ttk.Spinbox(tab, from_=1, to=1440, textvariable=var, width=10, **kwargs)
        w.grid(row=row, column=1, sticky="w", pady=4)
        state.adaptive_widgets.append((lbl, w))
        return lbl, w

    state.adaptive_business_hours_var = tk.IntVar(
        value=int(adaptive.get("weekday_business_hours", 3))
    )
    state.adaptive_evening_var = tk.IntVar(
        value=int(adaptive.get("weekday_evening", 10))
    )
    state.adaptive_weekend_var = tk.IntVar(
        value=int(adaptive.get("weekend", 30))
    )
    state.adaptive_off_weekend_var = tk.BooleanVar(
        value=bool(adaptive.get("off_on_weekend", False))
    )
    state.adaptive_start_hour_var = tk.IntVar(
        value=int(adaptive.get("business_hours_start", 8))
    )
    state.adaptive_end_hour_var = tk.IntVar(
        value=int(adaptive.get("business_hours_end", 20))
    )

    # (autosave traces are wired by build_widget after all tabs exist)

    _add_adaptive_row(
        19,
        t("settings.label_adaptive_business", default="Weekday business hours (min):"),
        state.adaptive_business_hours_var,
        "int",
    )
    _add_adaptive_row(
        20,
        t("settings.label_adaptive_evening", default="Weekday evening (min):"),
        state.adaptive_evening_var,
        "int",
    )
    _add_adaptive_row(
        21,
        t("settings.label_adaptive_weekend", default="Weekend (min):"),
        state.adaptive_weekend_var,
        "int",
    )
    _add_adaptive_row(
        22,
        t("settings.label_adaptive_off_weekend", default="Off on weekend:"),
        state.adaptive_off_weekend_var,
        "bool",
    )
    _add_adaptive_row(
        23,
        t("settings.label_adaptive_start_hour", default="Business hours start (0-23):"),
        state.adaptive_start_hour_var,
        "hour",
    )
    _add_adaptive_row(
        24,
        t("settings.label_adaptive_end_hour", default="Business hours end (0-23):"),
        state.adaptive_end_hour_var,
        "hour",
    )

    def _apply_scheduler_mode(*_args):
        """Enable/disable sub-blocks based on the selected scheduler mode."""
        mode = state.scheduler_mode_var.get()
        if mode == "fixed":
            # Disable adaptive block
            for lbl, w in state.adaptive_widgets:
                try:
                    lbl.configure(state="disabled")
                except tk.TclError:
                    pass
                try:
                    w.configure(state="disabled")
                except tk.TclError:
                    pass
            # Enable fixed control
            try:
                state.fixed_interval_label.configure(state="normal")
                state.fixed_interval_spin.configure(state="normal")
            except tk.TclError:
                pass
        else:
            # Enable adaptive block
            for lbl, w in state.adaptive_widgets:
                try:
                    lbl.configure(state="normal")
                except tk.TclError:
                    pass
                try:
                    w.configure(state="normal")
                except tk.TclError:
                    pass
            # Disable fixed control
            try:
                state.fixed_interval_label.configure(state="disabled")
                state.fixed_interval_spin.configure(state="disabled")
            except tk.TclError:
                pass

    # Re-run when the user changes scheduler mode
    state.scheduler_mode_var.trace_add("write", _apply_scheduler_mode)
    # Apply initial state
    _apply_scheduler_mode()

    _build_storage_block(tab, start_row=25)


# =============================================================================
# Storage block — lets the user pick where config / databases / logs live
# =============================================================================
def _build_storage_block(tab, start_row: int) -> None:
    """Data-folder picker.

    This is deliberately NOT part of ``config.yaml``: the data folder is
    where config.yaml itself lives, so it cannot be stored inside it. The
    choice goes to a pointer file via ``paths.save_data_dir_choice()`` and
    takes effect on the next launch.
    """
    ttk.Separator(tab, orient="horizontal").grid(
        row=start_row, column=0, columnspan=3, sticky="ew", pady=(16, 8)
    )
    ttk.Label(
        tab,
        text=t("settings.storage_block_title", default="Storage"),
        font=theme.font("subheading"),
    ).grid(
        row=start_row + 1, column=0, sticky="w", pady=(0, 6)
    )

    ttk.Label(tab, text=t("settings.label_data_folder", default="Data folder:")).grid(
        row=start_row + 2, column=0, sticky="w", pady=4
    )

    path_var = tk.StringVar(value=str(paths.DATA_DIR))
    entry = ttk.Entry(tab, textvariable=path_var, width=46, state="readonly")
    entry.grid(row=start_row + 2, column=1, sticky="w", pady=4)

    source_labels = {
        "cli": "set by --data-dir for this run",
        "env": "set by THAORESEARCH_DATA_DIR",
        "pointer:portable": "your choice (saved next to the program)",
        "pointer:user": "your choice (saved in LocalAppData)",
        "portable": "default: next to the program",
        "localappdata": "fallback: the program folder is not writable",
        "temp": "last resort: temporary folder",
        "unwritable": "no writable location found",
    }
    note = ttk.Label(
        tab,
        text=source_labels.get(paths.DATA_DIR_SOURCE, paths.DATA_DIR_SOURCE),
        foreground=theme.COLORS["danger"] if paths.DATA_DIR_FALLBACK else theme.COLORS["fg_muted"],
    )
    note.grid(row=start_row + 3, column=1, sticky="w")

    btns = ttk.Frame(tab)
    btns.grid(row=start_row + 4, column=1, sticky="w", pady=(8, 0))

    def _change_folder():
        chosen = filedialog.askdirectory(
            title="Choose a folder for ThaoResearch data",
            initialdir=str(paths.DATA_DIR if paths.DATA_DIR.exists() else Path.home()),
            mustexist=False,
        )
        if not chosen:
            return
        target = Path(chosen)

        if not paths.is_writable(target):
            messagebox.showerror(
                "Folder not writable",
                f"ThaoResearch cannot write to:\n{target}\n\n"
                "Pick a folder you own, such as one inside Documents.",
            )
            return

        # Offer to bring the existing data along, so history and the license
        # are not silently left behind in the old folder.
        moved: list[str] = []
        old = paths.DATA_DIR
        if old.exists() and old.resolve() != target.resolve():
            names = [
                n
                for n in ("config.yaml", "init_config.yaml", "history.db",
                          "prices.db", "license.dat")
                if (old / n).is_file()
            ]
            if names and messagebox.askyesno(
                "Copy existing data?",
                "Copy your current settings, history and license to the new "
                f"folder?\n\nFrom: {old}\nTo:   {target}\n\n"
                f"Files: {', '.join(names)}\n\n"
                "The originals are left untouched.",
            ):
                import shutil

                for n in names:
                    try:
                        shutil.copy2(old / n, target / n)
                        moved.append(n)
                    except Exception as e:
                        logger.warning("Could not copy %s: %s", n, e)

        ok, where = paths.save_data_dir_choice(target)
        if not ok:
            messagebox.showerror("Could not save the choice", where)
            return

        path_var.set(str(target))
        note.configure(text="your choice — restart to apply", foreground=theme.COLORS["success"])
        tail = f"\n\nCopied: {', '.join(moved)}" if moved else ""
        messagebox.showinfo(
            "Data folder changed",
            f"ThaoResearch will use:\n{target}\n\n"
            f"Saved in: {where}{tail}\n\n"
            "Restart the app for this to take effect.",
        )

    def _reset_folder():
        removed = paths.clear_data_dir_choice()
        path_var.set(str(paths.default_portable_dir()))
        note.configure(text="default — restart to apply", foreground=theme.COLORS["success"])
        messagebox.showinfo(
            "Reset to default",
            "ThaoResearch will go back to its default data folder on the next "
            "launch.\n\n"
            + (f"Removed: {', '.join(str(p) for p in removed)}" if removed
               else "No saved choice was set.")
            + "\n\nYour files are not deleted.",
        )

    ttk.Button(btns, text="Change…", command=_change_folder).pack(side="left")
    ttk.Button(btns, text="Open", command=lambda: _browse_path(paths.DATA_DIR)).pack(
        side="left", padx=(6, 0)
    )
    ttk.Button(btns, text="Reset to default", command=_reset_folder).pack(
        side="left", padx=(6, 0)
    )

    if paths.DATA_DIR_FALLBACK:
        ttk.Label(
            tab,
            text=(
                "Your chosen folder could not be written to, so a fallback is "
                "in use. Pick another folder."
            ),
            foreground=theme.COLORS["danger"],
            wraplength=430,
        ).grid(row=start_row + 5, column=1, sticky="w", pady=(8, 0))


def _build_sources_tab(notebook, config):
    tab = ttk.Frame(notebook, padding=12)
    notebook.add(tab, text=t("settings.tab_sources"))

    cols = ("enabled", "name", "type", "url")
    tree = ttk.Treeview(tab, columns=cols, show="headings", height=10)
    for col, label, width in (
        ("enabled", "On", 50),
        ("name", "Name", 160),
        ("type", "Type", 60),
        ("url", "URL", 380),
    ):
        tree.heading(col, text=label)
        tree.column(col, width=width, anchor="w")

    for s in config.get("sources", []):
        tree.insert(
            "",
            "end",
            values=(
                "[x]" if s.get("enabled", True) else "[ ]",
                s.get("name", ""),
                s.get("type", ""),
                s.get("url", ""),
            ),
        )

    tree.pack(fill="both", expand=True, side="left")

    side = ttk.Frame(tab)
    side.pack(side="left", fill="y", padx=(8, 0))

    def _toggle():
        sel = tree.selection()
        if not sel:
            return
        for iid in sel:
            vals = list(tree.item(iid, "values"))
            vals[0] = "[ ]" if vals[0] == "[x]" else "[x]"
            tree.item(iid, values=vals)

    def _add():
        existing_names = {tree.item(iid, "values")[1] for iid in tree.get_children()}
        dlg = _EntryDialog(
            tab.winfo_toplevel(),
            t("settings.sources.add_title"),
            fields=[
                ("name", t("settings.sources.field_name"), "str"),
                ("type", t("settings.sources.field_type"), "choice", ["rss", "html"]),
                ("url", t("settings.sources.field_url"), "str"),
                ("enabled", t("settings.sources.field_enabled"), "bool"),
            ],
        )
        if not dlg.result:
            return
        new = dlg.result
        if not new.get("name"):
            messagebox.showerror(
                t("settings.sources.add_title"),
                t("settings.sources.error_name_required"),
                parent=tab.winfo_toplevel(),
            )
            return
        if new["name"] in existing_names:
            messagebox.showerror(
                t("settings.sources.add_title"),
                t("settings.sources.error_name_exists", name=new["name"]),
                parent=tab.winfo_toplevel(),
            )
            return
        if not new.get("url"):
            messagebox.showerror(
                t("settings.sources.add_title"),
                t("settings.sources.error_url_required"),
                parent=tab.winfo_toplevel(),
            )
            return
        tree.insert(
            "",
            "end",
            values=(
                "[x]" if new.get("enabled", True) else "[ ]",
                new.get("name", ""),
                new.get("type", "rss"),
                new.get("url", ""),
            ),
        )

    def _edit():
        sel = tree.selection()
        if not sel:
            messagebox.showinfo(
                t("settings.sources.edit_title"),
                t("settings.sources.error_select_row"),
                parent=tab.winfo_toplevel(),
            )
            return
        iid = sel[0]
        vals = list(tree.item(iid, "values"))
        dlg = _EntryDialog(
            tab.winfo_toplevel(),
            t("settings.sources.edit_title"),
            fields=[
                ("name", t("settings.sources.field_name"), "str"),
                ("type", t("settings.sources.field_type"), "choice", ["rss", "html"]),
                ("url", t("settings.sources.field_url"), "str"),
                ("enabled", t("settings.sources.field_enabled"), "bool"),
            ],
            initial={
                "name": vals[1],
                "type": vals[2] or "rss",
                "url": vals[3],
                "enabled": vals[0] == "[x]",
            },
        )
        if not dlg.result:
            return
        new = dlg.result
        if not new.get("name"):
            messagebox.showerror(
                t("settings.sources.edit_title"),
                t("settings.sources.error_name_required"),
                parent=tab.winfo_toplevel(),
            )
            return
        if not new.get("url"):
            messagebox.showerror(
                t("settings.sources.edit_title"),
                t("settings.sources.error_url_required"),
                parent=tab.winfo_toplevel(),
            )
            return
        tree.item(
            iid,
            values=(
                "[x]" if new.get("enabled", True) else "[ ]",
                new.get("name", ""),
                new.get("type", "rss"),
                new.get("url", ""),
            ),
        )

    def _delete():
        sel = tree.selection()
        if not sel:
            messagebox.showinfo(
                t("settings.btn_delete"),
                t("settings.sources.error_select_row"),
                parent=tab.winfo_toplevel(),
            )
            return
        names = [tree.item(iid, "values")[1] for iid in sel]
        if not messagebox.askyesno(
            t("settings.btn_delete"),
            t("settings.sources.confirm_delete", count=len(names), names=", ".join(names)),
            parent=tab.winfo_toplevel(),
        ):
            return
        for iid in sel:
            tree.delete(iid)

    ttk.Button(side, text=t("settings.btn_add"), command=_add, width=14).pack(fill="x", pady=2)
    ttk.Button(side, text=t("settings.btn_edit"), command=_edit, width=14).pack(fill="x", pady=2)
    ttk.Button(side, text=t("settings.btn_delete"), command=_delete, width=14).pack(fill="x", pady=2)
    ttk.Button(side, text=t("settings.btn_toggle"), command=_toggle, width=14).pack(fill="x", pady=(8, 2))
    return tree


def _build_commodities_tab(notebook, config):
    tab = ttk.Frame(notebook, padding=12)
    notebook.add(tab, text=t("settings.tab_commodities"))

    cols = ("enabled", "name", "keywords", "threshold")
    tree = ttk.Treeview(tab, columns=cols, show="headings", height=10)
    for col, label, width in (
        ("enabled", "On", 50),
        ("name", "Name", 130),
        ("keywords", "Keywords", 280),
        ("threshold", "Threshold (USD)", 130),
    ):
        tree.heading(col, text=label)
        tree.column(col, width=width, anchor="w")

    for c in config.get("commodities", []):
        tree.insert(
            "",
            "end",
            values=(
                "[x]" if c.get("enabled", True) else "[ ]",
                c.get("name", ""),
                ", ".join(c.get("keywords", [])),
                c.get("price_threshold", ""),
            ),
        )

    tree.pack(fill="both", expand=True, side="left")

    side = ttk.Frame(tab)
    side.pack(side="left", fill="y", padx=(8, 0))

    def _toggle():
        sel = tree.selection()
        if not sel:
            return
        for iid in sel:
            vals = list(tree.item(iid, "values"))
            vals[0] = "[ ]" if vals[0] == "[x]" else "[x]"
            tree.item(iid, values=vals)

    def _add():
        existing_names = {tree.item(iid, "values")[1] for iid in tree.get_children()}
        dlg = _EntryDialog(
            tab.winfo_toplevel(),
            t("settings.commodities.add_title"),
            fields=[
                ("name", t("settings.commodities.field_name"), "str"),
                ("keywords", t("settings.commodities.field_keywords"), "comma_list"),
                ("price_threshold", t("settings.commodities.field_threshold"), "optional_int"),
                ("enabled", t("settings.commodities.field_enabled"), "bool"),
            ],
        )
        if not dlg.result:
            return
        new = dlg.result
        if not new.get("name"):
            messagebox.showerror(
                t("settings.commodities.add_title"),
                t("settings.commodities.error_name_required"),
                parent=tab.winfo_toplevel(),
            )
            return
        if new["name"] in existing_names:
            messagebox.showerror(
                t("settings.commodities.add_title"),
                t("settings.commodities.error_name_exists", name=new["name"]),
                parent=tab.winfo_toplevel(),
            )
            return
        threshold = new.get("price_threshold")
        tree.insert(
            "",
            "end",
            values=(
                "[x]" if new.get("enabled", True) else "[ ]",
                new.get("name", ""),
                ", ".join(new.get("keywords", [])),
                "" if threshold is None else threshold,
            ),
        )

    def _edit():
        sel = tree.selection()
        if not sel:
            messagebox.showinfo(
                t("settings.commodities.edit_title"),
                t("settings.commodities.error_select_row"),
                parent=tab.winfo_toplevel(),
            )
            return
        iid = sel[0]
        vals = list(tree.item(iid, "values"))
        threshold_str = vals[3]
        dlg = _EntryDialog(
            tab.winfo_toplevel(),
            t("settings.commodities.edit_title"),
            fields=[
                ("name", t("settings.commodities.field_name"), "str"),
                ("keywords", t("settings.commodities.field_keywords"), "comma_list"),
                ("price_threshold", t("settings.commodities.field_threshold"), "optional_int"),
                ("enabled", t("settings.commodities.field_enabled"), "bool"),
            ],
            initial={
                "name": vals[1],
                "keywords": [s.strip() for s in vals[2].split(",") if s.strip()],
                "price_threshold": threshold_str,
                "enabled": vals[0] == "[x]",
            },
        )
        if not dlg.result:
            return
        new = dlg.result
        if not new.get("name"):
            messagebox.showerror(
                t("settings.commodities.edit_title"),
                t("settings.commodities.error_name_required"),
                parent=tab.winfo_toplevel(),
            )
            return
        threshold = new.get("price_threshold")
        tree.item(
            iid,
            values=(
                "[x]" if new.get("enabled", True) else "[ ]",
                new.get("name", ""),
                ", ".join(new.get("keywords", [])),
                "" if threshold is None else threshold,
            ),
        )

    def _delete():
        sel = tree.selection()
        if not sel:
            messagebox.showinfo(
                t("settings.btn_delete"),
                t("settings.commodities.error_select_row"),
                parent=tab.winfo_toplevel(),
            )
            return
        names = [tree.item(iid, "values")[1] for iid in sel]
        if not messagebox.askyesno(
            t("settings.btn_delete"),
            t("settings.commodities.confirm_delete", count=len(names), names=", ".join(names)),
            parent=tab.winfo_toplevel(),
        ):
            return
        for iid in sel:
            tree.delete(iid)

    ttk.Button(side, text=t("settings.btn_add"), command=_add, width=14).pack(fill="x", pady=2)
    ttk.Button(side, text=t("settings.btn_edit"), command=_edit, width=14).pack(fill="x", pady=2)
    ttk.Button(side, text=t("settings.btn_delete"), command=_delete, width=14).pack(fill="x", pady=2)
    ttk.Button(side, text=t("settings.btn_toggle"), command=_toggle, width=14).pack(fill="x", pady=(8, 2))
    return tree


def _build_filters_tab(notebook, config):
    tab = ttk.Frame(notebook, padding=12)
    notebook.add(tab, text=t("settings.tab_filters"))

    filters = config.get("filters", {})

    ttk.Label(tab, text="High priority keywords (one per line):").pack(anchor="w")
    high_text = tk.Text(tab, height=8, width=60)
    theme.style_text_widget(high_text)
    high_text.pack(fill="x", pady=4)
    high_text.insert("1.0", "\n".join(filters.get("high_priority_keywords", [])))
    # autosave wiring is done by build_widget after all tabs exist

    ttk.Label(tab, text="Blacklist keywords (one per line):").pack(anchor="w", pady=(12, 0))
    black_text = tk.Text(tab, height=8, width=60)
    theme.style_text_widget(black_text)
    black_text.pack(fill="x", pady=4)
    black_text.insert("1.0", "\n".join(filters.get("blacklist_keywords", [])))
    # autosave wiring is done by build_widget after all tabs exist

    ttk.Label(tab, text="Enable delta alert:").pack(anchor="w", pady=(12, 0))
    delta_enabled_var = tk.BooleanVar(value=filters.get("enable_delta_alert", False))
    ttk.Checkbutton(
        tab,
        text="Compare current vs last price, alert on >= delta_threshold_pct",
        variable=delta_enabled_var,
    ).pack(anchor="w")

    ttk.Label(tab, text="Delta threshold (%):").pack(anchor="w", pady=(8, 0))
    delta_var = tk.StringVar(value=str(filters.get("delta_threshold_pct", 5.0)))
    ttk.Entry(tab, textvariable=delta_var, width=10).pack(anchor="w")

    return high_text, black_text, delta_enabled_var, delta_var


def _build_about_tab(notebook, config_path=None, on_change_license=None):
    """Build the About tab.

    ``on_change_license`` is kept for backwards compatibility with the
    ``build_widget`` signature but is no longer wired up — the license
    UI now lives entirely in the General tab.
    """
    _ = on_change_license  # unused; preserved for API stability
    tab = ttk.Frame(notebook, padding=12)
    notebook.add(tab, text=t("settings.tab_about"))

    ttk.Label(tab, text="ThaoResearch v1.0.0", font=theme.font("title")).pack(anchor="w")
    ttk.Label(
        tab,
        text="Commodity News Intelligence & Alerting",
        foreground=theme.COLORS["fg_muted"],
    ).pack(anchor="w", pady=(0, 12))

    # License info used to live here (status line + Change/View License
    # buttons). It now lives in the General tab, so the About tab is
    # just app metadata + quick links.

    ttk.Separator(tab, orient="horizontal").pack(fill="x", pady=12)

    # Config file info
    ttk.Label(tab, text="Config files").pack(anchor="w")
    info_frame = ttk.Frame(tab)
    info_frame.pack(anchor="w", pady=(4, 0))
    ttk.Label(info_frame, text=f"Config: {config_path or paths.CONFIG_FILE}", foreground=theme.COLORS["fg_muted"], font=theme.font("caption")).pack(anchor="w")
    ttk.Label(info_frame, text=f"Data folder: {paths.DATA_DIR}  ({paths.DATA_DIR_SOURCE})", foreground=theme.COLORS["fg_muted"], font=theme.font("caption")).pack(anchor="w")

    ttk.Separator(tab, orient="horizontal").pack(fill="x", pady=12)
    ttk.Label(tab, text="Quick links").pack(anchor="w")
    link_frame = ttk.Frame(tab)
    link_frame.pack(anchor="w", pady=4)
    ttk.Button(link_frame, text="Open logs folder", command=lambda: _browse_path(paths.LOG_DIR)).pack(
        side="left", padx=(0, 8)
    )
    ttk.Button(link_frame, text="Open data folder", command=lambda: _browse_path(paths.DATA_DIR)).pack(
        side="left"
    )


def _gather_config(
    config,
    state,
    sources_tree,
    high_text,
    black_text,
    delta_enabled_var,
    delta_var,
):
    """Pull current widget values into a new config dict (no side effects)."""
    try:
        max_articles = int(state.max_articles_var.get())
        if max_articles < 1:
            raise ValueError("max_articles must be positive")
    except ValueError as e:
        messagebox.showerror("Invalid value", f"Invalid numeric field: {e}")
        return None

    new_config = dict(config)
    general_section = {
        **new_config.get("general", {}),
        "max_articles_per_run": max_articles,
        "timezone": state.tz_var.get().strip(),
        "language": state.lang_var.get(),
        "theme": state.theme_var.get(),
        "enable_desktop_toast": bool(state.toast_var.get()),
        "enable_telegram_alert": bool(state.tg_var.get()),
        "force_send_duplicates": bool(state.force_send_var.get()),
        "autosave_settings": bool(state.autosave_var.get()),
    }
    # License gate: never persist ``true`` for the two gated flags when
    # there's no valid license. The Settings UI already disables the
    # checkboxes but the persisted YAML is the source of truth on next
    # launch, so we make sure it can't be turned back on via editing
    # the file while the license is inactive.
    try:
        lic_ok, _ = license_mod.is_license_valid()
    except Exception:
        lic_ok = False
    if not lic_ok:
        general_section["enable_desktop_toast"] = False
        general_section["enable_telegram_alert"] = False
    new_config["general"] = general_section

    # Build the scheduler block. Whether the adaptive block is persisted depends
    # on the selected mode to avoid two sources of truth on disk.
    mode = state.scheduler_mode_var.get()
    scheduler_block = {
        **new_config.get("scheduler", {}),
        "mode": mode,
        "fixed_interval_minutes": int(state.fixed_interval_var.get() or 5),
    }
    if mode == "adaptive":
        scheduler_block["adaptive"] = {
            "weekday_business_hours": int(state.adaptive_business_hours_var.get()),
            "weekday_evening": int(state.adaptive_evening_var.get()),
            "weekend": int(state.adaptive_weekend_var.get()),
            "off_on_weekend": bool(state.adaptive_off_weekend_var.get()),
            "business_hours_start": int(state.adaptive_start_hour_var.get()),
            "business_hours_end": int(state.adaptive_end_hour_var.get()),
            "timezone": new_config["general"].get("timezone", "Asia/Ho_Chi_Minh"),
        }
    new_config["scheduler"] = scheduler_block

    sources_tree = state.sources_tree
    sources = []
    for iid in sources_tree.get_children():
        vals = sources_tree.item(iid, "values")
        # Try to preserve metadata from existing entry. Prefer the original
        # name (the iid tag) before the user renamed the row.
        orig_name = iid if isinstance(iid, str) else None
        existing_source = None
        if orig_name and orig_name in [s.get("name") for s in config.get("sources", [])]:
            existing_source = next(
                (s for s in config.get("sources", []) if s.get("name") == orig_name),
                None,
            )
        if existing_source is None:
            existing_source = next(
                (s for s in config.get("sources", []) if s.get("name") == vals[1]),
                None,
            )
        if existing_source:
            sources.append(
                {
                    **existing_source,
                    "name": vals[1],
                    "type": vals[2] or existing_source.get("type", "rss"),
                    "url": vals[3],
                    "enabled": vals[0] == "[x]",
                }
            )
        else:
            sources.append(
                {
                    "name": vals[1],
                    "type": vals[2] or "rss",
                    "url": vals[3],
                    "enabled": vals[0] == "[x]",
                }
            )
    new_config["sources"] = sources

    commodities_tree = state.commodities_tree
    commodities = []
    for iid in commodities_tree.get_children():
        vals = commodities_tree.item(iid, "values")
        threshold_raw = vals[3]
        threshold_val: Optional[float]
        if threshold_raw in ("", None):
            threshold_val = None
        else:
            try:
                threshold_val = float(threshold_raw)
            except ValueError:
                threshold_val = None
        keywords_list = [s.strip() for s in vals[2].split(",") if s.strip()]

        orig_name = iid if isinstance(iid, str) else None
        existing_commodity = None
        if orig_name and orig_name in [c.get("name") for c in config.get("commodities", [])]:
            existing_commodity = next(
                (c for c in config.get("commodities", []) if c.get("name") == orig_name),
                None,
            )
        if existing_commodity is None:
            existing_commodity = next(
                (c for c in config.get("commodities", []) if c.get("name") == vals[1]),
                None,
            )
        if existing_commodity:
            entry: dict = {
                **existing_commodity,
                "name": vals[1],
                "keywords": keywords_list,
                "enabled": vals[0] == "[x]",
            }
            if threshold_val is not None:
                entry["price_threshold"] = threshold_val
            elif "price_threshold" in entry:
                entry.pop("price_threshold", None)
            commodities.append(entry)
        else:
            entry = {
                "name": vals[1],
                "keywords": keywords_list,
                "enabled": vals[0] == "[x]",
            }
            if threshold_val is not None:
                entry["price_threshold"] = threshold_val
            commodities.append(entry)
    new_config["commodities"] = commodities

    new_config["filters"] = {
        "high_priority_keywords": [
            line.strip() for line in high_text.get("1.0", "end").splitlines() if line.strip()
        ],
        "blacklist_keywords": [
            line.strip() for line in black_text.get("1.0", "end").splitlines() if line.strip()
        ],
        "enable_delta_alert": bool(delta_enabled_var.get()),
        "delta_threshold_pct": float(delta_var.get() or 5.0),
    }

    # Telegram (token + chat_id) lives in .env and is loaded at runtime.
    # It is intentionally NOT gathered here so it stays out of config.yaml.

    new_config["_autostart"] = bool(state.autostart_var.get())

    return new_config


def _persist_config(new_config, config_path, state, parent_window):
    """Write init_config.yaml + config.yaml + apply autostart. Show dialogs. Return True on success.

    The autosave loop writes only ``init_config.yaml`` (so a half-edited
    field never clobbers the file the user originally saved). The
    manual Save button used to write only ``config.yaml`` — but the
    load order at startup prefers ``init_config.yaml`` when it exists,
    so a manual Save could end up being silently shadowed by an older
    autosave. Writing to BOTH files from manual Save guarantees that
    "I clicked Save and the value changed" is always true regardless
    of which file the next launch loads from.
    """
    if new_config is None:
        return False

    # Write to init_config.yaml first (the canonical autosave target,
    # also the file the load order prefers on next launch).
    init_target = _resolve_init_config_path(config_path)
    if not _persist_config_to(new_config, init_target):
        return False

    # Mirror to config.yaml so a manual Save still updates the file the
    # user typically edits by hand. We skip this if the resolved paths
    # happen to be the same (single-file mode).
    try:
        if Path(init_target).resolve() != Path(config_path).resolve():
            _persist_config_to(new_config, config_path)
    except Exception:
        # Mirror failure shouldn't fail the Save — init_config.yaml is
        # already up to date.
        pass

    # Telegram credentials (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID) live in .env
    # and are managed manually — not written from the Settings GUI.

    try:
        if new_config.get("_autostart"):
            autostart.install_autostart()
        else:
            autostart.uninstall_autostart()
    except Exception as e:
        messagebox.showwarning("Autostart", f"Failed to update autostart: {e}")

    i18n.set_language(new_config.get("general", {}).get("language", "en"))

    messagebox.showinfo("ThaoResearch", "Settings saved.", parent=parent_window)
    return True


def _persist_config_to(new_config, target_path: Path) -> bool:
    """Atomic YAML write to ``target_path``. Returns True on success.

    Shared by the manual Save button (which writes ``config.yaml`` and
    shows a dialog) and the autosave loop (which writes
    ``init_config.yaml`` silently). We write to a ``.tmp`` neighbour
    first then ``os.replace`` so a crash mid-write never leaves a
    half-written config file that the next launch can't parse.
    """
    try:
        from ruamel.yaml import YAML

        y = YAML()
        y.preserve_quotes = True
        persist = {k: v for k, v in new_config.items() if not k.startswith("_")}
        tmp = target_path.with_suffix(target_path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            y.dump(persist, f)
        os.replace(tmp, target_path)
        return True
    except ImportError:
        import yaml

        try:
            persist = {k: v for k, v in new_config.items() if not k.startswith("_")}
            tmp = target_path.with_suffix(target_path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                yaml.safe_dump(persist, f, allow_unicode=True, sort_keys=False)
            os.replace(tmp, target_path)
            return True
        except Exception as e:
            logger.warning("Write %s failed: %s", target_path, e)
            return False
    except Exception as e:
        logger.warning("Write %s failed: %s", target_path, e)
        return False


class SettingsWindow:
    """Standalone Toplevel hosting the Settings form."""

    def __init__(
        self,
        config: dict,
        config_path: Path,
        on_save: Optional[Callable[[dict], None]] = None,
        on_change_license: Optional[Callable[[], None]] = None,
    ):
        self.original_config = config
        self.config_path = config_path
        self.on_save = on_save
        self.on_change_license = on_change_license
        self.window = tk.Tk()
        self.window.title(t("settings.title", default="Settings - ThaoResearch"))
        self.window.geometry("700x650")
        self.window.minsize(600, 500)
        apply_app_icon(self.window)
        self._widget = build_widget(
            self.window,
            config=config,
            config_path=config_path,
            on_save=on_save,
            on_change_license=on_change_license,
            show_outer_buttons=True,
        )
        self._widget.pack(fill="both", expand=True)

    def show(self):
        try:
            self.window.mainloop()
        finally:
            try:
                self.window.destroy()
            except tk.TclError:
                pass


def open_settings(config: dict, config_path: Path, on_save: Optional[Callable[[dict], None]] = None):
    SettingsWindow(config, config_path, on_save=on_save).show()