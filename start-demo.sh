#!/bin/bash
# 现场演示一键启动脚本（提交 10）
#
# 目标：评委面前一条命令起全套 —— 后端服务 + 取件屏大屏页。
# 设计原则：
#   1. 不依赖任何环境变量默认值（缺什么就明说，不静默用假值）
#   2. 起不来要立刻报错并打印排查提示，不留半死不活的后台进程
#   3. 取件屏默认用浏览器大屏兜底档（L1）；T5 屏固件可用时另见 docs/现场部署手册.md
#
# 用法：
#   ./start-demo.sh                # 起后端 + 打印大屏页地址
#   ./start-demo.sh --port 8000    # 指定端口
#   DEVICE=dev-ab12 ./start-demo.sh  # 指定要展示的设备

set -u

PORT=8000
DEVICE=""
HOST="127.0.0.1"
PYTHON="python3"

while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --device) DEVICE="$2"; shift 2 ;;
    --host) HOST="$2"; shift 2 ;;
    --python) PYTHON="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "未知参数：$1"; exit 1 ;;
  esac
done

cd "$(dirname "$0")" || exit 1

echo "=============================================="
echo " 思印客 Silnk · 现场演示启动"
echo "=============================================="

# ---- 1. 前置检查 ----
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "[×] 找不到 $PYTHON"
  echo "    装依赖或用 --python 指定解释器，例如："
  echo "    ./start-demo.sh --python /path/to/venv/bin/python"
  exit 1
fi

"$PYTHON" - <<'PYCHECK'
import importlib, sys
missing = []
for mod in ("fastapi", "uvicorn"):
    try:
        importlib.import_module(mod)
    except ImportError:
        missing.append(mod)
if missing:
    print("[×] 缺依赖：" + ", ".join(missing))
    print("    pip install -r requirements.txt")
    sys.exit(1)
print("[√] 依赖齐全")
PYCHECK
[ $? -ne 0 ] && exit 1

# ---- 2. 端口占用检查（现场最容易踩的坑）----
if command -v lsof >/dev/null 2>&1 && lsof -i ":$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "[×] 端口 $PORT 已被占用"
  echo "    换端口：./start-demo.sh --port $((PORT+1))"
  echo "    或杀掉占用进程后重跑"
  exit 1
fi

# ---- 3. 起后端 ----
echo "[·] 启动后端  http://$HOST:$PORT"
SECRET_KEY="${SECRET_KEY:-}" \
"$PYTHON" -m uvicorn main:app --host "$HOST" --port "$PORT" &
SERVER_PID=$!

# 等后端真的起来（不等够时间就报地址 = 现场手忙脚乱）
READY=0
for i in $(seq 1 30); do
  if curl -fsS "http://$HOST:$PORT/healthz" >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 1
done

if [ "$READY" -ne 1 ]; then
  echo "[×] 后端 30 秒内没起来，日志："
  tail -20 /tmp/silnk-demo.log 2>/dev/null || true
  kill "$SERVER_PID" 2>/dev/null
  exit 1
fi
echo "[√] 后端已就绪"

# ---- 4. 挑一台设备（没有就列出所有）----
if [ -z "$DEVICE" ]; then
  DEVICE=$("$PYTHON" - <<'PYDEV'
import db
try:
    db.init_db()
    devs = [d.get("device_id") for d in db.list_devices() if d.get("device_id")]
    print(devs[0] if devs else "")
except Exception:
    print("")
PYDEV
)
fi

# ---- 5. 打印使用说明 ----
echo ""
echo "=============================================="
echo " [√] 全部就绪"
echo ""
echo "  管理台        http://$HOST:$PORT/admin/display.html"
if [ -n "$DEVICE" ]; then
  echo "  取件屏大屏    http://$HOST:$PORT/display?device_id=$DEVICE"
else
  echo "  取件屏大屏    http://$HOST:$PORT/display?device_id=<设备ID>"
  echo "                （设备列表见 /admin/display.html）"
fi
echo "  接口自检      curl -H \"Authorization: Bearer \$SECRET_KEY\" \\"
echo "                    http://$HOST:$PORT/api/display/<设备ID>/state"
echo ""
echo "  Ctrl+C 停止"
echo "=============================================="
echo ""

trap 'echo ""; echo "[·] 停止后端…"; kill "$SERVER_PID" 2>/dev/null; exit 0' INT TERM
wait "$SERVER_PID"
