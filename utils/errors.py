"""Custom exceptions with stable error codes (E01xx..E09xx).

Adding new codes is OK; do NOT renumber existing codes.
"""
from __future__ import annotations

from typing import Optional


class ThaoResearchError(Exception):
    """Base exception for all ThaoResearch errors."""

    code: str = "E0000"
    suggestion: str = "See logs/app.log for details."

    def __init__(self, message: str = "", suggestion: Optional[str] = None):
        super().__init__(message)
        if suggestion is not None:
            self.suggestion = suggestion


# ---- E01xx Configuration & Setup ----
class ConfigError(ThaoResearchError):
    code = "E0100"
    suggestion = "Check config.yaml syntax. See config.yaml.example for reference."


class InvalidYAMLError(ThaoResearchError):
    code = "E0101"
    suggestion = "Fix YAML syntax (indentation must be 2 spaces, no tabs)."


class MissingFieldError(ThaoResearchError):
    code = "E0102"
    suggestion = "Add the missing field. See config.yaml.example."


class InvalidURLError(ThaoResearchError):
    code = "E0103"
    suggestion = "URL must start with http:// or https://."


# ---- E02xx .env file & Secrets ----
class EnvFileError(ThaoResearchError):
    code = "E0200"
    suggestion = "Copy .env.example to .env and fill in values."


class EmptyTokenError(ThaoResearchError):
    code = "E0201"
    suggestion = "See INSTALL_TELEGRAM.md to create a bot, or set general.enable_telegram_alert: false."


class InvalidTokenError(ThaoResearchError):
    code = "E0202"
    suggestion = "Copy TELEGRAM_BOT_TOKEN again from @BotFather. No extra spaces."


class EmptyChatIdError(ThaoResearchError):
    code = "E0203"
    suggestion = "See INSTALL_TELEGRAM.md section 2 to get your Chat ID."


class InvalidChatIdError(ThaoResearchError):
    code = "E0204"
    suggestion = "Chat ID must be a number like 987654321 (private) or -100123456789 (group)."


# ---- E03xx Network & HTTP ----
class NetworkTimeoutError(ThaoResearchError):
    code = "E0300"
    suggestion = "Check internet connection. Will retry next run."


class DNSError(ThaoResearchError):
    code = "E0301"
    suggestion = "Check DNS settings or VPN."


class SSLError(ThaoResearchError):
    code = "E0302"
    suggestion = "Check system clock. If behind a corporate proxy, configure HTTPS_PROXY."


class HTTPClientError(ThaoResearchError):
    code = "E0303"
    suggestion = "Site may block bots. Add a User-Agent header or use a different source."


class HTTPServerError(ThaoResearchError):
    code = "E0304"
    suggestion = "Source is temporarily down. Skipped this run. Will retry next."


class RateLimitError(ThaoResearchError):
    code = "E0305"
    suggestion = "Reduce check_interval to >= 5 min."


# ---- E04xx Telegram API ----
class TelegramAuthError(ThaoResearchError):
    code = "E0400"
    suggestion = "Token may be revoked. Send /revoke to @BotFather, then update .env."


class TelegramForbiddenError(ThaoResearchError):
    code = "E0401"
    suggestion = "Bot was blocked by the user. Update TELEGRAM_CHAT_ID."


class TelegramChatNotFoundError(ThaoResearchError):
    code = "E0402"
    suggestion = "Add bot back to the group, OR update TELEGRAM_CHAT_ID in .env."


class TelegramBadRequestError(ThaoResearchError):
    code = "E0403"
    suggestion = "Title may contain Markdown-breaking chars. See error log."


class TelegramMessageTooLongError(ThaoResearchError):
    code = "E0404"
    suggestion = "Article too long. Truncate summary to 500 chars max."


class TelegramRetryAfterError(ThaoResearchError):
    code = "E0405"
    suggestion = "Tool will wait and retry automatically."


