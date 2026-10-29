# trading-review-kit：币安只读导出 + Telegram 群信号监听

把币安 U 本位合约账户（成交、盈亏、持仓、挂单、止盈止损单）每 15 分钟导出一次，把一个 Telegram 群的消息实时采集下来，都写进你自己的 GitHub 私有仓库。之后在 Claude / GPT 里说「看看我的仓位」「现在有没有新单子」，它们直接读仓库，不用再截图、复制粘贴。

- 全程只读：币安 Key 只开「允许读取」并绑定 VPS 的 IP；程序不下单，也不在 Telegram 发消息。
- 所有密钥只放在你的 VPS 上（权限 600），不进聊天、不进仓库。
- 只用 Python 自带库 + telethon，后台运行不调用任何 AI。

**完整的分步教程见《币安只读导出 + Telegram 信号监听：搭建方法集》**（向分享这个仓库的人要）。下面只列文件和最少的命令。

下载：点本页右上方绿色的 **Code → Download ZIP**，或在 VPS 上 `git clone` 本仓库到 `~/app`。

## 文件

| 文件 | 作用 |
| --- | --- |
| `auto_run.py` | 定时任务入口：导出今天（顺带补前 7 天缺的）/ 08:30 补全昨天；监听启用后顺带做健康检查 |
| `daily_export.py`、`account_wide.py`、`binance_*.py`、`review_core.py` | 币安只读读取和每日报告生成（运行前先检查 Key 只读 + 有 IP 白名单，否则拒绝） |
| `github_upload.py` | 只往仓库的 `data/`、`status/`、`signals/` 写文件 |
| `secure_store.py` | 存密钥：Windows 用 DPAPI 加密；Linux 用权限 600 的文件，权限被放宽就拒绝启动 |
| `setup_auto.py` | 一次性设置：隐藏输入币安 Key / Secret、GitHub 令牌，验证后保存 |
| `tg_login.py` | 在 VPS 上登录 Telegram（首次会问 API ID / Hash），生成新的 session |
| `tg_collect.py` | Telegram 消息的存储格式和增量采集（没装监听时由定时任务调用） |
| `signal_listener.py` + `signal-listener.service` | 常驻实时监听（systemd），群里一发消息就写入，约 1 分钟内上传 |
| `push_secrets.py` | 可选：把 Windows 电脑上已保存的密钥经 SSH 传到 VPS，不显示内容 |
| `run_daily_export.*`、`backfill.bat`、`setup_auto.bat` | Windows 上手动运行用 |
| `test_*.py` | 离线测试（假数据，不联网） |

## 用之前必须改的两处

1. `tg_collect.py` 里的 `CHAT_ID`：改成你要监听的群 ID（负数，形如 `-100xxxxxxxxxx`）。不改的话监听会直接退出并提示。
2. `setup_auto.py` 里的 `DEFAULT_REPO`：改成你的 `GitHub 用户名/仓库名`（运行时也可以直接输入）。

## 最少的命令（VPS：Ubuntu 24.04，用户 `tr`）

```bash
# 1. 代码放到 ~/app，先跑测试（最后一行应为 OK；监听的推送测试会显示 skipped）
cd ~/app && python3 -B -m unittest test_automation test_daily_export test_review test_tg_collect test_signal_listener

# 2. Telegram 库
python3 -m venv ~/venv && ~/venv/bin/pip install telethon==1.45.0

# 3. 币安 Key + GitHub 令牌（隐藏输入）
python3 -B setup_auto.py

# 4. 手动导出一次，看最后一行是否 "ok": true
~/venv/bin/python -B auto_run.py --mode tonight; tail -c 400 ~/trading-review/auto_run.log

# 5. 定时任务
printf '%s\n' '*/15 * * * * cd ~/app && flock -n /tmp/tr-export.lock ~/venv/bin/python -B auto_run.py --mode tonight' \
  '30 8 * * * cd ~/app && flock -w 900 /tmp/tr-export.lock ~/venv/bin/python -B auto_run.py --mode catchup' | crontab -

# 6. Telegram 登录（在你自己电脑的终端窗口里交互输入：手机号、验证码、两步验证密码，每次按回车）
~/venv/bin/python -B tg_login.py

# 7. 常驻监听（root）
cp /home/tr/app/signal-listener.service /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now signal-listener
```

监听掉线提醒用的是 Telegram 机器人：把机器人 token 放在 `~/sig/config/telegram_bot.token`，chat id 放在 `~/sig/config/telegram_chat_id`（目录 700，文件 600），并把发消息的 `notify.py` 放在 `~/sig/tool/`。不配置也能正常采集，只是没有掉线提醒。

## 可选：往手机推送带标注的信号

`signal_listener.py --push` 会给每条新信号做评级标注再推送到机器人。它需要你自己的信号解析脚本（`signal_features`、`v5_tags`、`signal_stats`、`rules_research`），放在 `~/sig/tool/`，这个包不包含它们：信号格式和评级规则因群、因人而异。没有这些脚本时，监听照常采集上传，只是不推送。

## 注意

- 只监听你自己已经加入的群，采集到的内容只给自己分析用；付费群的信号不要转发或公开，仓库保持私有。
- 评级和分析只是参考，所有下单都由你自己决定、自己操作。
