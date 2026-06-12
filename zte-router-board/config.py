# config.py
import os

# ========== 路由器连接配置 ==========
ROUTER_BASE = "http://zte.home"
# 优先从环境变量取密码，否则使用硬编码（建议设置环境变量）
PASSWORD = "8U36d6YzJ#8xi3!m"

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

# ========== 数据保留天数 ==========
DATA_RETENTION_DAYS = 7
