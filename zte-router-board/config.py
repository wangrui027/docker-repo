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
    "a4:6b:b6:3c:86:3d"
]

# ========== 数据采集间隔（秒） ==========
FETCH_INTERVAL_SECONDS = 5

# ========== 禁止操作限速的设备 IP 列表 ==========
# 列表中的 IP 和最后一位 < 10 的设备均不允许操作限速
QOS_RESTRICTED_IPS = [
    # "192.168.100.1",   # 示例：路由器本身
    "192.168.100.114",    # r68s
    "192.168.100.53",     # Nihaorz-PC
    "192.168.100.54",     # yoga14s
]

# ========== 数据保留天数 ==========
DATA_RETENTION_DAYS = 7
