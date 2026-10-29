"""每日币安合约只读导出。在你自己的电脑上运行。

用法（PowerShell 或双击 run_daily_export.bat）：
    python run_daily_export.py                 # 导出昨天（UTC+8）
    python run_daily_export.py --day today     # 导出今天截至现在
    python run_daily_export.py --day 2026-10-23
    python run_daily_export.py --out "D:\\交易复盘"

API Key / Secret 运行时隐藏输入，只留在内存，不写文件、不上传。
"""
import argparse
from datetime import datetime, timedelta
import os
import sys

from account_wide import AccountWideTransport
from binance_local_credentials import prompt_memory_credentials
from binance_readonly_transport import ReadOnlyError
from daily_export import build_report, write_outputs
from review_core import TZ

DEFAULT_OUT = os.path.join(os.path.expanduser("~"), "Documents", "交易复盘")

HINTS = {
    "authentication_or_permission_denied": "Key 无效、IP 不在白名单，或该 Key 没有合约读取权限。",
    "permission_check_failed": "Key 必须勾选「允许读取」并设置 IP 白名单。",
    "write_permission_or_unknown_permission": "这个 Key 开了交易/提现等写权限，为安全起见拒绝使用。请换一个只读 Key。",
    "rate_limited_no_retry": "触发币安限频，等几分钟再试。",
    "transport_or_decode_error": "网络连接失败，检查网络或加速器后重试。",
}


def resolve_day(value):
    now = datetime.now(TZ)
    if value in (None, "yesterday"):
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")
    if value == "today":
        return now.strftime("%Y-%m-%d")
    datetime.strptime(value, "%Y-%m-%d")
    return value


def main(argv=None):
    ap = argparse.ArgumentParser(description="币安 U 本位合约每日只读导出")
    ap.add_argument("--day", default="yesterday", help="yesterday / today / YYYY-MM-DD")
    ap.add_argument("--out", default=DEFAULT_OUT, help="输出文件夹")
    args = ap.parse_args(argv)
    day = resolve_day(args.day)
    print(f"将用只读 API Key 读取币安合约账户 {day} 的数据，输出到：{args.out}")
    if input("继续？(y/n) ").strip().lower() != "y":
        print("已取消。")
        return 1
    try:
        creds = prompt_memory_credentials(entry_authorized=True)
        transport = AccountWideTransport(creds, live_authorized=True)
        report = build_report(transport, day, clock_ms=transport._now)
        path, ann = write_outputs(report, args.out)
    except ReadOnlyError as exc:
        code = str(exc)
        print("失败：", code, exc.safe_diagnostics() or "")
        print(HINTS.get(code, ""))
        return 2
    except ValueError as exc:
        print("失败：", exc)
        return 2
    s = report["account_summary"]
    print(f"完成。交易币种：{', '.join(report['traded_symbols']) or '无'}")
    print(f"已实现盈亏 {s['realized_pnl_usdt']}  手续费 {s['commission_usdt']}  资金费 {s['funding_usdt']}  "
          f"净额 {s['trading_net_usdt_from_income']} USDT")
    if report["issues"]:
        print("注意：", ", ".join(report["issues"]))
    print("报告：", path)
    print("标注表（可在 Excel 里填评级/止盈步数）：", ann)
    print(f"共发出 {report['request_count']} 个只读请求。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
