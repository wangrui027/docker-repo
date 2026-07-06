# config.py
# ========== 路由器连接配置 ==========
ROUTER_BASE = "http://zte.home"
# 优先从环境变量取密码，否则使用硬编码（建议设置环境变量）
PASSWORD = "U9wH$A9PY374s@CA"

# ========== 设备匹配列表 ==========
PRESET_MATCH_LIST = [
    "红米K40",
    "红米K30S",
    "iQOO Neo9",
    "Nihaorz-PC",
    "OPPO Reno3 Pro",
    "小度青禾学习手机",
    "电犀牛r68s",
    "X96_X6",
    "yoga14s",
    "192.168.100.50",
    "192.168.100.102",
    "a4:6b:b6:3c:86:3d"
]

# ========== 数据采集间隔（秒） ==========
FETCH_INTERVAL_SECONDS = 5

# ========== 禁止操作限速的设备 IP 列表 ==========
# 列表中的 IP 和最后一位 < 10 的设备均不允许操作限速
QOS_RESTRICTED_IPS = [
    # "192.168.100.1",   # 示例：路由器本身
    "192.168.100.114",    # r68s
    "192.168.100.102",    # ubuntu-PC
    "192.168.100.53",     # Nihaorz-PC
    "192.168.100.54",     # yoga14s
]

# ========== 自动分时限速策略 ==========
# 每条策略包含：生效日期、时间段、目标设备、限速速率
# days: 1=周一 ~ 7=周日，例如 [1,2,3,4,5] 表示周一至周五
# time_start / time_end: 格式 HH:MM，允许跨天（如 22:00 ~ 08:00）
# devices: 设备标识列表（支持名称、IP、MAC 任一匹配）
# max_upload_mbps / max_download_mbps: 限速速率（Mbps）
AUTO_QOS_SCHEDULES = [
    {
        "days": [1,2,3,4,5,6,7],
        "time_start": "20:30",
        "time_end": "09:30",
        "devices": ["X96_X6"],
        "max_upload_mbps": 0.01,
        "max_download_mbps": 0.01,
    },
]

# ========== 数据保留天数 ==========
DATA_RETENTION_DAYS = 7

# "DEBUG" 显示详细采集计时, "INFO" 只显示关键日志, "WARNING" 只显示警告
LOG_LEVEL = "INFO"

# ========== WxPusher 通知配置 ==========
# 用于数据采集停滞告警推送
WXPUSHER_URL = "http://wxpusher.zjiecode.com/api/send/message"
WXPUSHER_APP_TOKEN = "xxxxx"
WXPUSHER_UIDS = ["xxxxx"]
