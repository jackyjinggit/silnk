#!/bin/bash
# Linux / Docker 启动脚本。
# setting.py 本身优先读环境变量，这里不再重写配置文件，
# 避免依赖可能不存在的 setting.py.bak。

check_empty_env() {
  local empty_vars=()

  [[ -z "$SERVER_HOST" ]] && empty_vars+=("SERVER_HOST")
  [[ -z "$PRINTER_URL" ]] && empty_vars+=("PRINTER_URL")
  [[ -z "$SECRET_KEY" ]] && empty_vars+=("SECRET_KEY")
  if [ ${#empty_vars[@]} -gt 0 ]; then
    echo "警告: 以下必需的环境变量为空或未设置:"
    for var in "${empty_vars[@]}"; do
      echo "  - $var"
    done
    echo "请设置以上环境变量后再启动"
  fi
}

check_empty_env

# 容器/工控机上启动 CUPS，供 ipptool 走 ipp://localhost:631/printers/<队列名>
/usr/sbin/cupsd -f &
if [[ -n "$PRINTER_URL" ]]; then
  python3 main.py &
fi

wait
