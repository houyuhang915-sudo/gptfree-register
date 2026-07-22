"""
GPT Pay Pipeline - 配置文件

敏感字段（接码号、代理凭据、API key、真实卡）默认从环境变量读取，
也可以本地写一个 `config.local.py` 覆盖（已在 .gitignore 中）。
public 备份只保留结构和占位符。
"""

import os

# ========== 运行模式 ==========
# "register" = 自动注册新账号 (需要 Outlook 池)
# "login"    = 登录已有账号 (需要 accounts.txt)
MODE = "register"

# ========== 订阅计划 ==========
PLAN_NAME = "chatgptplus"           # chatgptplus / chatgptteamplan
CHECKOUT_COUNTRY = "US"             # US / JP / BR / DE / ID / 其他（JP/BR 等会驱动对应 billing/phone）
CHECKOUT_CURRENCY = "USD"           # USD / JPY / BRL / EUR / IDR ... 留空时按 country 自动推断

# ========== 账单地址 ==========
# 控制 fill_hosted_checkout 时的账单国家：
#   "auto"   = 跟 CHECKOUT_COUNTRY 走（默认）
#   "US"/"JP"/...= 强制覆盖
BILLING_COUNTRY = "auto"

# meiguodizhi /<cc>-address 拉真实地址；False 时只用本地 LOCAL_SEEDS（参考
# address_provider.py）。注册阶段不影响，只在支付填账单那一步生效。
USE_MEIGUODIZHI = True

# ========== 账号文件 (login 模式) ==========
ACCOUNTS_FILE = "accounts.txt"      # 格式: 邮箱----密码  (每行一个)

# ========== Outlook 邮箱池 (register 模式) ==========
OUTLOOK_ACCOUNTS_FILE = "outlook_accounts.txt"  # 格式: email----password----client_id----refresh_token
OTP_TIMEOUT = 240                   # OTP 拉取超时(秒)
OTP_METHOD = "graph"                # graph = Microsoft Graph API / imap = IMAP XOAUTH2

# ========== 并发 ==========
MAX_WORKERS = 1                     # 并发线程数
THREAD_START_DELAY = 2              # 线程启动间隔(秒)

# ========== 浏览器 ==========
BROWSER_MODE = "local"              # local = 本地 Chrome / bitbrowser = 比特指纹浏览器
CHROME_PATH = "/Applications/Chromium.app/Contents/MacOS/Chromium"  # Chromium 路径
USER_DATA_DIR = ""                  # Chrome 用户数据目录，留空用临时目录

# ========== 比特指纹浏览器（支付阶段用） ==========
BITBROWSER_API = "http://127.0.0.1:54345"
USE_BITBROWSER_FOR_PAY = False
# BitBrowser 出口代理（占位；真值放 config.local.py）
BITBROWSER_PROXY = {
    "proxyType": "socks5",
    "host": os.environ.get("PROXY_HOST", "proxy.example.invalid"),
    "port": int(os.environ.get("PROXY_PORT", "10000")),
    "user": os.environ.get("PROXY_USER", "PROXY_USER_PLACEHOLDER"),
    "password": os.environ.get("PROXY_PASS", "PROXY_PASS_PLACEHOLDER"),
}
# 模板 profile（ephemeral 模式不再需要，保留向后兼容）
BITBROWSER_TEMPLATE_PROFILE_ID = os.environ.get("BITBROWSER_TEMPLATE_PROFILE_ID", "")
# 指纹模板：auto/desktop、iphone、android。国家化 PayPal 流程可按目标地区选择。
BITBROWSER_FINGERPRINT_PROFILE = os.environ.get("BITBROWSER_FINGERPRINT_PROFILE", "auto")

# ========== 代理配置（BitBrowser 支付阶段 + 可选注册阶段） ==========
PROXY_ENABLED = False               # 注册阶段是否走代理（默认不走，BitBrowser 自带代理）
PROXY_HOST = os.environ.get("PROXY_HOST", "proxy.example.invalid")
PROXY_PORT = int(os.environ.get("PROXY_PORT", "10000"))
PROXY_USER = os.environ.get("PROXY_USER", "PROXY_USER_PLACEHOLDER")
PROXY_PASS = os.environ.get("PROXY_PASS", "PROXY_PASS_PLACEHOLDER")

