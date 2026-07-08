import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

import requests
import schedule
from flask import Flask, request, jsonify, render_template

# 导入配置文件中的所有配置
from config import *

# ==================== 配置 ====================
# 属性筛选列表：用于控制最终 JSON 中保存哪些字段。
# - 如果列表为空（[]），则保存设备的所有字段（完整信息）。
# - 如果列表非空，则只保存列表中指定的字段名。
# 下方列出了常用的关键字段，可根据实际需求增删。
KEEP_FIELDS = [
    # "DevName",  # 设备名称（如“红米K40”）
    # "IPAddress",  # IPv4 地址
    # "MACAddress",  # MAC 地址
    # "ActiveTime",  # 最近一次活跃时间（设备有数据传输的时刻）
    # "InactiveTime",  # 进入非活跃状态的时间（设备断开或休眠的时刻）
    # "SNTPTime",  # 路由器当前网络时间（SNTP 同步时间）
    # "OnlineTimes",  # 设备累计上线次数（每次完整连接计为一次）
    # "BytesSend",  # 累计发送字节数（上行流量）
    # "BytesReceived",  # 累计接收字节数（下行流量）
    # "UsbandWidth",  # 上行带宽限制
    # "DsbandWidth",  # 下行带宽限制
    # "Active"  # 设备在线状态（1在线，0离线）
]

VALID_CACHE_SECONDS = 30  # 相同Cookie跳过验证的缓存时长（秒）

# ===== 数据库配置 =====
DB_FILE = "data/data.db"
PLUGIN_COOKIE_FILE = "data/router_plugin_cookie.txt"  # 插件 Cookie 持久化文件
db_lock = threading.Lock()  # 数据库写入锁
qos_deleting_set = set()  # 正在解除限速的设备 MAC 集合（内存标记）


# =====================

# =============================================

# 日志工具
def log_info(msg):
    if LOG_LEVEL in ("INFO", "DEBUG"):
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [INFO] {msg}")


def log_debug(msg):
    if LOG_LEVEL == "DEBUG":
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [DEBUG] {msg}")


