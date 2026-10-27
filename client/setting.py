"""打印机端配置。

配置来源优先级：环境变量 > 同目录 config.json > 下面的默认值。
config.json 由零配置接入向导（setup_gui.py）写入，正常部署不需要手改脚本。

Windows 主机额外需要：
    PRINTER_NAME           控制面板里打印机队列的名字，例如 "HP Smart Tank 518 series"
    SUMATRAPDF_PATH        SumatraPDF.exe 路径（不设则从 PATH 和默认安装目录查找）
    PRINTER_FORCE_SIMPLEX  设为 1 时强制单面（手动双面打印机的无人值守场景建议开）
"""

import json
import os

_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_DIR, "config.json")


def load_config():
    """读取零配置接入生成的 config.json；不存在或损坏时返回空字典。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


_CONFIG = load_config()

SERVER_HOST = os.getenv('SERVER_HOST') or _CONFIG.get('server_host', '')
PRINTER_URL = os.getenv('PRINTER_URL') or _CONFIG.get('printer_url', '')
# 老部署用 SECRET_KEY；零配置接入后这里是本机专属的设备令牌
SECRET_KEY = os.getenv('SECRET_KEY') or _CONFIG.get('token', '')

# main.py 在 import setting 之后才读这些环境变量，这里把 config.json 的值补进去，
# 客户端代码因此完全不用改：插电即用，不再手工设 PRINTER_DEVICE_ID / TENANT_ID。
_ENV_FROM_CONFIG = (
    ("PRINTER_DEVICE_ID", _CONFIG.get("device_id")),
    ("TENANT_ID", _CONFIG.get("tenant_id")),
    ("PRINTER_NAME", _CONFIG.get("printer_name")),
    ("PRINTER_DISPLAY_NAME", _CONFIG.get("printer_display_name")),
    ("PRINTER_PAPER_CAPACITY", _CONFIG.get("paper_capacity")),
)

for _env_name, _value in _ENV_FROM_CONFIG:
    if _value not in (None, "") and not os.environ.get(_env_name):
        os.environ[_env_name] = str(_value)
