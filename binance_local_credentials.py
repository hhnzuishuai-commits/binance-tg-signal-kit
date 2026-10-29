"""Explicit local-console entry only; no file/env/vault access or persistence."""
import getpass
import warnings
from binance_readonly_transport import MemoryCredentials, ReadOnlyError


def prompt_memory_credentials(*, entry_authorized=False):
    """Call only after user confirms this entry action. Never call via AI tooling.

    Not a CLI and never auto-invoked. No echo fallback when console hiding fails.
    Strings cannot be reliably zeroized in Python; exit the process after use.
    """
    if entry_authorized is not True:
        raise ReadOnlyError("credential_entry_not_authorized")
    failure = None
    result = None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            key = getpass.getpass("Binance read-only API key (hidden): ")
            secret = getpass.getpass("Binance HMAC secret (hidden): ")
            result = MemoryCredentials(key, secret)
    except ReadOnlyError as exc:
        failure = ("credential_mask_placeholder" if str(exc) == "credential_mask_placeholder"
                   else "credential_entry_cancelled_or_invalid")
    except (Exception, KeyboardInterrupt):
        failure = "credential_entry_cancelled_or_invalid"
    if failure:
        raise ReadOnlyError(failure)
    return result