def log_warning(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [WARN] {msg}")


# 获取请求的真实客户端 IP（支持反向代理）
def get_client_ip():
    """优先从 X-Forwarded-For / X-Real-IP 获取，fallback 到 remote_addr"""
    x_forwarded_for = request.headers.get('X-Forwarded-For', '')
    if x_forwarded_for:
        # X-Forwarded-For 格式: client, proxy1, proxy2，取第一个
        return x_forwarded_for.split(',')[0].strip()
    x_real_ip = request.headers.get('X-Real-IP', '')
    if x_real_ip:
        return x_real_ip.strip()
    return request.remote_addr or 'unknown IP'


def is_qos_restricted(ip):
    """检查 IP 是否不允许操作限速（最后一位 < 10 或在禁止列表中）"""
    if not ip:
        return False
    # IP 在配置的禁止列表中
    if ip in QOS_RESTRICTED_IPS:
        return True
    # IP 最后一位 < 10（路由器基础设施）
    try:
        return int(ip.split('.')[-1]) < 10
    except (ValueError, IndexError):
        return False


# ========== 时间解析辅助函数 ==========
def parse_time_to_datetime(time_str):
    """尝试解析两种常见时间格式，返回 datetime 对象或 None"""
    if not time_str or not time_str.strip():
        return None
    # 格式1: "2026-06-10 19:03:36"
    # 格式2: "2026-06-10T19:09:36"
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(time_str.strip(), fmt)
        except ValueError:
            continue
    log_warning(f"无法解析时间字符串: {time_str}")
    return None


# ===== 数据库初始化函数 =====
def init_db():
    """创建设备信息表、历史记录表、速率表及索引，并启动每日清理任务"""
    # 确保 data 目录存在
    Path(DB_FILE).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_FILE) as conn:
        # 设备信息表（自动维护）
        conn.execute('''
            CREATE TABLE IF NOT EXISTS device_info (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                devname TEXT,
                ipaddress TEXT,
                macaddress TEXT UNIQUE NOT NULL,
                first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                active INTEGER DEFAULT 0,
                sntp_time TIMESTAMP NULL,
                active_time TIMESTAMP NULL,
                offline_time TIMESTAMP NULL,
                online_duration_sec INTEGER NULL,
                offline_duration_sec INTEGER NULL,
                latest_upload_kbps REAL NULL,
                latest_download_kbps REAL NULL
            )
        ''')
        # 添加最新流量字段（兼容旧表）
        try:
            conn.execute("ALTER TABLE device_info ADD COLUMN latest_bytes_send BIGINT DEFAULT 0")
            conn.execute("ALTER TABLE device_info ADD COLUMN latest_bytes_received BIGINT DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # 列已存在
        # 添加 QoS 限速字段（兼容旧表）
        try:
            conn.execute("ALTER TABLE device_info ADD COLUMN qos_enabled INTEGER DEFAULT 0")
            conn.execute("ALTER TABLE device_info ADD COLUMN qos_max_upload_kbps INTEGER DEFAULT 0")
            conn.execute("ALTER TABLE device_info ADD COLUMN qos_max_download_kbps INTEGER DEFAULT 0")
            conn.execute("ALTER TABLE device_info ADD COLUMN qos_inst_id TEXT")
            conn.execute("ALTER TABLE device_info ADD COLUMN qos_limit_time TEXT")
            conn.execute("ALTER TABLE device_info ADD COLUMN qos_duration_minutes INTEGER DEFAULT 0")
            conn.execute("ALTER TABLE device_info ADD COLUMN qos_is_auto INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # 列已存在
        # 设备历史记录表
        conn.execute('''
            CREATE TABLE IF NOT EXISTS device_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                record_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                DevName TEXT,
                IPAddress TEXT,
                MACAddress TEXT,
                ActiveTime TEXT,
                InactiveTime TEXT,
                SNTPTime TEXT,
                OnlineTimes INTEGER,
                BytesSend BIGINT,
                BytesReceived BIGINT,
                UsbandWidth INTEGER,
                DsbandWidth INTEGER
            )
        ''')
        # 设备速率表（新增 sntp_time 字段）
        conn.execute('''
            CREATE TABLE IF NOT EXISTS device_speed (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mac TEXT NOT NULL,
                start_time TIMESTAMP NOT NULL,
                end_time TIMESTAMP NOT NULL,
                sntp_time TIMESTAMP NULL,
                upload_kbps REAL NOT NULL,
                download_kbps REAL NOT NULL,
                upload_bytes INTEGER NOT NULL,
                download_bytes INTEGER NOT NULL,
                duration_sec REAL NOT NULL,
                prev_history_id INTEGER NOT NULL,
                curr_history_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # 创建索引
        conn.execute('CREATE INDEX IF NOT EXISTS idx_devname ON device_history(DevName)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_ipaddress ON device_history(IPAddress)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_macaddress ON device_history(MACAddress)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_record_time ON device_history(record_time)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_speed_mac ON device_speed(mac)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_speed_sntp_time ON device_speed(sntp_time)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_speed_mac_sntp ON device_speed(mac, sntp_time)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_speed_prev_id ON device_speed(prev_history_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_speed_curr_id ON device_speed(curr_history_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_device_info_mac ON device_info(macaddress)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_device_info_last_seen ON device_info(last_seen)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_device_info_offline_time ON device_info(offline_time)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_history_mac_record ON device_history(MACAddress, record_time)')


def normalize_sntp_time(sntp_str):
    """将 SNTPTime 字符串中的 'T' 替换为空格，统一为 'YYYY-MM-DD HH:MM:SS' 格式"""
    if not sntp_str:
        return None
    return sntp_str.replace('T', ' ')


def upsert_device_info(device_info, active, offline_time_str, active_time_str, sntp_time_str, latest_send, latest_recv,
                       _conn=None):
    """
    插入或更新设备信息表（基于 MAC 地址），同时更新最新流量累计值
    _conn: 复用上层连接，避免双连接争文件锁
    """
    mac = device_info.get("MACAddress", "")
    if not mac:
        return
    devname = device_info.get("DevName", "")
    ip = device_info.get("IPAddress", "")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # QoS 限速信息（从 OBJ_QOS_BCRULE_ID 解析得到）
    qos_enabled = int(device_info.get("qos_enabled", 0) or 0)
    qos_max_up = int(device_info.get("qos_max_upload_kbps", 0) or 0)
    qos_max_down = int(device_info.get("qos_max_download_kbps", 0) or 0)
    qos_inst_id = device_info.get("qos_inst_id") or None
    qos_limit_time = device_info.get("qos_limit_time") or None
    qos_duration_minutes = int(device_info.get("qos_duration_minutes", 0) or 0)
    qos_is_auto = int(device_info.get("qos_is_auto", 0) or 0)

    final_offline = None
    if active == 0:
        if offline_time_str and offline_time_str.strip():
            final_offline = offline_time_str.strip()
        else:
            final_offline = now_str

    final_active = None
    if active_time_str and active_time_str.strip():
        final_active = active_time_str.strip()
    elif active == 1:
        final_active = now_str

    online_dur = None
    offline_dur = None
    sntp_time_str = normalize_sntp_time(sntp_time_str) if sntp_time_str else None
    sntp_dt = parse_time_to_datetime(sntp_time_str) if sntp_time_str else None
    if active == 1 and active_time_str:
        active_dt = parse_time_to_datetime(active_time_str)
        if sntp_dt and active_dt:
            online_dur = int((sntp_dt - active_dt).total_seconds())
            if online_dur < 0:
                online_dur = 0
    elif active == 0 and offline_time_str:
        inactive_dt = parse_time_to_datetime(offline_time_str)
        if sntp_dt and inactive_dt:
            offline_dur = int((sntp_dt - inactive_dt).total_seconds())
            if offline_dur < 0:
                offline_dur = 0

    # 复用上层连接 or 自建连接
    def _do_upsert(conn):
        nonlocal qos_limit_time, qos_duration_minutes
        cur = conn.execute("SELECT id FROM device_info WHERE macaddress = ?", (mac,))
        exists = cur.fetchone()
        # 如果是 UPDATE 且设备有限速，但本次未传 qos_limit_time / qos_duration_minutes，
        # 则保留数据库已有值（fetch_and_process 采集时不覆盖这两个字段）
        if exists and qos_enabled == 1 and not qos_limit_time and qos_duration_minutes == 0:
            cur = conn.execute(
                "SELECT qos_limit_time, qos_duration_minutes FROM device_info WHERE macaddress = ?",
                (mac,))
            row = cur.fetchone()
            if row:
                qos_limit_time = row[0] or qos_limit_time
                qos_duration_minutes = row[1] if row[1] else qos_duration_minutes
        if exists:
            conn.execute('''
                UPDATE device_info
                SET devname = ?, ipaddress = ?, last_seen = ?, active = ?,
                    sntp_time = ?, active_time = ?, offline_time = ?,
                    online_duration_sec = ?, offline_duration_sec = ?,
                    latest_bytes_send = ?, latest_bytes_received = ?,
                    qos_enabled = ?, qos_max_upload_kbps = ?, qos_max_download_kbps = ?,
                    qos_inst_id = ?, qos_limit_time = ?, qos_duration_minutes = ?,
                    qos_is_auto = ?
                WHERE macaddress = ?
            ''', (devname, ip, now_str, active,
                  sntp_time_str, final_active, final_offline,
                  online_dur, offline_dur,
                  latest_send, latest_recv,
                  qos_enabled, qos_max_up, qos_max_down,
                  qos_inst_id, qos_limit_time, qos_duration_minutes,
                  qos_is_auto, mac))
        else:
            conn.execute('''
                INSERT INTO device_info (
                    devname, ipaddress, macaddress, first_seen, last_seen,
                    active, sntp_time, active_time, offline_time,
                    online_duration_sec, offline_duration_sec,
                    latest_upload_kbps, latest_download_kbps,
                    latest_bytes_send, latest_bytes_received,
                    qos_enabled, qos_max_upload_kbps, qos_max_download_kbps,
                    qos_inst_id, qos_limit_time, qos_duration_minutes, qos_is_auto
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (devname, ip, mac, now_str, now_str,
                  active, sntp_time_str, final_active, final_offline,
                  online_dur, offline_dur, None, None,
                  latest_send, latest_recv,
                  qos_enabled, qos_max_up, qos_max_down,
                  qos_inst_id, qos_limit_time, qos_duration_minutes,
                  qos_is_auto))

    if _conn is not None:
        _do_upsert(_conn)
    else:
        with sqlite3.connect(DB_FILE) as conn:
            _do_upsert(conn)


def update_devices_offline(matched_macs, _conn=None):
    if not matched_macs:
        return
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    placeholders = ','.join(['?'] * len(matched_macs))
    if _conn is None:
        with sqlite3.connect(DB_FILE) as conn:
            _do_update(conn, now_str, placeholders, matched_macs)
    else:
        _do_update(_conn, now_str, placeholders, matched_macs)


def _do_update(conn, now_str, placeholders, matched_macs):
    query = f'''
        UPDATE device_info
        SET offline_time = ?, latest_upload_kbps = NULL, latest_download_kbps = NULL
        WHERE macaddress NOT IN ({placeholders}) AND offline_time IS NULL
    '''
    params = [now_str] + matched_macs
    conn.execute(query, params)


def insert_device_record(device_info, _conn=None):
    def to_int(val):
        try:
            return int(val) if val and val.strip() else 0
        except:
            return 0

    mac = device_info.get("MACAddress", "")
    active = to_int(device_info.get("Active", 0))
    inactive_time = device_info.get("InactiveTime", "")
    active_time = device_info.get("ActiveTime", "")
    sntp_time = device_info.get("SNTPTime", "")
    sntp_time = normalize_sntp_time(sntp_time)
    bytes_send = to_int(device_info.get("BytesSend"))
    bytes_recv = to_int(device_info.get("BytesReceived"))

    if _conn is not None:
        conn = _conn
        _close_conn = False
    else:
        conn = sqlite3.connect(DB_FILE)
        _close_conn = True
    try:
        upsert_device_info(device_info, active, inactive_time, active_time, sntp_time, bytes_send, bytes_recv,
                           _conn=conn)

        cursor = conn.execute('''
            INSERT INTO device_history (
                DevName, IPAddress, MACAddress, ActiveTime, InactiveTime,
                SNTPTime, OnlineTimes, BytesSend, BytesReceived, UsbandWidth, DsbandWidth
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            device_info.get("DevName", ""),
            device_info.get("IPAddress", ""),
            mac,
            device_info.get("ActiveTime", ""),
            device_info.get("InactiveTime", ""),
            device_info.get("SNTPTime", ""),
            to_int(device_info.get("OnlineTimes")),
            bytes_send,
            bytes_recv,
            to_int(device_info.get("UsbandWidth")),
            to_int(device_info.get("DsbandWidth"))
        ))
        current_id = cursor.lastrowid
        if mac:
            calculate_and_insert_speed(conn, mac, current_id, sntp_time)
        return current_id
    finally:
        if _close_conn:
            conn.close()


def calculate_and_insert_speed(conn, mac, current_id, sntp_time_str):
    """
    计算并插入速率记录，同时存储对应的 SNTPTime（路由器时间）
    插入成功后更新 device_info 中的最新速率
    """
    cursor = conn.execute('''
        SELECT id, record_time, BytesSend, BytesReceived
        FROM device_history
        WHERE MACAddress = ?
        ORDER BY record_time DESC
        LIMIT 2
    ''', (mac,))
    rows = cursor.fetchall()
    if len(rows) < 2:
        return

    if rows[0][0] != current_id:
        return

    prev_id, prev_time_str, prev_send, prev_recv = rows[1]
    curr_id, curr_time_str, curr_send, curr_recv = rows[0]

    try:
        prev_time = datetime.strptime(prev_time_str, "%Y-%m-%d %H:%M:%S")
        curr_time = datetime.strptime(curr_time_str, "%Y-%m-%d %H:%M:%S")
    except:
        return
    duration = (curr_time - prev_time).total_seconds()
    if duration <= 0:
        return

    upload_bytes = curr_send - prev_send
    download_bytes = curr_recv - prev_recv
    if upload_bytes < 0 or download_bytes < 0:
        # 流量计数器重置，清除最新速率
        conn.execute(
            "UPDATE device_info SET latest_upload_kbps = NULL, latest_download_kbps = NULL WHERE macaddress = ?",
            (mac,))
        return

    upload_kbps = round(upload_bytes / duration / 1024.0, 2)
    download_kbps = round(download_bytes / duration / 1024.0, 2)

    # 避免重复插入
    exists = conn.execute('''
        SELECT 1 FROM device_speed
        WHERE mac = ? AND prev_history_id = ? AND curr_history_id = ?
    ''', (mac, prev_id, curr_id)).fetchone()
    if exists:
        return

    sntp_time_str = normalize_sntp_time(sntp_time_str) if sntp_time_str else None
    conn.execute('''
        INSERT INTO device_speed
        (mac, start_time, end_time, sntp_time, upload_kbps, download_kbps,
         upload_bytes, download_bytes, duration_sec,
         prev_history_id, curr_history_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (mac, prev_time_str, curr_time_str, sntp_time_str,
          upload_kbps, download_kbps,
          upload_bytes, download_bytes, duration,
          prev_id, curr_id))

    # 更新设备信息表中的最新速率
    conn.execute('''
        UPDATE device_info
        SET latest_upload_kbps = ?, latest_download_kbps = ?
        WHERE macaddress = ?
    ''', (upload_kbps, download_kbps, mac))


def cleanup_old_data():
    cutoff_time = (datetime.now() - timedelta(days=DATA_RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    with db_lock:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.execute("DELETE FROM device_history WHERE record_time < ?", (cutoff_time,))
            deleted_history = cursor.rowcount
            cursor = conn.execute("DELETE FROM device_speed WHERE end_time < ?", (cutoff_time,))
            deleted_speed = cursor.rowcount
            if deleted_history > 0 or deleted_speed > 0:
                log_info(
                    f"清理过期数据: 删除历史记录 {deleted_history} 条，速率记录 {deleted_speed} 条 (保留最近 {DATA_RETENTION_DAYS} 天)")


# ========== WxPusher 通知 ==========
def send_wxpusher_notification(message):
    """发送 WxPusher 通知"""
    try:
        payload = {
            "appToken": WXPUSHER_APP_TOKEN,
            "content": message,
            "summary": "📡【网络设备看板】数据采集异常",
            "contentType": 1,
            "uids": WXPUSHER_UIDS,
        }
        resp = requests.post(WXPUSHER_URL, json=payload, timeout=10)
        log_info(f"[WxPusher] 通知发送结果: HTTP {resp.status_code}")
    except Exception as e:
        log_warning(f"[WxPusher] 通知发送失败: {e}")


# ========== 数据采集停滞检测 ==========
_last_stall_notification_time = 0.0
_last_cookie_miss_notification_time = 0.0  # "无有效Cookie"通知冷却
COOKIE_MISS_COOLDOWN = 3600  # Cookie 缺失通知冷却时间（秒）
STALL_CHECK_INTERVAL = 5  # 每次查询间隔（秒）
STALL_CHECK_COUNT = 5  # 连续查询次数
STALL_NOTIFICATION_COOLDOWN = 3600  # 通知冷却时间（秒）


def check_data_collection_stalled():
    """
    每5分钟执行一次：连续5次（每次间隔5秒）查询 device_history 最新记录，
    若始终相同则说明数据采集停滞，发送 WxPusher 告警（每小时最多一次）
    """
    global _last_stall_notification_time

    if not plugin_cookie:
        log_debug("[StallCheck] 无插件Cookie，跳过停滞检测")
        return

    log_debug("[StallCheck] 开始检测数据采集状态...")
    last_id = None
    all_same = True
    last_record_time = None

    for i in range(STALL_CHECK_COUNT):
        row = None
        with db_lock:
            with sqlite3.connect(DB_FILE) as conn:
                cursor = conn.execute(
                    "SELECT id, SNTPTime FROM device_history ORDER BY SNTPTime DESC, id DESC LIMIT 1"
                )
                row = cursor.fetchone()

        current_id = row[0] if row else None
        current_time = row[1] if row else None
        log_debug(f"[StallCheck] 第{i + 1}次查询: id={current_id}, SNTPTime={current_time}")

        if i == 0:
            last_id = current_id
            last_record_time = current_time
        elif current_id != last_id:
            all_same = False
            log_debug("[StallCheck] 检测到新数据，采集正常")
            break

        if i < STALL_CHECK_COUNT - 1:
            time.sleep(STALL_CHECK_INTERVAL)

    # 表为空（last_id is None）时不触发通知
    if all_same and last_id is not None:
        now = time.time()
        if now - _last_stall_notification_time >= STALL_NOTIFICATION_COOLDOWN:
            message = (
                f"数据采集失败\n"
                f"最后记录 ID={last_id}\n"
                f"最后 SNTPTime={last_record_time}\n"
                f"连续{STALL_CHECK_COUNT}次({STALL_CHECK_COUNT * STALL_CHECK_INTERVAL}秒)无新数据"
            )
            send_wxpusher_notification(message)
            _last_stall_notification_time = now
            log_info("[StallCheck] 已发送停滞通知")
        else:
            remaining = STALL_NOTIFICATION_COOLDOWN - (now - _last_stall_notification_time)
            log_info(f"[StallCheck] 数据停滞但冷却中，剩余{int(remaining)}秒")


# =============================================

# 全局变量
plugin_cookie = None
invalid_cookies = set()
last_valid_cookie = None
last_valid_time = 0
last_used_source = None
lock = threading.Lock()
task_lock = threading.Lock()

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True  # 模板自动重载：修改 HTML 后无需重启


# ---------- Cookie 辅助函数 ----------
def parse_cookie_string(cookie_str):
    cookies = {}
    for item in cookie_str.split(';'):
        item = item.strip()
        if not item:
            continue
        if '=' in item:
            key, value = item.split('=', 1)
            cookies[key] = value
    return cookies


def check_session_valid(session):
    try:
        timestamp = int(time.time() * 1000)
        test_url = f"{ROUTER_BASE}/?_type=vueData&_tag=localnet_lan_info_lua&_={timestamp}"
        resp = session.get(test_url, timeout=10)
        return resp.status_code == 200 and ("Instance" in resp.text or "OBJ_LAN_INFO_ID" in resp.text)
    except:
        return False


def validate_cookie_string(cookie_str):
    try:
        test_session = requests.Session()
        test_session.cookies.update(parse_cookie_string(cookie_str))
        return check_session_valid(test_session)
    except:
        return False


def save_plugin_cookie(cookie_str):
    """将插件 Cookie 写入文件持久化"""
    try:
        Path(PLUGIN_COOKIE_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(PLUGIN_COOKIE_FILE, "w", encoding="utf-8") as f:
            f.write(cookie_str)
    except Exception as e:
        log_warning(f"保存插件Cookie文件失败: {e}")


def load_plugin_cookie():
    """从文件加载插件 Cookie，文件不存在或读取失败时返回 None"""
    try:
        if Path(PLUGIN_COOKIE_FILE).exists():
            with open(PLUGIN_COOKIE_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip()
                return content if content else None
    except:
        pass
    return None


def delete_plugin_cookie():
    """删除插件 Cookie 文件"""
    try:
        Path(PLUGIN_COOKIE_FILE).unlink(missing_ok=True)
    except:
        pass


def reset_valid_cache():
    global last_valid_cookie, last_valid_time
    last_valid_cookie = None
    last_valid_time = 0


def get_effective_cookie_and_session(skip_validate=True):
    """
    获取路由器会话，仅依赖插件 Cookie。
    优先使用内存中的 Cookie，无有效 Cookie 时尝试从文件加载。
    skip_validate=True（默认）：跳过 session 有效性验证 GET，省 0.5s。
    无有效 Cookie 时返回 (None, None)。
    """
    global plugin_cookie, last_valid_cookie, last_valid_time
    with lock:
        if plugin_cookie and plugin_cookie not in invalid_cookies:
            session = requests.Session()
            session.cookies.update(parse_cookie_string(plugin_cookie))
            if skip_validate or check_session_valid(session):
                return session, 'plugin'
            else:
                log_warning("插件Cookie失效，已清除")
                invalid_cookies.add(plugin_cookie)
                plugin_cookie = None
                delete_plugin_cookie()
                reset_valid_cache()
        # 内存无 Cookie，尝试从文件加载
        if not plugin_cookie:
            file_cookie = load_plugin_cookie()
            if file_cookie and file_cookie not in invalid_cookies:
                plugin_cookie = file_cookie
                session = requests.Session()
                session.cookies.update(parse_cookie_string(plugin_cookie))
                if skip_validate or check_session_valid(session):
                    log_info("从文件加载插件Cookie成功")
                    return session, 'plugin'
                else:
                    log_warning("文件Cookie失效，已清除")
                    invalid_cookies.add(plugin_cookie)
                    plugin_cookie = None
                    delete_plugin_cookie()
                    reset_valid_cache()
        return None, None


# ---------- 自动分时限速辅助函数 ----------
def in_time_window(days, time_start, time_end):
    """判断当前时间是否在指定的星期+时间段窗口内（支持跨天），days: 1=周一~7=周日"""
    now = datetime.now()
    weekday = now.weekday() + 1  # Python weekday(): 0=周一 → 转换为 1=周一
    if weekday not in days:
        return False
    current_min = now.hour * 60 + now.minute
    sh, sm = map(int, time_start.split(':'))
    eh, em = map(int, time_end.split(':'))
    start_min = sh * 60 + sm
    end_min = eh * 60 + em
    if end_min <= start_min:  # 跨天窗口（如 22:00 ~ 08:00）
        return current_min >= start_min or current_min < end_min
    return start_min <= current_min < end_min


def calc_remaining_minutes(time_end):
    """计算从当前时间到下一次 time_end 的剩余分钟数"""
    now = datetime.now()
    eh, em = map(int, time_end.split(':'))
    end_dt = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    if end_dt <= now:
        end_dt += timedelta(days=1)  # 已经过了，取明天同一时间
    return max(1, int((end_dt - now).total_seconds() / 60))


def match_auto_qos_device(dev_name, ip, mac, device_list):
    """检查设备是否匹配策略中的设备列表"""
    for entry in device_list:
        if entry == dev_name or entry == ip or entry == mac:
            return True
    return False


# ---------- 数据采集任务 ----------
def fetch_and_process():
    global last_used_source, last_valid_cookie, last_valid_time, plugin_cookie
    _fetch_start = time.time()
    if not task_lock.acquire(timeout=5):
        log_info("采集任务被Cookie验证占用，本次跳过")
        return
    # 预初始化计时变量，确保 finally 中异常时也能安全引用
    _t1 = _t2 = _t3 = _t4 = _t5 = _t6 = _fetch_start
    try:
        session, source = get_effective_cookie_and_session()
        if session is None:
            log_info("无有效Cookie，跳过本次采集")
            global _last_cookie_miss_notification_time
            now = time.time()
            if now - _last_cookie_miss_notification_time >= COOKIE_MISS_COOLDOWN:
                send_wxpusher_notification("无有效Cookie，跳过本次数据采集")
                _last_cookie_miss_notification_time = now
                log_info("[WxPusher] 已发送 Cookie 缺失通知")
            return
        if source != last_used_source:
            log_warning(f"会话切换为 {source}")
            last_used_source = source
        _t1 = time.time()
        log_debug(f"  ├─ session: {_t1 - _fetch_start:.2f}s")

        timestamp = int(time.time() * 1000)
        xml_url = f"{ROUTER_BASE}/?_type=vueData&_tag=localnet_lan_info_lua&_={timestamp}"
        response = session.get(xml_url, timeout=10)
        xml_content = response.text
        _t2 = time.time()
        log_debug(f"  ├─ HTTP请求(XML): {_t2 - _t1:.2f}s (状态码={response.status_code})")

        # 解析 XML，如果失败则清除过期 session 重试一次
        try:
            root = ET.fromstring(xml_content)
        except ET.ParseError:
            log_warning("XML解析失败，插件Cookie已过期，清除等待新Cookie...")
            with lock:
                if plugin_cookie:
                    invalid_cookies.add(plugin_cookie)
                    plugin_cookie = None
                    delete_plugin_cookie()
                reset_valid_cache()
            session, source = get_effective_cookie_and_session()
            if session is None:
                return
            url2 = f"{ROUTER_BASE}/?_type=vueData&_tag=localnet_lan_info_lua&_={int(time.time() * 1000)}"
            response = session.get(url2, timeout=10)
            xml_content = response.text
            root = ET.fromstring(xml_content)  # 再失败直接抛异常
        _t3 = time.time()
        log_debug(f"  ├─ XML解析: {_t3 - _t2:.2f}s (设备数={len(root.findall('.//OBJ_LAN_INFO_ID/Instance'))})")

        # 解析 QoS 限速规则（OBJ_QOS_BCRULE_ID），按 MAC 建立索引
        qos_map = {}  # MAC → {"qos_enabled": 1, "qos_max_upload_kbps": ..., "qos_max_download_kbps": ...}
        for qos_inst in root.findall(".//OBJ_QOS_BCRULE_ID/Instance"):
            qos_data = {}
            for pn, pv in zip(qos_inst.findall("ParaName"), qos_inst.findall("ParaValue")):
                name = pn.text
                value = pv.text if pv.text is not None else ""
                qos_data[name] = value
            mac = qos_data.get("MACDev", "")
            if mac:
                try:
                    qos_map[mac] = {
                        "qos_enabled": 1,
                        "qos_inst_id": qos_data.get("_InstID", ""),
                        "qos_max_upload_kbps": int(float(qos_data.get("BandwithRateMaxUp", 0))),
                        "qos_max_download_kbps": int(float(qos_data.get("BandwithRateMaxDown", 0)))
                    }
                except (ValueError, TypeError):
                    pass

        # 解析设备列表（OBJ_LAN_INFO_ID）
        instances = root.findall(".//OBJ_LAN_INFO_ID/Instance")
        matched_devices = []
        matched_macs = []
        for inst in instances:
            device_info = {}
            para_names = inst.findall("ParaName")
            para_values = inst.findall("ParaValue")
            for pn, pv in zip(para_names, para_values):
                name = pn.text
                value = pv.text if pv.text is not None else ""
                device_info[name] = value
            dev_name = device_info.get("DevName", "")
            ip_addr = device_info.get("IPAddress", "")
            mac_addr = device_info.get("MACAddress", "")
            matched = False
            if dev_name in PRESET_MATCH_LIST or ip_addr in PRESET_MATCH_LIST or mac_addr in PRESET_MATCH_LIST:
                matched = True
            if matched:
                # 合并 QoS 限速信息（正在解除限速的设备强制跳过）
                if mac_addr in qos_deleting_set:
                    device_info["qos_enabled"] = 0
                    device_info["qos_inst_id"] = None
                    device_info["qos_max_upload_kbps"] = 0
                    device_info["qos_max_download_kbps"] = 0
                    device_info["qos_limit_time"] = None
                    device_info["qos_duration_minutes"] = 0
                    if mac_addr not in qos_map:
                        qos_deleting_set.discard(mac_addr)  # 路由器已同步
                elif mac_addr in qos_map:
                    qos = qos_map[mac_addr]
                    device_info["qos_enabled"] = 1
                    device_info["qos_inst_id"] = qos["qos_inst_id"]
                    device_info["qos_max_upload_kbps"] = qos["qos_max_upload_kbps"]
                    device_info["qos_max_download_kbps"] = qos["qos_max_download_kbps"]
                    # qos_limit_time / qos_duration_minutes 不设置 → upsert 保留已有值
                else:
                    # 未匹配到限速规则 → 全部重置
                    device_info["qos_enabled"] = 0
                    device_info["qos_inst_id"] = None
                    device_info["qos_max_upload_kbps"] = 0
                    device_info["qos_max_download_kbps"] = 0
                    device_info["qos_limit_time"] = None
                    device_info["qos_duration_minutes"] = 0

                if KEEP_FIELDS:
                    filtered_info = {field: device_info.get(field, "") for field in KEEP_FIELDS}
                    matched_devices.append(filtered_info)
                else:
                    matched_devices.append(device_info)
                if mac_addr:
                    matched_macs.append(mac_addr)

        _t4 = time.time()
        log_debug(f"  ├─ 设备/QoS解析: {_t4 - _t3:.2f}s (匹配={len(matched_devices)})")

        # ===== 自动解除过期 QoS 限速 =====
        with sqlite3.connect(DB_FILE) as conn:
            expired = conn.execute(
                "SELECT macaddress, qos_inst_id, qos_limit_time, qos_duration_minutes, ipaddress "
                "FROM device_info WHERE qos_enabled = 1 AND qos_duration_minutes > 0"
            ).fetchall()
        if expired:
            # 构建 MAC → SNTPTime 映射（从当前 XML 所有实例中提取）
            sntp_map = {}
            for inst in root.findall(".//OBJ_LAN_INFO_ID/Instance"):
                mac = None
                sntp = None
                for pn, pv in zip(inst.findall("ParaName"), inst.findall("ParaValue")):
                    if pn.text == "MACAddress":
                        mac = pv.text
                    elif pn.text == "SNTPTime":
                        sntp = normalize_sntp_time(pv.text if pv.text else "")
                if mac and sntp:
                    sntp_map[mac] = sntp

            for mac, qos_inst_id, limit_time_str, dur_min, ip_addr in expired:
                # 受限制 IP 的设备永久限速，不自动解除
                if is_qos_restricted(ip_addr):
                    continue
                if not limit_time_str or mac not in sntp_map:
                    continue
                try:
                    limit_dt = parse_time_to_datetime(limit_time_str)
                    current_dt = parse_time_to_datetime(sntp_map[mac])
                    if limit_dt and current_dt:
                        expire_dt = limit_dt + timedelta(minutes=dur_min)
                        if current_dt >= expire_dt:
                            log_info(f"QoS限速已过期，自动解除: MAC={mac}, instID={qos_inst_id}, duration={dur_min}min")
                            qos_deleting_set.add(mac)
                            try:
                                token = get_session_token(session)
                                form_data = {
                                    "IF_ACTION": "Delete",
                                    "_InstID": qos_inst_id,
                                    "_sessionTOKEN": token
                                }
                                qos_api_post(session, form_data)
                            except Exception as e:
                                log_warning(f"QoS自动解除API调用失败: {mac}, {e}")
                            with db_lock:
                                with sqlite3.connect(DB_FILE) as conn2:
                                    conn2.execute(
                                        "UPDATE device_info SET qos_enabled=0, qos_inst_id=NULL, "
                                        "qos_limit_time=NULL, qos_duration_minutes=0, qos_is_auto=0 WHERE macaddress=?",
                                        (mac,))
                except Exception as e:
                    log_warning(f"QoS过期检查异常: {mac}, {e}")

        # # 保存原始 JSON 快照到 data/日期/ 目录
        # if matched_devices:
        #     now = datetime.now()
        #     date_dir = Path("data") / now.strftime("%Y-%m-%d")
        #     date_dir.mkdir(parents=True, exist_ok=True)
        #     filename = now.strftime("%Y-%m-%d_%H-%M-%S.json")
        #     filepath = date_dir / filename
        #     with open(filepath, "w", encoding="utf-8") as f:
        #         json.dump(matched_devices, f, ensure_ascii=False, indent=2)
        #     log_debug(f"已保存快照: {filepath}（{len(matched_devices)} 台设备）")

        if matched_devices or matched_macs:
            with db_lock:
                with sqlite3.connect(DB_FILE) as conn:
                    for device in matched_devices:
                        insert_device_record(device, _conn=conn)
                    if matched_macs:
                        update_devices_offline(matched_macs, _conn=conn)
        _t5 = time.time()
        log_debug(f"  ├─ DB写入: {_t5 - _t4:.2f}s ({len(matched_devices)}台设备)")
        # ===== 自动分时限速策略（纯配置驱动，无需 DB 持久化） =====
        if AUTO_QOS_SCHEDULES:
            for policy in AUTO_QOS_SCHEDULES:
                days = policy.get("days", [])
                t_start = policy.get("time_start", "00:00")
                t_end = policy.get("time_end", "00:00")
                devices_list = policy.get("devices", [])
                max_up = int(float(policy.get("max_upload_mbps", 0) or 0) * 1000)
                max_down = int(float(policy.get("max_download_mbps", 0) or 0) * 1000)
                in_window = in_time_window(days, t_start, t_end)

                for inst in root.findall(".//OBJ_LAN_INFO_ID/Instance"):
                    dev = {}
                    for pn, pv in zip(inst.findall("ParaName"), inst.findall("ParaValue")):
                        dev[pn.text] = pv.text if pv.text is not None else ""
                    mac = dev.get("MACAddress", "")
                    name = dev.get("DevName", "")
                    ip = dev.get("IPAddress", "")
                    if not match_auto_qos_device(name, ip, mac, devices_list):
                        continue

                    # 查 DB 中已有 inst_id（用于 Apply/Delete 操作）
                    with sqlite3.connect(DB_FILE) as conn:
                        cur = conn.execute(
                            "SELECT qos_inst_id FROM device_info WHERE macaddress=?", (mac,))
                        row = cur.fetchone()
                    inst_id = row[0] if row else None

                    # 判断路由器当前 QoS 状态：以实际 XML 数据为准
                    in_qos_map = mac in qos_map
                    xml_up = qos_map[mac]["qos_max_upload_kbps"] if in_qos_map else 0
                    xml_down = qos_map[mac]["qos_max_download_kbps"] if in_qos_map else 0
                    already_limited = in_qos_map and xml_up == max_up and xml_down == max_down

                    if in_window and max_up > 0 and max_down > 0 and not already_limited:
                        # 窗口内 + 速率不一致 → 应用自动限速
                        try:
                            token = get_session_token(session)  # 复用 fetch_and_process 已获取的 session
                            sntp = normalize_sntp_time(dev.get("SNTPTime", ""))
                            existing = inst_id if inst_id else "-1"
                            form_data = {
                                "MACDev": mac, "BandwithRateMaxDown": str(max_down),
                                "BandwithRateMaxUp": str(max_up), "UserName": mac,
                                "IF_ACTION": "Apply", "_InstID": existing,
                                "_sessionTOKEN": token
                            }
                            post_resp = qos_api_post(session, form_data)
                            try:
                                root2 = ET.fromstring(post_resp)
                                el = root2.find("_InstID")
                                if el is not None and el.text:
                                    inst_id = el.text
                            except Exception:
                                pass
                            with db_lock:
                                with sqlite3.connect(DB_FILE) as conn:
                                    conn.execute(
                                        "UPDATE device_info SET qos_enabled=1, qos_inst_id=?, "
                                        "qos_max_upload_kbps=?, qos_max_download_kbps=?, "
                                        "qos_limit_time=?, qos_duration_minutes=?, qos_is_auto=1 WHERE macaddress=?",
                                        (inst_id, max_up, max_down, sntp,
                                         calc_remaining_minutes(t_end), mac))
                            log_info(f"自动限速已应用: {name}({mac}) → up={max_up}Kbps down={max_down}Kbps")
                        except Exception as e:
                            log_warning(f"自动限速失败: {name}({mac}), {e}")

                    elif not in_window and already_limited:
                        # 窗口外 + 路由器已有限速 → 解除自动限速
                        try:
                            if inst_id:
                                token = get_session_token(session)  # 复用已获取的 session
                                form_data = {"IF_ACTION": "Delete", "_InstID": inst_id,
                                             "_sessionTOKEN": token}
                                qos_api_post(session, form_data)
                            with db_lock:
                                with sqlite3.connect(DB_FILE) as conn:
                                    conn.execute(
                                        "UPDATE device_info SET qos_enabled=0, qos_inst_id=NULL, "
                                        "qos_limit_time=NULL, qos_duration_minutes=0, qos_is_auto=0 "
                                        "WHERE macaddress=?", (mac,))
                            log_info(f"自动限速已解除: {name}({mac})")
                        except Exception as e:
                            log_warning(f"自动限速解除失败: {name}({mac}), {e}")

        _t6 = time.time()
        log_debug(f"  └─ QoS限速处理: {_t6 - _t5:.2f}s")
        if source == 'plugin':
            with lock:
                last_valid_time = time.time()
                if plugin_cookie:
                    last_valid_cookie = plugin_cookie
    except Exception as e:
        log_warning(f"采集失败: {e}")
    finally:
        _elapsed = time.time() - _fetch_start
        log_debug(
            f"  └─ 合计: {_elapsed:.1f}s (HTTP={_t2 - _t1:.1f}s, 解析={_t4 - _t3:.1f}s, DB={_t5 - _t4:.1f}s, QoS={_t6 - _t5:.1f}s)")
        task_lock.release()


@app.route('/')
def dashboard():
    return render_template('index.html', fetch_interval=FETCH_INTERVAL_SECONDS,
                           qos_restricted_ips=QOS_RESTRICTED_IPS)


# ---------- HTTP 服务接口 ----------
@app.route('/api/update-cookie', methods=['POST'])
def update_cookie():
    global plugin_cookie, last_valid_cookie, last_valid_time
    client_ip = get_client_ip()
    data = request.get_json()
    cookie_str = data.get('cookies')
    log_info(f"[{client_ip}] 接收到Cookie更新请求")
    if not cookie_str:
        log_warning(f"[{client_ip}] 收到空Cookie请求")
        return jsonify({"status": "error", "message": "No cookies"}), 400
    if 'sidebarStatus=' not in cookie_str or 'SID=' not in cookie_str:
        log_info(f"[{client_ip}] 收到不完整Cookie（缺少sidebarStatus或SID），忽略")
        return jsonify({"status": "ignored", "message": "Incomplete cookie"}), 200
    if cookie_str in invalid_cookies:
        log_info(f"[{client_ip}] Cookie已在黑名单中，忽略")
        return jsonify({"status": "ignored", "message": "Invalid cookie (blacklisted)"}), 200
    now = time.time()
    if cookie_str == last_valid_cookie and (now - last_valid_time) < VALID_CACHE_SECONDS:
        with lock:
            if plugin_cookie != cookie_str:
                plugin_cookie = cookie_str
                save_plugin_cookie(cookie_str)
                log_info(f"[{client_ip}] 插件Cookie已同步（缓存命中）")
        return jsonify({"status": "ok", "cached": True})
    if not task_lock.acquire(timeout=10):
        log_warning(f"[{client_ip}] 无法获取任务锁，采集任务可能长时间运行，放弃本次验证")
        return jsonify({"status": "error", "message": "Task busy"}), 503
    try:
        log_info(f"[{client_ip}] 验证插件Cookie有效性...")
        is_valid = validate_cookie_string(cookie_str)
        if is_valid:
            with lock:
                plugin_cookie = cookie_str
                save_plugin_cookie(cookie_str)
                if cookie_str in invalid_cookies:
                    invalid_cookies.discard(cookie_str)
                last_valid_cookie = cookie_str
                last_valid_time = now
            log_warning(f"[{client_ip}] 插件Cookie有效，已更新")
        else:
            with lock:
                invalid_cookies.add(cookie_str)
                if plugin_cookie == cookie_str:
                    plugin_cookie = None
                    delete_plugin_cookie()
                reset_valid_cache()
            log_warning(f"[{client_ip}] 插件Cookie无效，已加入黑名单")
        return jsonify({"status": "ok" if is_valid else "invalid"})
    finally:
        task_lock.release()


# ===== 设备历史记录查询接口 =====
@app.route('/api/device-history', methods=['POST'])
def query_device_history():
    data = request.get_json() or {}
    filters = data.get("filters", [])
    order_by = data.get("order_by", "record_time")
    order_dir = data.get("order_dir", "asc").lower()
    page = int(data.get("page", 1))
    page_size = int(data.get("page_size", 20))
    allowed_order_fields = {
        "id", "record_time", "DevName", "IPAddress", "MACAddress",
        "ActiveTime", "InactiveTime", "SNTPTime", "OnlineTimes",
        "BytesSend", "BytesReceived", "UsbandWidth", "DsbandWidth"
    }
    if order_by not in allowed_order_fields:
        order_by = "record_time"
    order_dir = "DESC" if order_dir == "desc" else "ASC"
    where_clauses = []
    params = []
    for f in filters:
        field = f.get("field")
        op = f.get("op", "eq")
        value = f.get("value")
        if not field or value is None:
            continue
        if field not in allowed_order_fields:
            continue
        if op == "eq":
            where_clauses.append(f"{field} = ?")
            params.append(value)
        elif op == "like":
            where_clauses.append(f"{field} LIKE ?")
            params.append(f"%{value}%")
    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
    count_sql = f"SELECT COUNT(*) FROM device_history WHERE {where_sql}"
    offset = (page - 1) * page_size
    query_sql = f"""
        SELECT id, record_time, DevName, IPAddress, MACAddress,
               ActiveTime, InactiveTime, SNTPTime, OnlineTimes,
               BytesSend, BytesReceived, UsbandWidth, DsbandWidth
        FROM device_history
        WHERE {where_sql}
        ORDER BY {order_by} {order_dir}
        LIMIT ? OFFSET ?
    """
    with sqlite3.connect(DB_FILE) as conn:
        total = conn.execute(count_sql, params).fetchone()[0]
        params.extend([page_size, offset])
        rows = conn.execute(query_sql, params).fetchall()
        rows = conn.execute(query_sql, params).fetchall()
    columns = ["id", "record_time", "DevName", "IPAddress", "MACAddress",
               "ActiveTime", "InactiveTime", "SNTPTime", "OnlineTimes",
               "BytesSend", "BytesReceived", "UsbandWidth", "DsbandWidth"]
    devices = [dict(zip(columns, row)) for row in rows]
    return jsonify({"total": total, "page": page, "page_size": page_size, "data": devices})


# ===== 设备速率查询接口（增强：支持 macs 列表、起止时间、按 sntp_time 升序） =====
@app.route('/api/device-speed', methods=['POST'])
def query_device_speed():
    data = request.get_json() or {}
    # 支持单个 mac（兼容旧版）或 macs 列表
    macs = data.get('macs')
    mac = data.get('mac')
    if macs is None and mac is not None:
        macs = [mac]
    if not macs or not isinstance(macs, list):
        return jsonify({"status": "error", "message": "Provide 'macs' list or 'mac' string"}), 400

    start_sntp_time = data.get('start_sntp_time')
    end_sntp_time = data.get('end_sntp_time')

    # 构建 IN 子句
    placeholders = ','.join(['?'] * len(macs))
    params = macs.copy()
    query = f"""
        SELECT mac, sntp_time, upload_kbps, download_kbps, duration_sec
        FROM device_speed
        WHERE mac IN ({placeholders})
    """
    if start_sntp_time:
        query += " AND sntp_time >= ?"
        params.append(start_sntp_time)
    if end_sntp_time:
        query += " AND sntp_time <= ?"
        params.append(end_sntp_time)
    query += " ORDER BY mac, sntp_time ASC"

    with sqlite3.connect(DB_FILE) as conn:
        rows = conn.execute(query, params).fetchall()

    # 按 MAC 分组
    result = {}
    for row in rows:
        mac_addr = row[0]
        if mac_addr not in result:
            result[mac_addr] = []
        result[mac_addr].append({
            "sntp_time": row[1],
            "upload_kbps": row[2],
            "download_kbps": row[3],
            "duration_sec": row[4]
        })
    return jsonify({"status": "ok", "data": result})


# ===== 设备信息表查询接口 =====
@app.route('/api/device-info', methods=['POST'])
def query_device_info():
    data = request.get_json() or {}
    filters = data.get("filters", [])
    order_by = data.get("order_by", "last_seen")
    order_dir = data.get("order_dir", "desc").lower()
    page = int(data.get("page", 1))
    page_size = int(data.get("page_size", 20))
    allowed_fields = {
        "id", "devname", "ipaddress", "macaddress", "first_seen", "last_seen",
        "active", "sntp_time", "active_time", "offline_time",
        "online_duration_sec", "offline_duration_sec",
        "latest_upload_kbps", "latest_download_kbps",
        "latest_bytes_send", "latest_bytes_received",
        "qos_enabled", "qos_max_upload_kbps", "qos_max_download_kbps",
        "qos_inst_id", "qos_limit_time", "qos_duration_minutes", "qos_is_auto"
    }
    if order_by not in allowed_fields:
        order_by = "last_seen"
    order_dir = "DESC" if order_dir == "desc" else "ASC"
    where_clauses = []
    params = []
    for f in filters:
        field = f.get("field")
        op = f.get("op", "eq")
        value = f.get("value")
        if not field or value is None:
            continue
        if field not in allowed_fields:
            continue
        if op == "eq":
            where_clauses.append(f"{field} = ?")
            params.append(value)
        elif op == "like":
            where_clauses.append(f"{field} LIKE ?")
            params.append(f"%{value}%")
    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
    count_sql = f"SELECT COUNT(*) FROM device_info WHERE {where_sql}"
    offset = (page - 1) * page_size
    query_sql = f"""
        SELECT id, devname, ipaddress, macaddress, first_seen, last_seen,
               active, sntp_time, active_time, offline_time,
               online_duration_sec, offline_duration_sec,
               latest_upload_kbps, latest_download_kbps,
               latest_bytes_send, latest_bytes_received,
               qos_enabled, qos_max_upload_kbps, qos_max_download_kbps,
               qos_inst_id, qos_limit_time, qos_duration_minutes, qos_is_auto
        FROM device_info
        WHERE {where_sql}
        ORDER BY {order_by} {order_dir}
        LIMIT ? OFFSET ?
    """
    with sqlite3.connect(DB_FILE) as conn:
        total = conn.execute(count_sql, params).fetchone()[0]
        params.extend([page_size, offset])
        rows = conn.execute(query_sql, params).fetchall()
        rows = conn.execute(query_sql, params).fetchall()
    devices = []
    for row in rows:
        devices.append({
            "id": row[0], "devname": row[1], "ipaddress": row[2], "macaddress": row[3],
            "first_seen": row[4], "last_seen": row[5], "active": row[6],
            "sntp_time": row[7], "active_time": row[8], "offline_time": row[9],
            "online_duration_sec": row[10], "offline_duration_sec": row[11],
            "latest_upload_kbps": row[12], "latest_download_kbps": row[13],
            "latest_bytes_send": row[14], "latest_bytes_received": row[15],
            "qos_enabled": row[16], "qos_max_upload_kbps": row[17], "qos_max_download_kbps": row[18],
            "qos_inst_id": row[19], "qos_limit_time": row[20], "qos_duration_minutes": row[21],
            "qos_is_auto": row[22]
        })
    return jsonify({"total": total, "page": page, "page_size": page_size, "data": devices})


@app.route('/api/device-info/<int:device_id>', methods=['DELETE'])
def delete_device_info(device_id):
    with db_lock:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.execute("SELECT id FROM device_info WHERE id = ?", (device_id,))
            if not cursor.fetchone():
                return jsonify({"status": "error", "message": "Device not found"}), 404
            conn.execute("DELETE FROM device_info WHERE id = ?", (device_id,))
    return jsonify({"status": "ok", "message": f"Device info with id {device_id} deleted"})


# ===== QoS API 辅助函数 =====
QOS_API_URL = f"{ROUTER_BASE}/?_type=vueData&_tag=localnet_qosdyn_bandwithrule_lua"


def get_session_token(session):
    """从已有 session 获取 _sessionTOKEN，失败抛出异常"""
    resp = session.get(f"{ROUTER_BASE}/?_type=loginsceneData&_tag=login_token_json", timeout=10)
    if resp.status_code != 200:
        raise Exception(f"获取 _sessionTOKEN 失败，HTTP {resp.status_code}")
    data = resp.json()
    token = data.get("_sessionToken", "")
    if not token:
        raise Exception("_sessionTOKEN 为空")
    return token


def qos_api_post(session, form_data):
    """POST 调用 QoS 限速 API，返回响应文本"""
    action = form_data.get("IF_ACTION", "?")
    mac = form_data.get("MACDev", form_data.get("_InstID", "?"))
    log_info(f"[QoS API] POST {action} | mac/inst={mac} | params={form_data}")
    resp = session.post(QOS_API_URL, data=form_data, timeout=10)
    log_info(f"[QoS API] 响应 HTTP {resp.status_code} | body={resp.text}")
    if resp.status_code != 200:
        raise Exception(f"QoS API POST 失败，HTTP {resp.status_code}")
    return resp.text


# ===== QoS 限速设置接口 =====
@app.route('/api/qos-limit', methods=['POST'])
def set_qos_limit():
    """前端限速配置 → 真实调用路由器 QoS API"""
    data = request.get_json() or {}
    mac = data.get('mac')
    enabled = data.get('enabled', False)
    max_upload_kbps = int(data.get('max_upload_kbps', 0) or 0)
    max_download_kbps = int(data.get('max_download_kbps', 0) or 0)
    duration_minutes = int(data.get('duration_minutes', 0) or 0)
    client_ip = get_client_ip()

    if not mac:
        return jsonify({"status": "error", "message": "MAC 地址不能为空"}), 400

    # IP 最后一位 < 10 的设备不允许操作限速（路由器基础设施或特殊设备）
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.execute("SELECT ipaddress FROM device_info WHERE macaddress = ?", (mac,))
        row = cur.fetchone()
    if row and row[0] and is_qos_restricted(row[0]):
        return jsonify({"status": "error", "message": "该设备不允许操作限速"}), 403

    try:
        session, _ = get_effective_cookie_and_session()
        if session is None:
            return jsonify({"status": "error", "message": "无有效Cookie，请先通过浏览器扩展提交Cookie"}), 401
        session_token = get_session_token(session)

        if enabled:
            # ======== 开启 / 修改限速 ========
            # 查询设备已有 _InstID
            with sqlite3.connect(DB_FILE) as conn:
                cur = conn.execute(
                    "SELECT qos_inst_id FROM device_info WHERE macaddress = ?", (mac,))
                row = cur.fetchone()
                existing_inst_id = row[0] if row and row[0] else None

            inst_id = existing_inst_id if existing_inst_id else "-1"
            form_data = {
                "MACDev": mac,
                "BandwithRateMaxDown": str(max_download_kbps),
                "BandwithRateMaxUp": str(max_upload_kbps),
                "UserName": mac,
                "IF_ACTION": "Apply",
                "_InstID": inst_id,
                "_sessionTOKEN": session_token
            }
            post_resp_text = qos_api_post(session, form_data)

            # 从 POST 响应直接解析 _InstID（初次设置时为 IGD.QoSBandwidthRuleN）
            try:
                post_root = ET.fromstring(post_resp_text)
                inst_id_el = post_root.find("_InstID")
                real_inst_id = inst_id_el.text if (inst_id_el is not None and inst_id_el.text) else existing_inst_id
            except Exception:
                real_inst_id = existing_inst_id

            log_info(
                f"[{client_ip}] QoS Apply: MAC={mac}, up={max_upload_kbps}Kbps, down={max_download_kbps}Kbps, instID={real_inst_id}")

            # 获取设备当前 SNTPTime 作为限速时刻
            timestamp = int(time.time() * 1000)
            lan_resp = session.get(
                f"{ROUTER_BASE}/?_type=vueData&_tag=localnet_lan_info_lua&_={timestamp}",
                timeout=10)
            limit_time = None
            if lan_resp.status_code == 200:
                lan_root = ET.fromstring(lan_resp.text)
                for inst in lan_root.findall(".//OBJ_LAN_INFO_ID/Instance"):
                    dev_info = {}
                    for pn, pv in zip(inst.findall("ParaName"), inst.findall("ParaValue")):
                        dev_info[pn.text] = pv.text if pv.text is not None else ""
                    if dev_info.get("MACAddress") == mac:
                        limit_time = normalize_sntp_time(dev_info.get("SNTPTime", ""))
                        break

            # 更新数据库
            with db_lock:
                with sqlite3.connect(DB_FILE) as conn:
                    conn.execute('''
                        UPDATE device_info
                        SET qos_enabled = 1,
                            qos_max_upload_kbps = ?, qos_max_download_kbps = ?,
                            qos_inst_id = ?, qos_limit_time = ?, qos_duration_minutes = ?,
                            qos_is_auto = 0
                        WHERE macaddress = ?
                    ''', (max_upload_kbps, max_download_kbps,
                          real_inst_id, limit_time, duration_minutes, mac))

            log_info(
                f"[{client_ip}] QoS限速已生效: MAC={mac}, instID={real_inst_id}, "
                f"limit_time={limit_time}, duration={duration_minutes}min"
            )
            return jsonify({"status": "ok", "message": "限速设置成功"})

        else:
            # ======== 关闭限速 ========
            qos_deleting_set.add(mac)  # 内存标记，防止采集线程重新填充 QoS

            with sqlite3.connect(DB_FILE) as conn:
                cur = conn.execute(
                    "SELECT qos_inst_id FROM device_info WHERE macaddress = ?", (mac,))
                row = cur.fetchone()
                qos_inst_id = row[0] if row and row[0] else None

            if qos_inst_id:
                form_data = {
                    "IF_ACTION": "Delete",
                    "_InstID": qos_inst_id,
                    "_sessionTOKEN": session_token
                }
                qos_api_post(session, form_data)
                log_info(f"[{client_ip}] QoS Delete: MAC={mac}, instID={qos_inst_id}")

            # 清除数据库 QoS 相关字段
            with db_lock:
                with sqlite3.connect(DB_FILE) as conn:
                    conn.execute('''
                        UPDATE device_info
                        SET qos_enabled = 0,
                            qos_inst_id = NULL, qos_limit_time = NULL,
                            qos_duration_minutes = 0, qos_is_auto = 0
                        WHERE macaddress = ?
                    ''', (mac,))

            log_info(f"[{client_ip}] QoS限速已解除: MAC={mac}，标记待采集确认")
            return jsonify({"status": "ok", "message": "限速已解除"})

    except Exception as e:
        log_warning(f"[{client_ip}] QoS限速设置失败: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# ===== 批量解除 QoS 限速 =====
@app.route('/api/qos-batch-delete', methods=['GET'])
def batch_delete_qos():
    """接收 macs=all 或 macs=mac1,mac2 批量解除限速"""
    client_ip = get_client_ip()
    macs_param = request.args.get('macs', '')

    if macs_param == 'all':
        with sqlite3.connect(DB_FILE) as conn:
            rows = conn.execute(
                "SELECT macaddress, qos_inst_id FROM device_info "
                "WHERE qos_enabled = 1 AND qos_inst_id IS NOT NULL"
            ).fetchall()
        mac_list = [(row[0], row[1]) for row in rows]
    else:
        macs = [m.strip() for m in macs_param.split(',') if m.strip()]
        if not macs:
            return jsonify({"status": "error", "message": "macs 不能为空"}), 400
        with sqlite3.connect(DB_FILE) as conn:
            placeholders = ','.join(['?'] * len(macs))
            rows = conn.execute(
                f"SELECT macaddress, qos_inst_id, ipaddress FROM device_info "
                f"WHERE macaddress IN ({placeholders}) AND qos_enabled = 1 AND qos_inst_id IS NOT NULL",
                macs
            ).fetchall()
        # 过滤受限制 IP 的设备（永久限速，不可解除）
        mac_list = []
        for row in rows:
            if not is_qos_restricted(row[2] or ''):
                mac_list.append((row[0], row[1]))

    if not mac_list:
        return jsonify({"status": "ok", "message": "没有需要解除的限速", "count": 0})

    # 内存标记所有目标设备为"正在删除"，防止采集线程重新填充 QoS
    for mac, _ in mac_list:
        qos_deleting_set.add(mac)

    session, _ = get_effective_cookie_and_session()
    if session is None:
        for mac, _ in mac_list:
            qos_deleting_set.discard(mac)
        return jsonify({"status": "error", "message": "无有效Cookie，请先通过浏览器扩展提交Cookie"}), 401

    success = 0
    failed = 0
    for mac, qos_inst_id in mac_list:
        try:
            # 每次 Delete 需重新获取 _sessionTOKEN（路由器只允许一次性使用）
            session_token = get_session_token(session)
            form_data = {
                "IF_ACTION": "Delete",
                "_InstID": qos_inst_id,
                "_sessionTOKEN": session_token
            }
            qos_api_post(session, form_data)
            with db_lock:
                with sqlite3.connect(DB_FILE) as conn:
                    conn.execute(
                        "UPDATE device_info SET qos_enabled=0, qos_inst_id=NULL, "
                        "qos_limit_time=NULL, qos_duration_minutes=0, qos_is_auto=0 "
                        "WHERE macaddress=?",
                        (mac,))
            success += 1
            log_info(f"[{client_ip}] 批量解除限速: MAC={mac}, instID={qos_inst_id}")
        except Exception as e:
            failed += 1
            log_warning(f"[{client_ip}] 批量解除限速失败: MAC={mac}, {e}")

    log_info(f"[{client_ip}] 批量解除完成: 成功{success}, 失败{failed}")
    return jsonify({"status": "ok", "message": f"成功{success}个，失败{failed}个", "success": success, "failed": failed})


# ---------- 定时任务调度 ----------
# schedule 库的执行策略是"跑完再等 N 秒"，
# 如果 fetch_and_process 本身耗时 ~5s，实际间隔会变成 ~10s。
# 改用固定周期循环：补偿执行耗时，保证 FETCH_INTERVAL_SECONDS 真实间隔。
def run_schedule():
    while True:
        start = time.time()
        fetch_and_process()
        elapsed = time.time() - start
        sleep_time = max(0.5, FETCH_INTERVAL_SECONDS - elapsed)
        time.sleep(sleep_time)


if __name__ == '__main__':
    # 关闭 Flask / Werkzeug 的 access 日志
    import logging

    logging.getLogger('werkzeug').setLevel(logging.WARNING)

    init_db()
    fetch_and_process()
    threading.Thread(target=run_schedule, daemon=True).start()


    # 数据清理仍用 schedule（每天一次，单独线程运行）
    def run_cleanup():
        while True:
            schedule.run_pending()
            time.sleep(60)  # 每分钟检查一次即可


    schedule.every().day.at("02:00").do(cleanup_old_data)
    check_data_collection_stalled()  # 启动后立即执行一次
    schedule.every(5).minutes.do(check_data_collection_stalled)
    threading.Thread(target=run_cleanup, daemon=True).start()
    app.run(host='0.0.0.0', port=5000, debug=False)
