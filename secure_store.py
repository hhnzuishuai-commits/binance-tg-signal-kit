"""Local secrets: Windows DPAPI on the laptop, file mode 600 on the Linux VPS.

Windows: encrypted for the current Windows user only; useless elsewhere.
Linux: no DPAPI. A key stored next to the data would add nothing, so the file
is plain JSON readable only by its owner (dir 700, file 600); loading refuses
a file that group/others can read. Values are never printed. Tests inject a
fake protector.
"""
import ctypes
import json
import os
import sys

WINDOWS = sys.platform == "win32"
ENTROPY = b"trading-review/binance-daily-export/v1"
APP_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "trading-review")
SECRETS_PATH = os.path.join(APP_DIR, "secrets.dpapi" if WINDOWS else "secrets.json")
FIELDS = ("binance_api_key", "binance_secret", "github_token", "github_repo")


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data):
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _dpapi(data, protect):
    if sys.platform != "win32":
        raise OSError("DPAPI requires Windows")
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    inp, _keep = _blob(data)
    ent, _keep2 = _blob(ENTROPY)
    out = _Blob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if protect:
        ok = fn(ctypes.byref(inp), "trading-review", ctypes.byref(ent), None, None, 0x1, ctypes.byref(out))
    else:
        ok = fn(ctypes.byref(inp), None, ctypes.byref(ent), None, None, 0x1, ctypes.byref(out))
    if not ok:
        raise OSError("DPAPI operation failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def dpapi_protect(data):
    return _dpapi(data, True)


def dpapi_unprotect(data):
    return _dpapi(data, False)


def _plain(data):
    return data


PROTECT, UNPROTECT = (dpapi_protect, dpapi_unprotect) if WINDOWS else (_plain, _plain)


def _write_private(path, data):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def _read_private(path):
    if not WINDOWS and os.stat(path).st_mode & 0o077:
        raise PermissionError("secrets file readable by others; run: chmod 600 " + path)
    with open(path, "rb") as fh:
        return fh.read()


def save_secrets(values, *, path=SECRETS_PATH, protect=PROTECT):
    if set(values) != set(FIELDS) or any(not isinstance(v, str) or not v for v in values.values()):
        raise ValueError("all secret fields required")
    _write_private(path, protect(json.dumps(values).encode("utf-8")))


def load_secrets(*, path=SECRETS_PATH, unprotect=UNPROTECT):
    values = json.loads(unprotect(_read_private(path)).decode("utf-8"))
    if set(values) != set(FIELDS):
        raise ValueError("secrets file incomplete; run setup again")
    return values


TG_PATH = os.path.join(APP_DIR, "telegram.dpapi" if WINDOWS else "telegram.json")
TG_FIELDS = ("api_id", "api_hash")


def save_tg(values, *, path=TG_PATH, protect=PROTECT):
    if set(values) != set(TG_FIELDS) or any(not isinstance(v, str) or not v for v in values.values()):
        raise ValueError("api_id and api_hash required")
    _write_private(path, protect(json.dumps(values).encode("utf-8")))


def load_tg(*, path=TG_PATH, unprotect=UNPROTECT):
    values = json.loads(unprotect(_read_private(path)).decode("utf-8"))
    if set(values) != set(TG_FIELDS):
        raise ValueError("telegram secrets incomplete")
    return values