# ========== PayPal 配置 ==========
# 全局 PayPal 表单电话（接码号），所有 worker 共享，文件锁串行 SMS。
# 真值通过环境变量或 config.local.py 注入。
PAYPAL_PHONE = os.environ.get("PAYPAL_PHONE", "+10000000000")
PAYPAL_SMS_URL = os.environ.get(
    "PAYPAL_SMS_URL",
    "http://example.invalid/api/get_sms?key=PLACEHOLDER",
)

# ========== SMS 接码 provider ==========
# "62us"     = 默认。固定号码 + URL（兼容 a.62-us.com 这一类预购号方案，
#              号码靠 PAYPAL_SMS_URL / 卡里的 sms_url，PayPal 表单 phone 用
#              PAYPAL_PHONE 或卡里的 phone 字段）。
# "smsbower" = sms-activate 协议（smsbower.app），用 apikey 自动取号 → 等码 → 还号。
# 同协议平台同一套：smshub / tiger-sms / smsverified / hero-sms / onlinesim。
SMS_PROVIDER = os.environ.get("SMS_PROVIDER", "62us")

# smsbower / sms-activate 协议平台配置（SMS_PROVIDER != "62us" 时生效）
SMSBOWER_API_KEY = os.environ.get("SMSBOWER_API_KEY", "")
SMSBOWER_BASE_URL = os.environ.get("SMSBOWER_BASE_URL", "")  # 留空走默认 smsbower.app
SMSBOWER_SERVICE = os.environ.get("SMSBOWER_SERVICE", "pp")   # PayPal: US 常见 pp；SMSBower 前端 Brazil PayPal 是 ts
SMSBOWER_COUNTRY = int(os.environ.get("SMSBOWER_COUNTRY", "12"))   # 12=US virtual；SMSBower 前端 Brazil 的 API country 是 73
SMSBOWER_MAX_PRICE = os.environ.get("SMSBOWER_MAX_PRICE", "")     # 留空 = 不限价；SMSBower 会映射为 fixPrice
SMSBOWER_OPERATOR = os.environ.get("SMSBOWER_OPERATOR", "")        # 留空 = 任意运营商
SMSBOWER_USE_V2 = os.environ.get("SMSBOWER_USE_V2", "1")           # 1=优先 getNumberV2(JSON)；指定价格/供应商时自动优先 getNumber
SMSBOWER_PROVIDER_IDS = os.environ.get("SMSBOWER_PROVIDER_IDS", "")              # 可选；SMSBower 会映射为 priorityProviderIds
SMSBOWER_EXCEPT_PROVIDER_IDS = os.environ.get("SMSBOWER_EXCEPT_PROVIDER_IDS", "") # 可选：排除 providerIds
SMSBOWER_PHONE_EXCEPTION = os.environ.get("SMSBOWER_PHONE_EXCEPTION", "")        # 可选：排除号段/号码
SMSBOWER_REF = os.environ.get("SMSBOWER_REF", "")                                # 可选：ref 参数

# Free/OpenAI 绑号使用独立号池。除 service 默认 dr 外，留空项会在 provider
# 工厂中回退到上面的全局 SMS/SmsBower 配置。
FREE_SMS_PROVIDER = os.environ.get("FREE_SMS_PROVIDER", "")
FREE_SMSBOWER_API_KEY = os.environ.get("FREE_SMSBOWER_API_KEY", "")
FREE_SMSBOWER_BASE_URL = os.environ.get("FREE_SMSBOWER_BASE_URL", "")
FREE_SMSBOWER_SERVICE = os.environ.get("FREE_SMSBOWER_SERVICE", "dr")
FREE_SMSBOWER_COUNTRY = os.environ.get("FREE_SMSBOWER_COUNTRY", "")
FREE_SMSBOWER_MAX_PRICE = os.environ.get("FREE_SMSBOWER_MAX_PRICE", "")
FREE_SMSBOWER_OPERATOR = os.environ.get("FREE_SMSBOWER_OPERATOR", "")
FREE_SMSBOWER_USE_V2 = os.environ.get("FREE_SMSBOWER_USE_V2", "")
FREE_SMSBOWER_PROVIDER_IDS = os.environ.get("FREE_SMSBOWER_PROVIDER_IDS", "")
FREE_SMSBOWER_EXCEPT_PROVIDER_IDS = os.environ.get("FREE_SMSBOWER_EXCEPT_PROVIDER_IDS", "")
FREE_SMSBOWER_PHONE_EXCEPTION = os.environ.get("FREE_SMSBOWER_PHONE_EXCEPTION", "")
FREE_SMSBOWER_REF = os.environ.get("FREE_SMSBOWER_REF", "")