class TelegramRetriesExhaustedError(ThaoResearchError):
    code = "E0407"
    suggestion = "Article NOT sent to Telegram. Still shown in Desktop GUI."


# ---- E05xx RSS / HTML Parsing ----
class RSSMalformedError(ThaoResearchError):
    code = "E0500"
    suggestion = "Source may have changed format. Disable in config.yaml or report bug."


class RSSEmptyError(ThaoResearchError):
    code = "E0501"
    suggestion = "Source may be temporarily empty. Will retry next run."


class HTMLParseError(ThaoResearchError):
    code = "E0503"
    suggestion = "Site may have changed layout. Check CSS selectors in config.yaml."


# ---- E06xx Database ----
class DatabaseLockedError(ThaoResearchError):
    code = "E0600"
    suggestion = "Another instance of ThaoResearch is running. Close it."


class DatabaseCorruptedError(ThaoResearchError):
    code = "E0601"
    suggestion = "Delete data/history.db and restart (history will be lost)."


class DiskFullError(ThaoResearchError):
    code = "E0602"
    suggestion = "Free up disk space. Data dir is at ./data/."


class PermissionDeniedError(ThaoResearchError):
    code = "E0603"
    suggestion = "Check folder permissions. Run as Administrator if needed."


# ---- E07xx Scheduler & Runtime ----
class SchedulerError(ThaoResearchError):
    code = "E0700"
    suggestion = "Check logs/app.log for details. Restart with --verbose."


class JobCrashedError(ThaoResearchError):
    code = "E0701"
    suggestion = "Run python main.py --now --verbose to reproduce."


class UserStopError(ThaoResearchError):
    code = "E0702"
    suggestion = "Graceful shutdown completed."


# ---- E08xx Desktop UI ----
class TrayIconError(ThaoResearchError):
    code = "E0800"
    suggestion = "Windows may block unregistered apps. Try running as Administrator."


class ToastError(ThaoResearchError):
    code = "E0801"
    suggestion = "Check Windows notification settings. Enable notifications for ThaoResearch."


class TkinterWindowError(ThaoResearchError):
    code = "E0802"
    suggestion = "Display issue. Restart the app."


class AutostartError(ThaoResearchError):
    code = "E0803"
    suggestion = "Run as Administrator for autostart to work."


# ---- E09xx License ----
class LicenseMissingError(ThaoResearchError):
    code = "E0900"
    suggestion = "Enter your license key when prompted."


class LicenseInvalidError(ThaoResearchError):
    code = "E0901"
    suggestion = "Check the key. For demo, use '1234'."


class LicenseExpiredError(ThaoResearchError):
    code = "E0902"
    suggestion = "Contact vendor for a new license."


class LicenseCorruptedError(ThaoResearchError):
    code = "E0903"
    suggestion = "Delete data/license.dat and re-enter your license."


class LicenseTooManyAttemptsError(ThaoResearchError):
    code = "E0904"
    suggestion = "Restart and try again. If you forgot your key, contact vendor."


# ---- Helper ----
def format_error(error: BaseException) -> str:
    """Format an exception as a user-facing text-only error block.

    >>> print(format_error(InvalidTokenError("bad format")))
    [E0202] InvalidTokenError
      What happened: bad format
      What to do:    Copy TELEGRAM_BOT_TOKEN again from @BotFather. No extra spaces.
      Docs:          Search 'E0202' in README or INSTALL_TELEGRAM.md
    """
    if isinstance(error, ThaoResearchError):
        return (
            f"[{error.code}] {type(error).__name__}: {error}\n"
            f"  What happened: {error}\n"
            f"  What to do:    {error.suggestion}\n"
            f"  Docs:          Search '{error.code}' in README or INSTALL_TELEGRAM.md\n"
        )
    return (
        f"[E9999] Unexpected error: {type(error).__name__}: {error}\n"
        f"  What happened: {error}\n"
        f"  What to do:    Run with --verbose for full traceback. "
        f"Report this bug if it persists.\n"
    )
