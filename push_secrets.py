"""Copy the Binance/GitHub secrets from this laptop (DPAPI) to the VPS over SSH.

Usage: py -3 -B push_secrets.py [--telegram] tr@<VPS IP>
  (default) Binance key/secret + GitHub token -> ~/trading-review/secrets.json
  --telegram  Telegram api_id/api_hash only    -> ~/trading-review/telegram.json
              (the login session is NOT copied; log in on the VPS with tg_login.py)
Values travel only inside the SSH connection (stdin of the remote `cat`); nothing
is printed or written to a local file. Remote files are mode 600.
"""
import json
import re
import subprocess
import sys

import secure_store


def remote(name):
    f = "~/trading-review/" + name
    return (f"umask 077 && mkdir -p ~/trading-review && cat > {f}.tmp && mv {f}.tmp {f}"
            f" && chmod 600 {f} && echo saved")


def main(argv):
    telegram = argv[:1] == ["--telegram"]
    if telegram:
        argv = argv[1:]
    if len(argv) != 1 or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}@[0-9A-Za-z.:-]{1,253}", argv[0]):
        print("用法：py -3 -B push_secrets.py [--telegram] tr@VPS的IP")
        return 2
    values = secure_store.load_tg() if telegram else secure_store.load_secrets()
    data = json.dumps(values).encode("utf-8")
    name = "telegram.json" if telegram else "secrets.json"
    return subprocess.run(["ssh", argv[0], remote(name)], input=data).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
