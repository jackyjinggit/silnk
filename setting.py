import os
import sys

# 跨域白名单：默认空 = 不开启跨域（前后端同源部署不需要 CORS）。
# 需要跨域时用环境变量显式列出，例：ALLOWED_ORIGINS=https://a.com,https://b.com
# 绝不使用 ['*']：通配符与 allow_credentials 组合既不安全也不符合规范。
ORIGIN = [o.strip() for o in os.getenv('ALLOWED_ORIGINS', '').split(',') if o.strip()]

# 密钥优先取环境变量 SECRET_KEY，正式部署必须设置（例如 systemd 的 Environment=SECRET_KEY=...）。
# 未设置时退回本地开发值：该兜底值仅供本地开发，绝不可用于公网部署。
_LOCAL_SECRET_KEY = 'local-dev-secret-key-0123456789abcdef0123456789'
SECRET_KEY = os.getenv('SECRET_KEY', _LOCAL_SECRET_KEY)

# 运维密钥：与商家密钥分开。运维只能看设备/告警、打印测试页、重启客户端，
# 不能改价格、看账单、确认收款。正式部署必须设置环境变量 OPS_KEY；
# 未设置时（且不是本地兜底密钥）运维登录直接关闭，避免用公开的默认值进生产。
OPS_KEY = os.getenv(
    'OPS_KEY',
    'local-dev-ops-key-0123456789abcdef0123456789' if SECRET_KEY == _LOCAL_SECRET_KEY else ''
)
ALGORITHM = 'HS256'  # JWT加密算法，没特殊需求不用改
SERVER_HOST = os.getenv('SERVER_HOST', '')  # 支付回调地址，这里填你的后端地址，如 https://www.printer-backend.com:8000
APP_ID = os.getenv('APP_ID', '')  # 支付宝当面付签约ID
ALIPAY_PUBLIC_KEY = os.getenv('ALIPAY_PUBLIC_KEY', '')  # 支付宝当面付公钥
ALIPAY_PRIVATE_KEY = os.getenv('ALIPAY_PRIVATE_KEY', '')  # 支付宝当面付私钥

# 企微（企业微信）群机器人 Webhook，用于告警推送（R1~R5 触发时发文本消息）。
# 为空即不推送，管理端「接入状态」页会如实标注为「待配置」，不会假装已推送。
# 例：https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx-xxxx-xxxx
WECOM_WEBHOOK_URL = os.getenv('WECOM_WEBHOOK_URL', '')

# 收款方式：先用「个人微信收款码」收款（无需商户号）。
# 收款码图片放在 static/pay/ 下，由 main.py 以 /pay-qr 挂载对外提供；
# 这里只声明默认的图片路径与收款人昵称，管理端「价格对比」页可覆盖（存数据库 pay_config 表）。
WECHAT_QR_URL = os.getenv('WECHAT_QR_URL', '/pay-qr/wechat_qr.png')
WECHAT_PAYEE = os.getenv('WECHAT_PAYEE', '')

# 使用什么软件来读取 word：Windows 用 MSOffice，Linux 用 LibreOffice（headless，无需图形界面）。
# 默认按当前平台推断，避免在 Linux 上错误地选到依赖 win32com 的 MSOffice；
# 仍可用环境变量 CONVERT_TOOL 覆盖。
_DEFAULT_CONVERT_TOOL = 'MSOffice' if sys.platform.startswith('win') else 'LibreOffice'
CONVERT_TOOL = os.getenv('CONVERT_TOOL', _DEFAULT_CONVERT_TOOL)

# ----------------------- 用户账号 / 短信验证码 -----------------------
# 真实短信服务商尚未接入：SMS_PROVIDER 为空时，发送验证码接口会如实返回
# 「短信服务未接入」，绝不假装发送成功（接入任务见 /team 看板）。
# 预留：aliyun / tencent。
SMS_PROVIDER = os.getenv('SMS_PROVIDER', '')
# 短信通道：dysms = 短信服务（需正式签名）；dypns = 号码认证服务（可用测试签名）
SMS_CHANNEL = os.getenv('SMS_CHANNEL', 'dysms')
SMS_ACCESS_KEY = os.getenv('SMS_ACCESS_KEY', '')
SMS_ACCESS_SECRET = os.getenv('SMS_ACCESS_SECRET', '')
SMS_SIGN_NAME = os.getenv('SMS_SIGN_NAME', '')
SMS_TEMPLATE_CODE = os.getenv('SMS_TEMPLATE_CODE', '')
# 演示模式：仅用于本地/黑客松演示。开启后验证码随响应返回，并在页面明确标注
# 「演示验证码，未接入短信服务」；生产环境必须保持关闭。
SMS_DEMO_MODE = os.getenv('SMS_DEMO_MODE', '0').strip().lower() in (
    '1', 'true', 'yes', 'on')
# 登录 token 有效期（天）；上传会话仍由 uploads 目录的 15 分钟过期控制
USER_TOKEN_DAYS = int(os.getenv('USER_TOKEN_DAYS', '7'))

# ----------------------- 印前内容审核（智谱 GLM） -----------------------
# 未配置 Key 时不送审，接口如实返回「未接入」，不假装审核通过。
GLM_API_KEY = os.getenv('GLM_API_KEY', '')
GLM_MODEL = os.getenv('GLM_MODEL', 'glm-5.3-flash')
GLM_URL = os.getenv('GLM_URL', 'https://open.bigmodel.cn/api/paas/v4/chat/completions')
# 每份文档审核前 N 页，控制耗时与费用
GLM_REVIEW_PAGES = int(os.getenv('GLM_REVIEW_PAGES', '5'))
# 高风险是否直接禁止下单：默认关闭（只提示，由管理员判断）
GLM_BLOCK_ON_RISK = os.getenv('GLM_BLOCK_ON_RISK', '0').strip().lower() in (
    '1', 'true', 'yes', 'on')

# ----------------------- 共享资料库 agent 初审（DeepSeek） -----------------------
# 优先使用 DeepSeek 做文本审核（资料库共享的成品 PDF 提取文本层后送审）；
# 未配置 DEEPSEEK_API_KEY 时回退到 GLM 视觉审核；两者都未配置则如实转人工。
# 密钥只从环境变量读取，绝不写入仓库或文档。
DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY', '')
DEEPSEEK_MODEL = os.getenv('DEEPSEEK_MODEL', 'deepseek-flash')
DEEPSEEK_URL = os.getenv('DEEPSEEK_URL', 'https://api.deepseek.com/chat/completions')
DEEPSEEK_TIMEOUT = int(os.getenv('DEEPSEEK_TIMEOUT', '60'))
# 单份资料最多送审的文本字符数，避免超长文档把请求拖死
DEEPSEEK_MAX_CHARS = int(os.getenv('DEEPSEEK_MAX_CHARS', '12000'))
