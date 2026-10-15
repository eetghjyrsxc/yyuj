#!/data/data/com.termux/files/usr/bin/bash
# 手机桥 · 手机端启动脚本（拷进 Termux 当 ~/bridge.sh 用）
# ============================================================================
# v2：**一个服务干两件事**（对应沙箱里两个入口，用的是同一个端口）
#   ① 只读文件服务        → 沙箱侧 `运维工具/手机桥/手机文件.sh`（照旧能用）
#   ② 白名单执行层        → 沙箱侧 `运维工具/手机桥/手机控制.sh`
#      写文件 / 弹通知 / 打开网址或文件 / 拍照 / 让手机挑文件 / 跑 termux-* 命令
#
# 用法（Termux 里）：
#   bash ~/bridge.sh                   默认 8899 端口、根目录 /storage/emulated/0
#   bash ~/bridge.sh 9000              换端口（沙箱侧要同步改 PHONE_BRIDGE_URL）
#   bash ~/bridge.sh 8899 /storage/emulated/0
#   bash ~/bridge.sh 覆盖              允许覆盖同名文件（默认只新建、同名停手）
#   bash ~/bridge.sh 危险              放开短信/通讯录/拨号（默认关闭）
#   bash ~/bridge.sh 只读              退回 v1 行为（原生 http.server，只读；应急用）
#
# 前置：python（pkg install python）
#       要用执行层还得装 Termux:API：App（F-Droid 或 GitHub 版，务必与 Termux 同源）
#       + `pkg install termux-api`。没装也照样能跑，只是只剩「读文件」。
#
# 安全：只绑 127.0.0.1（同 WiFi 的别人访问不到）；写只进白名单目录；只新建不删改。
# 详细说明与故障排查 → 工作区 `运维工具/手机桥/手机桥指南.md`
# 重复执行安全：会先停掉旧的桥进程，不会撞 Address already in use
# ============================================================================

set -u

PORT=8899
ROOT="/storage/emulated/0"
MODE="v2"
EXTRA=""

for a in "$@"; do
    case "$a" in
        只读) MODE="ro" ;;
        覆盖) EXTRA="$EXTRA --overwrite" ;;
        危险) EXTRA="$EXTRA --allow-risky" ;;
        [0-9]*) PORT="$a" ;;
        /*) ROOT="$a" ;;
        *) echo "看不懂的参数：$a（跑 bash ~/bridge.sh 看用法）" >&2; exit 2 ;;
    esac
done

command -v python >/dev/null 2>&1 || { echo "python not found. 请先执行: pkg install -y python"; exit 1; }

# —— 清掉残留的旧桥进程（只认自己的两种服务，不碰别的进程）——
python - <<'PY' 2>/dev/null
import glob, os, signal
me = os.getpid(); killed = []
for path in glob.glob("/proc/[0-9]*/cmdline"):
    try:
        pid = int(path.split("/")[2])
        if pid == me:
            continue
        parts = open(path, "rb").read().split(b"\0")
    except Exception:
        continue
    joined = b" ".join(parts)
    if b"http.server" in joined or b"phone_server.py" in joined:
        try:
            os.kill(pid, signal.SIGKILL)
            killed.append(str(pid))
        except Exception:
            pass
if killed:
    print("stopped old bridge: PID " + ", ".join(killed))
PY
sleep 0.3

# 唤醒锁：Termux:API 装了才有这个命令，没有就跳过（服务照跑，只是更容易被系统掐）
termux-wake-lock 2>/dev/null || echo "(termux-wake-lock 不可用，跳过；装了 Termux:API 才有)"

if [ "$MODE" = "ro" ]; then
    echo
    echo "  手机桥 · v1 只读模式（应急）"
    echo "  ├─ 服务目录：$ROOT"
    echo "  ├─ 监听地址：127.0.0.1:$PORT"
    echo "  └─ 只能读文件，没有执行层"
    echo
    exec python -m http.server "$PORT" --bind 127.0.0.1 --directory "$ROOT"
fi

SRV="${BRIDGE_PY:-$HOME/phone_server.py}"
if [ ! -f "$SRV" ]; then
    echo "✗ 找不到 $SRV" >&2
    echo "  手机端服务脚本要先放进 Termux 主目录（见 运维工具/手机桥/手机桥指南.md）," >&2
    echo "  或者先应急用只读模式： bash ~/bridge.sh 只读" >&2
    exit 1
fi

# shellcheck disable=SC2086
exec python "$SRV" --port "$PORT" --root "$ROOT" $EXTRA
