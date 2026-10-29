"""One-time setup: store keys encrypted, test them, register two scheduled tasks.

Run via setup_auto.bat. Keys are typed hidden and saved with Windows DPAPI.
"""
import getpass
import json
import os
import subprocess
import sys
import tempfile

from account_wide import AccountWideTransport
from binance_readonly_transport import MemoryCredentials, ReadOnlyError
from github_upload import GitHubUploader, UploadError
import secure_store

DEFAULT_REPO = "your-github-name/trading-review"   # 改成你自己的 GitHub 用户名/仓库名
HERE = os.path.dirname(os.path.abspath(__file__))
TONIGHT_TIME = "22:50"
CATCHUP_TIME = "08:30"


def ask_hidden(prompt):
    value = getpass.getpass(prompt).strip()
    if not value:
        raise SystemExit("未输入，已取消。")
    return value


def pythonw():
    candidate = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return candidate if os.path.exists(candidate) else sys.executable


def ps_quote(s):
    return "'" + s.replace("'", "''") + "'"


def register_tasks():
    exe, script = pythonw(), os.path.join(HERE, "auto_run.py")
    lines = [
        "$ErrorActionPreference = 'Stop'",
        "$set1 = New-ScheduledTaskSettingsSet -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-ExecutionTimeLimit (New-TimeSpan -Minutes 20)",
        "$set2 = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-ExecutionTimeLimit (New-TimeSpan -Minutes 20)",
    ]
    for name, mode, time, settings in (("TradingReview-Tonight", "tonight", TONIGHT_TIME, "$set1"),
                                       ("TradingReview-Catchup", "catchup", CATCHUP_TIME, "$set2")):
        arg = f'"{script}" --mode {mode}'
        lines += [
            f"$a = New-ScheduledTaskAction -Execute {ps_quote(exe)} -Argument {ps_quote(arg)} "
            f"-WorkingDirectory {ps_quote(HERE)}",
            f"$t = New-ScheduledTaskTrigger -Daily -At {time}",
            f"Register-ScheduledTask -TaskName {ps_quote(name)} -Action $a -Trigger $t -Settings {settings} -Force | Out-Null",
        ]
    fd, path = tempfile.mkstemp(suffix=".ps1")
    with os.fdopen(fd, "w", encoding="utf-8-sig") as fh:
        fh.write("\n".join(lines))
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path],
                           capture_output=True, text=True)
    finally:
        os.remove(path)
    return r.returncode == 0, (r.stderr or "").strip()[-500:]


def main():
    print("=== 每日交易复盘：自动化安装 ===")
    print("将要：加密保存币安只读 Key 和 GitHub 令牌 → 测试 → 创建两个定时任务")
    print(f"  每天 {TONIGHT_TIME} 导出当天数据并上传；每天 {CATCHUP_TIME}（错过则开机后）补全前一天。\n")
    key = ask_hidden("币安只读 API Key（输入不显示）: ")
    secret = ask_hidden("币安 Secret Key（输入不显示）: ")
    token = ask_hidden("GitHub 令牌 github_pat_...（输入不显示）: ")
    repo = input(f"GitHub 仓库 [{DEFAULT_REPO}]: ").strip() or DEFAULT_REPO

    print("\n[1/4] 检查币安 Key 权限……")
    try:
        AccountWideTransport(MemoryCredentials(key, secret), live_authorized=True).check_permissions()
    except (ReadOnlyError, ValueError) as exc:
        print("  失败：", exc)
        print("  Key 必须：只勾『允许读取』、设置 IP 白名单、不开任何交易/提现权限。")
        return 2
    print("  通过：只读 + IP 白名单。")

    print("[2/4] 检查 GitHub 令牌……")
    try:
        GitHubUploader(token, repo).put_file("status/setup.json",
            json.dumps({"setup": "ok", "tasks": [TONIGHT_TIME, CATCHUP_TIME]}).encode(), "setup check")
    except (UploadError, ValueError) as exc:
        print("  失败：", exc)
        print("  令牌需要：只选 trading-review 仓库，Contents 读写。")
        return 2
    print("  通过：可以写入仓库。")

    print("[3/4] 加密保存……")
    secure_store.save_secrets({"binance_api_key": key, "binance_secret": secret,
                               "github_token": token, "github_repo": repo})
    if secure_store.WINDOWS:
        print("  已保存到", secure_store.SECRETS_PATH, "（只有当前 Windows 账号能解密）")
    else:
        print("  已保存到", secure_store.SECRETS_PATH, "（权限 600，只有当前 Linux 用户能读）")

    print("[4/4] 创建定时任务……")
    if not secure_store.WINDOWS:
        print("  Linux：跳过 Windows 定时任务，请按部署清单设置 cron。")
    else:
        ok, err = register_tasks()
        if not ok:
            print("  创建失败：", err)
            print("  可以右键 setup_auto.bat → 以管理员身份运行，再试一次。")
            return 2
        print("  已创建：TradingReview-Tonight、TradingReview-Catchup")

    if input("\n现在先跑一次（导出最近 7 天 + 今天截至现在）？(y/n) ").strip().lower() == "y":
        import auto_run
        code = auto_run.main(["--mode", "tonight"])
        print("完成。" if code == 0 else "有部分失败，详情见日志：" + auto_run.LOG_PATH)
        print("本地副本：", auto_run.LOCAL_OUT)
    print("\n安装完成。以后不用再管它；需要改 Key 时重新运行 setup_auto.bat。")
    return 0


if __name__ == "__main__":
    code = main()
    input("\n按回车关闭……")
    sys.exit(code)
