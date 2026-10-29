"""One-time Telegram login on the VPS: creates a NEW session (never copied from the laptop).

Run in an interactive SSH terminal:  ~/venv/bin/python -B tg_login.py
First run asks for the API ID / Hash from my.telegram.org (Hash typed hidden) and
saves them with secure_store (mode 600). Phone number, login code and 2FA password
are typed by the user and go straight to Telegram; nothing is printed or logged.
Session file is mode 600. To revoke later: Telegram → 设置 → 设备 → 终止这个会话.
"""
import getpass
import os
import sys

import auto_run
import secure_store


def main():
    os.umask(0o077)
    if not os.path.exists(secure_store.TG_PATH):
        api_id = input("API ID（my.telegram.org，纯数字）: ").strip()
        api_hash = getpass.getpass("API Hash（输入不显示）: ").strip()
        if not api_id.isdigit() or len(api_hash) < 20:
            print("格式不对，已取消。")
            return 2
        secure_store.save_tg({"api_id": api_id, "api_hash": api_hash})
    tg = secure_store.load_tg()
    from telethon.sync import TelegramClient
    client = TelegramClient(auto_run.TG_SESSION, int(tg["api_id"]), tg["api_hash"],
                            receive_updates=False, device_model="trading-review VPS")
    print("=== Telegram 登录（VPS）===")
    print("手机号用国际格式，例如 +86138xxxxxxxx。验证码会发到你的 Telegram App。")
    print("开了两步验证的话，最后会让你输密码（不显示）。\n")
    client.start()
    try:
        ok = client.is_user_authorized()
    finally:
        client.disconnect()
    os.chmod(auto_run.TG_SESSION, 0o600)
    print("\n登录成功。" if ok else "\n登录失败。")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
