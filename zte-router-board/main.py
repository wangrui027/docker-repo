import hashlib
import json
import os
import pickle
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
SESSION_FILE = "router_self_session.pkl"

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
LOG_LEVEL = "INFO"  # "INFO" 或 "WARNING"

# ===== 数据库配置 =====
DB_FILE = "data/data.db"
db_lock = threading.Lock()  # 数据库写入锁
qos_deleting_set = set()  # 正在解除限速的设备 MAC 集合（内存标记）

# =====================

# =============================================

# 日志工具
def log_info(msg):
    if LOG_LEVEL == "INFO":
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [INFO] {msg}")


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


def normalize_sntp_time(sntp_str):
    """将 SNTPTime 字符串中的 'T' 替换为空格，统一为 'YYYY-MM-DD HH:MM:SS' 格式"""
    if not sntp_str:
        return None
    return sntp_str.replace('T', ' ')


def upsert_device_info(device_info, active, offline_time_str, active_time_str, sntp_time_str, latest_send, latest_recv):
    """
    插入或更新设备信息表（基于 MAC 地址），同时更新最新流量累计值
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

    with sqlite3.connect(DB_FILE) as conn:
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
                    qos_inst_id = ?, qos_limit_time = ?, qos_duration_minutes = ?
                WHERE macaddress = ?
            ''', (devname, ip, now_str, active,
                  sntp_time_str, final_active, final_offline,
                  online_dur, offline_dur,
                  latest_send, latest_recv,
                  qos_enabled, qos_max_up, qos_max_down,
                  qos_inst_id, qos_limit_time, qos_duration_minutes, mac))
        else:
            conn.execute('''
                INSERT INTO device_info (
                    devname, ipaddress, macaddress, first_seen, last_seen,
                    active, sntp_time, active_time, offline_time,
                    online_duration_sec, offline_duration_sec,
                    latest_upload_kbps, latest_download_kbps,
                    latest_bytes_send, latest_bytes_received,
                    qos_enabled, qos_max_upload_kbps, qos_max_download_kbps,
                    qos_inst_id, qos_limit_time, qos_duration_minutes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (devname, ip, mac, now_str, now_str,
                  active, sntp_time_str, final_active, final_offline,
                  online_dur, offline_dur, None, None,
                  latest_send, latest_recv,
                  qos_enabled, qos_max_up, qos_max_down,
                  qos_inst_id, qos_limit_time, qos_duration_minutes))


def update_devices_offline(matched_macs):
    if not matched_macs:
        return
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    placeholders = ','.join(['?'] * len(matched_macs))
    with sqlite3.connect(DB_FILE) as conn:
        query = f'''
            UPDATE device_info
            SET offline_time = ?, latest_upload_kbps = NULL, latest_download_kbps = NULL
            WHERE macaddress NOT IN ({placeholders}) AND offline_time IS NULL
        '''
        params = [now_str] + matched_macs
        conn.execute(query, params)


def insert_device_record(device_info):
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

    with db_lock:
        with sqlite3.connect(DB_FILE) as conn:
            upsert_device_info(device_info, active, inactive_time, active_time, sntp_time, bytes_send, bytes_recv)

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


# =============================================

# 全局变量
plugin_cookie = None
self_session = None
self_cookie_valid = False
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


def save_self_session(session):
    with open(SESSION_FILE, "wb") as f:
        pickle.dump(session.cookies, f)


def load_self_session():
    if Path(SESSION_FILE).exists():
        session = requests.Session()
        with open(SESSION_FILE, "rb") as f:
            cookies = pickle.load(f)
            session.cookies.update(cookies)
        return session
    return None


def self_login():
    log_info("执行自主登录...")
    session = requests.Session()
    session.get(f"{ROUTER_BASE}/")
    resp = session.get(f"{ROUTER_BASE}/?_type=loginsceneData&_tag=login_token_json")
    data = resp.json()
    logintoken = data["logintoken"]
    session_token = data["_sessionToken"]
    password_hash = hashlib.sha256((PASSWORD + logintoken).encode()).hexdigest()
    payload = {
        "Username": "admin",
        "Password": password_hash,
        "action": "login",
        "Frm_Logintoken": "",
        "captchaCode": "",
        "_sessionTOKEN": session_token
    }
    resp = session.post(f"{ROUTER_BASE}/?_type=loginData&_tag=login_entry", data=payload)
    if resp.status_code != 200:
        raise Exception("自主登录失败，HTTP状态码: " + str(resp.status_code))
    save_self_session(session)
    return session


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


def reset_valid_cache():
    global last_valid_cookie, last_valid_time
    last_valid_cookie = None
    last_valid_time = 0


def get_effective_cookie_and_session():
    global plugin_cookie, self_session, self_cookie_valid, last_valid_cookie, last_valid_time
    with lock:
        if plugin_cookie and plugin_cookie not in invalid_cookies:
            test_session = requests.Session()
            test_session.cookies.update(parse_cookie_string(plugin_cookie))
            if check_session_valid(test_session):
                return test_session, 'plugin'
            else:
                log_warning("插件Cookie失效，已清除")
                invalid_cookies.add(plugin_cookie)
                plugin_cookie = None
                reset_valid_cache()
        if self_session is None:
            self_session = load_self_session()
        if self_session:
            if check_session_valid(self_session):
                self_cookie_valid = True
                return self_session, 'self'
            else:
                log_warning("持久化Session失效")
                self_session = None
                self_cookie_valid = False
                reset_valid_cache()
        log_warning("所有Cookie失效，执行自主登录...")
        new_session = self_login()
        self_session = new_session
        self_cookie_valid = True
        reset_valid_cache()
        return new_session, 'new_login'


# ---------- 数据采集任务 ----------
def fetch_and_process():
    global last_used_source, last_valid_cookie, last_valid_time, plugin_cookie
    if not task_lock.acquire(timeout=5):
        log_info("采集任务被Cookie验证占用，本次跳过")
        return
    try:
        session, source = get_effective_cookie_and_session()
        if source != last_used_source:
            log_warning(f"会话切换为 {source}")
            last_used_source = source
        timestamp = int(time.time() * 1000)
        xml_url = f"{ROUTER_BASE}/?_type=vueData&_tag=localnet_lan_info_lua&_={timestamp}"
        response = session.get(xml_url, timeout=10)
        xml_content = response.text
        root = ET.fromstring(xml_content)

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
                    # XML 中已无此限速规则 → 路由器已同步，清除标记
                    if mac_addr not in qos_map:
                        qos_deleting_set.discard(mac_addr)
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

        # ===== 自动解除过期 QoS 限速 =====
        with sqlite3.connect(DB_FILE) as conn:
            expired = conn.execute(
                "SELECT macaddress, qos_inst_id, qos_limit_time, qos_duration_minutes "
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

            for mac, qos_inst_id, limit_time_str, dur_min in expired:
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
                                        "qos_limit_time=NULL, qos_duration_minutes=0 WHERE macaddress=?",
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
        #     log_info(f"已保存快照: {filepath}（{len(matched_devices)} 台设备）")

        for device in matched_devices:
            insert_device_record(device)
        if matched_macs:
            with db_lock:
                update_devices_offline(matched_macs)
        if source == 'plugin':
            with lock:
                last_valid_time = time.time()
                if plugin_cookie:
                    last_valid_cookie = plugin_cookie
    except Exception as e:
        log_warning(f"采集失败: {e}")
    finally:
        task_lock.release()


@app.route('/')
def dashboard():
    return render_template('index.html', fetch_interval=FETCH_INTERVAL_SECONDS)


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
    with sqlite3.connect(DB_FILE) as conn:
        total = conn.execute(count_sql, params).fetchone()[0]
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
    params.extend([page_size, offset])
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
        "qos_inst_id", "qos_limit_time", "qos_duration_minutes"
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
    with sqlite3.connect(DB_FILE) as conn:
        total = conn.execute(count_sql, params).fetchone()[0]
    offset = (page - 1) * page_size
    query_sql = f"""
        SELECT id, devname, ipaddress, macaddress, first_seen, last_seen,
               active, sntp_time, active_time, offline_time,
               online_duration_sec, offline_duration_sec,
               latest_upload_kbps, latest_download_kbps,
               latest_bytes_send, latest_bytes_received,
               qos_enabled, qos_max_upload_kbps, qos_max_download_kbps,
               qos_inst_id, qos_limit_time, qos_duration_minutes
        FROM device_info
        WHERE {where_sql}
        ORDER BY {order_by} {order_dir}
        LIMIT ? OFFSET ?
    """
    params.extend([page_size, offset])
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
            "qos_inst_id": row[19], "qos_limit_time": row[20], "qos_duration_minutes": row[21]
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
    resp = session.post(QOS_API_URL, data=form_data, timeout=10)
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

    try:
        session, _ = get_effective_cookie_and_session()
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

            log_info(f"[{client_ip}] QoS Apply: MAC={mac}, up={max_upload_kbps}Kbps, down={max_download_kbps}Kbps, instID={real_inst_id}")

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
                            qos_inst_id = ?, qos_limit_time = ?, qos_duration_minutes = ?
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
                            qos_duration_minutes = 0
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
                f"SELECT macaddress, qos_inst_id FROM device_info "
                f"WHERE macaddress IN ({placeholders}) AND qos_enabled = 1 AND qos_inst_id IS NOT NULL",
                macs
            ).fetchall()
        mac_list = [(row[0], row[1]) for row in rows]

    if not mac_list:
        return jsonify({"status": "ok", "message": "没有需要解除的限速", "count": 0})

    # 内存标记所有目标设备为"正在删除"，防止采集线程重新填充 QoS
    for mac, _ in mac_list:
        qos_deleting_set.add(mac)

    try:
        session, _ = get_effective_cookie_and_session()
        session_token = get_session_token(session)
    except Exception as e:
        for mac, _ in mac_list:
            qos_deleting_set.discard(mac)
        return jsonify({"status": "error", "message": f"登录失败: {e}"}), 500

    success = 0
    failed = 0
    for mac, qos_inst_id in mac_list:
        try:
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
                        "qos_limit_time=NULL, qos_duration_minutes=0 "
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
def run_schedule():
    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == '__main__':
    # 关闭 Flask / Werkzeug 的 access 日志
    import logging
    logging.getLogger('werkzeug').setLevel(logging.WARNING)

    init_db()
    fetch_and_process()
    schedule.every(FETCH_INTERVAL_SECONDS).seconds.do(fetch_and_process)
    schedule.every().day.at("02:00").do(cleanup_old_data)
    threading.Thread(target=run_schedule, daemon=True).start()
    app.run(host='0.0.0.0', port=5000, debug=False)
