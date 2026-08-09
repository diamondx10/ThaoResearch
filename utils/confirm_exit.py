"""Confirm-on-exit helper.

Runs a background thread that listens for the Escape key in the Windows
console. On the first Esc, it prompts the user (in English) to confirm
exit. On a second Esc within CONFIRM_WINDOW_SECONDS, it exits immediately
without asking.

On non-Windows platforms this falls back to a tty/termios approach for
POSIX terminals. On Windows we use `win32console.ReadConsoleInput` so we
can read a single key press even when stdin has been redirected (the
crawler's normal output won't accidentally trigger Esc reads).
"""
from __future__ import annotations

import sys
import threading
import time
from typing import Callable, Optional

CONFIRM_WINDOW_SECONDS = 3.0
PROMPT_MESSAGE = "\n  Are you sure you want to exit? (y/n) "
ESC_AGAIN_MESSAGE = "\n  Press Esc again within 3s to force exit "
GOODBYE_MESSAGE = "\n  Exiting ThaoResearch. Goodbye."
ESC_KEY_EVENT = {"VK_ESCAPE": 0x1B}


# ---------------------------------------------------------------------------
# Windows console reader
# ---------------------------------------------------------------------------
def _windows_reader_factory():
    """Return a (read_key, has_console) callable pair for Windows.

    `read_key()` blocks until a key is pressed and returns one of:
      - 'esc'       for the Escape key
      - 'y' / 'n'   for yes/no answers
      - None        if no console is attached (detached process)
    """
    try:
        import win32console  # type: ignore[import-not-found]
    except ImportError:
        return None, False

    try:
        # Save current mode so we can restore on exit
        console = win32console.GetStdHandle(win32console.STD_INPUT_HANDLE)
        original_mode = console.GetConsoleMode()
        # Enable window input (so we get resize/etc events) and keep processed
        # input ON so Escape is delivered as a normal key, not a VK_ESCAPE
        # consumed by Windows. We do NOT need to disable processed input to
        # receive Esc key events.
        console.SetConsoleMode(
            original_mode | win32console.ENABLE_WINDOW_INPUT
        )
    except Exception:
        # Process has no console (e.g. launched detached by Start-Process)
        return None, False

    def read_key() -> Optional[str]:
        try:
            # Peek to know how many events are buffered (non-blocking check)
            events = console.PeekConsoleInput(1)
            if not events:
                time.sleep(0.1)
                return None
            events = console.ReadConsoleInput(1)
        except Exception:
            return None
        for ev in events:
            # pywin32 >= 306 flattens the input record; no .KeyEvent sub-object.
            event_type = getattr(ev, "EventType", None)
            if event_type != 1:  # 1 = KEY_EVENT
                continue
            key_down = getattr(ev, "KeyDown", None)
            if key_down == 0:  # key-up, ignore
                continue
            vkey = getattr(ev, "VirtualKeyCode", None)
            char = getattr(ev, "Char", "") or ""
            if vkey == 0x1B:  # VK_ESCAPE
                return "esc"
            if isinstance(char, str) and char.lower() in ("y", "n"):
                return char.lower()
            # Fallback: numeric Char (some consoles return int)
            try:
                if isinstance(char, int) and chr(char).lower() in ("y", "n"):
                    return chr(char).lower()
            except (ValueError, OverflowError):
                pass
        return None

    return read_key, True


# ---------------------------------------------------------------------------
# POSIX reader (fallback)
# ---------------------------------------------------------------------------
def _posix_reader_factory():
    try:
        import tty
        import termios  # type: ignore[import-not-found]
    except ImportError:
        return None, False

    if not sys.stdin.isatty():
        return None, False

    fd = sys.stdin.fileno()

    def read_key() -> Optional[str]:
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.buffer.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        if not ch:
            return None
        if ch == b"\x1b":
            return "esc"
        if ch.lower() in (b"y", b"n"):
            return ch.decode("ascii").lower()
        return None

    return read_key, True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def _make_reader():
    if sys.platform == "win32":
        return _windows_reader_factory()
    return _posix_reader_factory()


def _ask_yes_no(prompt: str) -> bool:
    """Print prompt, read a single y/n answer from stdin. Default = No."""
    sys.stdout.write(prompt)
    sys.stdout.flush()
    try:
        answer = sys.stdin.readline().strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes")


def wait_for_exit_signal(on_confirmed_exit: Callable[[], None]) -> None:
    """Blocking loop: watch for Esc, prompt to confirm, call callback on exit."""
    read_key, ok = _make_reader()
    if not ok:
        # No console attached; nothing to do
        return

    sys.stdout.write(
        "\n  Press Esc at any time to exit (you will be asked to confirm).\n"
    )
    sys.stdout.flush()

    last_esc_time: Optional[float] = None

    while True:
        key = read_key()
        if key is None:
            continue
        if key != "esc":
            continue

        now = time.monotonic()
        if last_esc_time is not None and (now - last_esc_time) < CONFIRM_WINDOW_SECONDS:
            sys.stdout.write(f"\n{GOODBYE_MESSAGE}\n")
            sys.stdout.flush()
            on_confirmed_exit()
            return

        last_esc_time = now
        sys.stdout.write(ESC_AGAIN_MESSAGE)
        sys.stdout.flush()

        # Wait up to CONFIRM_WINDOW_SECONDS for second Esc or y/n
        end = now + CONFIRM_WINDOW_SECONDS
        confirmed = False
        while time.monotonic() < end:
            key = read_key()
            if key is None:
                continue
            if key == "esc":
                sys.stdout.write(f"\n{GOODBYE_MESSAGE}\n")
                sys.stdout.flush()
                on_confirmed_exit()
                return
            if key in ("y", "n"):
                confirmed = (key == "y")
                break

        if confirmed:
            sys.stdout.write(f"\n{GOODBYE_MESSAGE}\n")
            sys.stdout.flush()
            on_confirmed_exit()
            return

        # No second Esc within window: fall back to y/n prompt on stdin
        if _ask_yes_no(PROMPT_MESSAGE):
            sys.stdout.write(f"\n{GOODBYE_MESSAGE}\n")
            sys.stdout.flush()
            on_confirmed_exit()
            return

        sys.stdout.write("  Resumed. Press Esc again to exit.\n")
        sys.stdout.flush()
        last_esc_time = None


def start_exit_watcher(on_confirmed_exit: Callable[[], None]) -> Optional[threading.Thread]:
    """Spawn the exit-watcher in a daemon thread. Returns None if no console."""
    reader, ok = _make_reader()
    if not ok:
        return None  # no console, nothing to watch
    t = threading.Thread(
        target=wait_for_exit_signal,
        args=(on_confirmed_exit,),
        name="exit-watcher",
        daemon=True,
    )
    t.start()
    return t