# ========== Sub2API 网关 ==========
# 工作台「账号管理 → Sub2API 配置」会把本地值写入 config.local.py。
GATEWAY_SUB2API_URL = os.environ.get("GATEWAY_SUB2API_URL", "")
GATEWAY_SUB2API_TOKEN = os.environ.get("GATEWAY_SUB2API_TOKEN", "")
GATEWAY_SUB2API_PATH = os.environ.get("GATEWAY_SUB2API_PATH", "/api/admin/import")
GATEWAY_SUB2API_AGENT_PATH = os.environ.get(
    "GATEWAY_SUB2API_AGENT_PATH",
    "/api/v1/admin/accounts/import/codex-session",
)
GATEWAY_SUB2API_GROUP_IDS = os.environ.get("GATEWAY_SUB2API_GROUP_IDS", "2")

# 卡池（默认空，运行时由 cards.txt + card_pool.py 加载）
# 这里保留几张占位卡仅用于代码不引用 cards.txt 时的兜底（单跑模式）。
CARD_POOL = [
    {
        "id": "card_placeholder_1",
        "cardNumber": "4111111111111111",
        "cardExpiry": "07/30",
        "cardCvv": "123",
        "first_name": "JANE",
        "last_name": "DOE",
        "cardholder_phone": "+10000000000",
        "address": {
            "street": "123 Example St",
            "city": "Sample City",
            "state": "ND",
            "zip": "00000",
        },
    },
]

# 旧的单卡配置（向后兼容；单跑模式默认用 CARD_POOL[0]）
PAYPAL_CARD_NUMBER = CARD_POOL[0]["cardNumber"]
PAYPAL_CARD_EXPIRY = CARD_POOL[0]["cardExpiry"]
PAYPAL_CARD_CVV = CARD_POOL[0]["cardCvv"]
PAYPAL_FIRST_NAME = CARD_POOL[0]["first_name"]
PAYPAL_LAST_NAME = CARD_POOL[0]["last_name"]
PAYPAL_CARD_PHONE = CARD_POOL[0]["cardholder_phone"]   # 持卡人电话（登记用）
PAYPAL_CARD_SMS_URL = ""                               # 持卡人电话不用接码
# 卡 BIN 覆盖：auto/visa 使用原 BIN，jcb/mc 使用内置 BIN 池。
PAYPAL_CARD_BIN_BRAND = os.environ.get("PAYPAL_CARD_BIN_BRAND", "auto")

# ========== 账单地址（默认；并发时每张卡有自己的）==========
BILLING_ADDRESS = CARD_POOL[0]["address"]

# ========== YesCaptcha (可选) ==========
CAPTCHA_API_URL = "https://api.yescaptcha.com"
CAPTCHA_API_KEY = os.environ.get("CAPTCHA_API_KEY", "")

# ========== 调试 ==========
DEBUG = True


# ============================================================
# 本地覆盖：如果存在 config.local.py，把里面定义的同名变量覆盖到本模块。
# config.local.py 已在 .gitignore 中，用来放真实凭据。
# ============================================================
try:
    from config_local import *  # type: ignore  # noqa: F401,F403
except ImportError:
    try:
        # 兼容文件命名 config.local.py（不是合法包名，只能动态加载）
        import importlib.util as _ilu
        import pathlib as _pl
        _local = _pl.Path(__file__).with_name("config.local.py")
        if _local.exists():
            _spec = _ilu.spec_from_file_location("_config_local", _local)
            if _spec and _spec.loader:
                _mod = _ilu.module_from_spec(_spec)
                _spec.loader.exec_module(_mod)
                for _k in dir(_mod):
                    if not _k.startswith("_"):
                        globals()[_k] = getattr(_mod, _k)
    except Exception:
        pass
