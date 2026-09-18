"""Constants for the AirCloud (合宙 IoT) integration."""

DOMAIN = "aircloud"

API_HOST = "https://api-iot.luatos.com"
API_BASE = f"{API_HOST}/iot/open_api"

CONF_TOKEN = "token"
CONF_SALT = "salt"
CONF_SID = "sid"
CONF_PUBLIC_KEY = "public_key"
CONF_APP_ID = "app_id"
CONF_PROJECT = "project"
CONF_DEVICES = "devices"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_PHONE = "phone"
CONF_PASSWORD = "password"
CONF_CAPTCHA = "captcha"
CONF_OCR_PROVIDER = "ocr_provider"

# --- 验证码自动识别（通过 HA 的 LLM Vision 集成） ---
# 实测单次识别准确率 83%（原图 160x80，12 次真实校验）；失败可换图重试，
# 3 次重试累计成功率 99.5%。放大图片反而降到 58%，故直接用原图。
OCR_MAX_ATTEMPTS = 3
OCR_PROMPT = (
    "这是一张包含 4 个字符的图形验证码图片。请仔细辨认每个字符。"
    "只输出这 4 个字符本身，不要标点、空格或任何解释。字符区分大小写。"
)
OCR_DIR_NAME = "aircloud_cap"
# 「不自动识别」的下拉值（config_flow 与自动登录共用）
OCR_MANUAL = "__manual__"

# --- 登录态保活（账号密码 + 识别模型会被保存，失效后自动重新登录）---
# 平台是单会话：别处登录会让本集成的 token 立刻失效。只要保存了
# 手机号 + 密码 + 识别模型，集成就会自己重新登录，不需要用户介入。
# 失败则每 RELOGIN_RETRY_S 秒重试一次（验证码识别偶发抖动、模型限流等都会自愈）。
RELOGIN_RETRY_S = 600          # 重试间隔：10 分钟
RELOGIN_FIRST_DELAY_S = 15     # 首次检测到失效后的等待，避开与其它登录撞车
# 依赖未就绪（HA 启动顺序问题）时的短重试间隔：不要按 10 分钟等
RELOGIN_NOTREADY_RETRY_S = 5
# 首次 setup 时等待依赖就绪的最长时间（秒）；超时则交给 HA 的 setup 重试
RELOGIN_SETUP_WAIT_S = 90
RELOGIN_LOG_AFTER = 3          # 前 N 次失败按 warning 记录，之后降为 info

DEFAULT_APP_ID = "move"
DEFAULT_SCAN_INTERVAL = 15
DEFAULT_TRACK_WINDOW = 7          # 每次回溯天数窗口（配合去重，保证不丢点）

# --- 展示名称（集成名 / 设备名，中文优先） ---
DEFAULT_NAME = "合宙 IoT"          # 集成与配置条目标题
DEVICE_NAME = "合宙 IoT"
DEVICE_MANUFACTURER = "合宙（LuatOS）"
DEVICE_MODEL = "IoT 定位设备"

PLATFORMS = ["device_tracker", "sensor", "binary_sensor"]

# 平台查询频率闸门（实测）：latest_location 1s 一次约 50% 返回 429；
# 而 location_history 实测 1s 一次 6/6 成功、15s 轮询 8/8 成功，且能取回
# 移动期的全部 5s 级点位 —— 因此状态刷新走 location_history，不走 latest_location。
RATE_LIMIT_CODE = 429
AUTH_FAIL_CODES = {102, 103, 105}

# --- Tag 表（官方清单，禁止编造；见 luatos-aircloud-webapp 技能） ---
TAG_LNG = 512          # 经度（val_ 形式为 GCJ02）
TAG_LAT = 513          # 纬度
TAG_VBAT = 799         # 电压 mV
TAG_SIGNAL = 782       # 4G 信号
TAG_FIX = 519          # 定位标识（2 = GPS 成功）
TAG_SAT = 517          # 可见卫星数
TAG_SAT_TOTAL = 516    # 搜星总数
TAG_CN4 = 515          # 最强 4 星 CN 值
TAG_TEMP = 256         # 温度
TAG_BOOT = 777         # 开机原因
TAG_GNSS_BIN = 1294    # GNSS BINARY 差分
TAG_GSENSOR = 1293     # gsensor IMU（私有）

# 一次查询合并取回的 tag 集
STATUS_TAGS = [TAG_VBAT, TAG_SAT, TAG_SIGNAL, TAG_FIX, TAG_CN4, TAG_GNSS_BIN]

# 电压 → 电量百分比
VBAT_EMPTY_MV = 3000
VBAT_FULL_MV = 4200
VBAT_LOW_MV = 3400    # < 3400 低电量
VBAT_MID_MV = 3700    # < 3700 中等

# 离线判定：设备移动时 5s 一报、静止时 300s 一报，
# 超过该秒数没有新定位才判定离线（留足静止期的余量）
OFFLINE_AFTER_S = 900

# 转弯判定：相邻两点方位角变化超过该角度即认为发生转向
TURN_THRESHOLD_DEG = 25
# 转向判定的最小分段位移：短于此距离的位移视为 GPS 抖动/停留，不参与转向统计
TURN_MIN_SEGMENT_M = 15.0
