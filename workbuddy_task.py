"""WorkBuddy 成长任务一键完成脚本（workbuddy2api-panel-main 的 Python 重构）。

参考 workbuddy_signin.py 获取本机 WorkBuddy 桌面端的登录会话（凭据文件
CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info），把面板
「成长任务一键完成（17/18）」的核心逻辑移植为可按数字选择执行的命令行脚本：
每个任务对应一个编号，输入编号即执行对应任务的「动作 → 回读 → 自动领奖」闭环。

WorkBuddy 较新版本（v5.9.0+ 已确认）会把本地凭据写成 $wbEncrypted 信封；
推荐先运行 login 通过浏览器登录换取 Bearer Token 并加密缓存，脚本同时保留
桌面端凭据与内存解密作为兜底。代码中不硬编码任何 Token、Cookie 或密钥。

端点（Go 代码实测口径）：
  - chat 域  https://copilot.tencent.com   growth/tasks/buddy/streak/chat/桌面指纹report
  - billing 域 https://www.codebuddy.cn     /v2/report（对话活跃上报 / 小程序指纹上报）
  - web 域   https://www.workbuddy.cn      任务领奖（Web 成长中心）/ web 指纹 report

语义要点（与 Go 侧一致）：
  - accept 是「报名」不产生进度；进度由服务端行为事件点亮，故动作幂等可重放；
  - 上游计分为异步：动作后有界轮询回读（4 次 × 3s），达标即自动领奖；
  - mp 口径任务（school_season / Sequential_Tasks_1..7）列表、accept、claim
    全链路要求 X-Client-Platform: miniprogram，默认列表不下发。

用法：
  python workbuddy_task.py            # 打印任务菜单，循环输入数字（可多个/区间）执行
  python workbuddy_task.py 1 3 5      # 直接执行第 1、3、5 项
  python workbuddy_task.py 1-10 17    # 区间 + 单个混合
  python workbuddy_task.py all        # 全部可自动化任务（先批量 accept）
  python workbuddy_task.py list       # 只列出任务清单
  python workbuddy_task.py chat_5     # 直接按任务 code 执行
  python workbuddy_task.py login      # 弹窗浏览器登录，自动抓取 Token/Cookie 并缓存
  python workbuddy_task.py status     # 查看登录缓存和过期时间
  python workbuddy_task.py logout     # 删除登录缓存
"""

import base64
import getpass
import hashlib
import json
import os
import re
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

# ---------------------------------------------------------------------------
# 常量（对齐 Go 侧 upstream 包）
# ---------------------------------------------------------------------------

AUTH_BASENAME = os.path.join("CodeBuddyExtension", "Data", "Public", "auth", "workbuddy-desktop.info")

CHAT_BASE = "https://copilot.tencent.com"    # growth / tasks / buddy / chat / 桌面 report
BILLING_BASE = "https://www.codebuddy.cn"    # 对话活跃上报 / 小程序指纹 report
WEB_BASE = "https://www.workbuddy.cn"        # Web 成长中心领奖 / web 指纹 report

PATH_LIST_TASKS = "/v2/activity/growth/tasks"
PATH_ACCEPT_TASKS = "/v2/activity/growth/tasks/accept"
PATH_ACCEPT_TASKS_WEB = "/activity/growth/tasks/accept"
PATH_REPORT = "/v2/report"
PATH_CHAT = "/v2/chat/completions"

GROWTH_CENTER_URL = WEB_BASE + "/profile/growth-center"
AUTH_CACHE_DIR = os.environ.get("WORKBUDDY_CACHE_DIR") or os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.local/share"),
    "workbuddy-task-auto")
AUTH_CACHE_FILE = os.path.join(AUTH_CACHE_DIR, "session.dat")

DESKTOP_UA = "WorkBuddy/5.5.6 WorkBuddy/5.5.6 CLI/2.137.1"
CLIENT_UA = "WorkBuddy/5.5.4 WorkBuddy/5.5.4 CLI/2.137.1"
BILLING_UA = "WorkBuddy/5.5.4"
MP_PLATFORM = "miniprogram"

# 节流与轮询（对齐 Go 常量）
REPORT_GAP = 1.05      # 连续上报间隔（1.05s 实测口径）
MP_GAP = 2.0           # mp 任务写动作间隔
CLAIM_POLL_ATTEMPTS = 4   # 异步计分有界轮询次数
CLAIM_POLL_GAP = 3.0      # 轮询间隔（秒）
EXPERT_GAP = 6.0       # 专家召唤链间隔（v11 实测 8s 内 100%）

SSL_CTX = ssl.create_default_context()

# mp 口径专属任务码（Go autotask.go mpTaskCodes）
MP_TASK_CODES = {
    "school_season",
    "Sequential_Tasks_1", "Sequential_Tasks_2", "Sequential_Tasks_3",
    "Sequential_Tasks_4", "Sequential_Tasks_5", "Sequential_Tasks_6",
    "Sequential_Tasks_7",
}

BUDDY_TASK_INCOMPLETE = "first_buddy task not completed yet"

# 服务端 requestId 形状（cmb- 前缀 32hex 或裸 32hex）
SERVER_ID_RE = re.compile(r"^(cmb-)?[0-9a-f]{32}$")


# ---------------------------------------------------------------------------
# 日志与通用工具
# ---------------------------------------------------------------------------

def log(msg):
    """带时间戳的进度日志（UTF-8 安全：GBK 控制台不会崩）。"""
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        # 旧版 Windows 控制台是 GBK；用 GBK 替换不可编码字符，避免二次抛错。
        try:
            print(line.encode("gbk", "replace").decode("gbk"), flush=True)
        except Exception:
            print(line.encode("ascii", "replace").decode("ascii"), flush=True)


def now_ms():
    return int(time.time() * 1000)


def brief(obj, limit=300):
    """压缩响应对象为短文本，避免刷屏。"""
    try:
        text = json.dumps(obj, ensure_ascii=False) if obj is not None else "null"
    except (TypeError, ValueError):
        text = str(obj)
    return text if len(text) <= limit else text[:limit] + "…"


def derive_id(uid, salt):
    """由 uid 稳定派生 36 hex 设备标识（同 Go deriveID：sha256(salt:uid) 前 18 字节）。"""
    return hashlib.sha256((salt + ":" + uid).encode("utf-8")).hexdigest()[:36]


def _disp_width(text):
    """按东亚全角宽度计算字符串显示宽度（中文对齐用）。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in text)


def _ljust(text, width):
    """按显示宽度左对齐补齐空格（中文不错位）。"""
    return text + " " * max(0, width - _disp_width(text))


def _rjust(text, width):
    """按显示宽度右对齐补齐空格。"""
    return " " * max(0, width - _disp_width(text)) + text


def _trunc(text, width):
    """按显示宽度截断，超长以省略号结尾。"""
    if _disp_width(text) <= width:
        return text
    out, w = "", 0
    for ch in text:
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if w + cw > width - 1:
            break
        out += ch
        w += cw
    return out + "…"


# ---------------------------------------------------------------------------
# 桌面端登录会话（照抄 workbuddy_signin.py）
# ---------------------------------------------------------------------------

def find_auth_file():
    """按平台探测 WorkBuddy 桌面端写出的登录凭据文件，支持环境变量覆盖。"""
    override = os.environ.get("WORKBUDDY_AUTH_FILE")
    if override:
        return override
    home = os.path.expanduser("~")
    local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    candidates = [
        os.path.join(local, AUTH_BASENAME),                                  # Windows
        os.path.join(home, "Library", "Application Support", AUTH_BASENAME),  # macOS
        os.path.join(home, ".config", AUTH_BASENAME),                        # Linux
        os.path.join(home, ".workbuddy", "auth", "workbuddy-desktop.info"),  # 兜底
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def load_session(auth_file):
    with open(auth_file, "r", encoding="utf-8") as f:
        return json.load(f)


def extract_at_rest_key_from_windows_processes(key_id_target=None):
    """Windows 下从运行中的 WorkBuddy.exe 进程内存中搜索 atRestSecretKey。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        import subprocess

        kernel32 = ctypes.windll.kernel32

        class MEMORY_BASIC_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BaseAddress", ctypes.c_void_p),
                ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wintypes.DWORD),
                ("PartitionId", wintypes.WORD),
                ("RegionSize", ctypes.c_size_t),
                ("State", wintypes.DWORD),
                ("Protect", wintypes.DWORD),
                ("Type", wintypes.DWORD),
            ]

        PROCESS_VM_READ = 0x0010
        PROCESS_QUERY_INFORMATION = 0x0400
        MEM_COMMIT = 0x1000
        PAGE_READWRITE = 0x04
        PAGE_READONLY = 0x02

        output = subprocess.check_output(
            'tasklist /FI "IMAGENAME eq WorkBuddy.exe" /FO CSV',
            shell=True
        ).decode("gbk", "ignore")
        lines = [l.strip() for l in output.splitlines() if l.strip()]
        pids = []
        for line in lines[1:]:
            parts = [p.strip('"') for p in line.split('","')]
            if len(parts) >= 2:
                try:
                    pids.append(int(parts[1]))
                except ValueError:
                    pass

        if not pids:
            return None

        pattern = re.compile(rb'"atRestSecretKey"\s*:\s*"([A-Za-z0-9+/]{43}=)"')

        for pid in pids:
            hProcess = kernel32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
            if not hProcess:
                continue
            try:
                address = 0
                mbi = MEMORY_BASIC_INFORMATION()
                while kernel32.VirtualQueryEx(hProcess, ctypes.c_void_p(address), ctypes.byref(mbi), ctypes.sizeof(mbi)):
                    if mbi.State == MEM_COMMIT and (mbi.Protect & (PAGE_READWRITE | PAGE_READONLY)):
                        size = mbi.RegionSize
                        if size <= 64 * 1024 * 1024:
                            buf = ctypes.create_string_buffer(size)
                            bytesRead = ctypes.c_size_t()
                            if kernel32.ReadProcessMemory(hProcess, ctypes.c_void_p(address), buf, size, ctypes.byref(bytesRead)):
                                data = bytes(buf.raw[:bytesRead.value])
                                for m in pattern.finditer(data):
                                    sec = m.group(1).decode("ascii")
                                    n = hashlib.sha256(sec.encode("utf-8")).digest()
                                    kid = hashlib.sha256(n).hexdigest()[:16]
                                    if not key_id_target or kid == key_id_target:
                                        return sec
                    address += mbi.RegionSize
                    if address >= 0x7FFFFFFF0000:
                        break
            finally:
                kernel32.CloseHandle(hProcess)
    except Exception:
        pass
    return None


def try_decrypt_at_rest_token(enc_obj):
    """当本地凭据被 $wbEncrypted 加密时，尝试从内存或环境变量提取密钥并解密明文 Token。"""
    if not isinstance(enc_obj, dict) or not enc_obj.get("envelope"):
        return None
    try:
        env_raw = base64.b64decode(enc_obj["envelope"])
        env_json = json.loads(env_raw)
        key_id = env_json.get("keyId")
    except Exception:
        return None

    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        log("提示: 解密 WorkBuddy 凭据需要 cryptography 库 (pip install cryptography)")
        log("     也可以运行 python workbuddy_task.py login 走浏览器登录缓存。")
        return None

    secret = os.environ.get("WORKBUDDY_ATREST_KEY")
    if not secret and os.environ.get("WORKBUDDY_DISABLE_MEMORY_SCAN") != "1":
        secret = extract_at_rest_key_from_windows_processes(key_id)

    if not secret:
        return None

    try:
        nonce = base64.b64decode(env_json["nonce"])
        auth_tag = base64.b64decode(env_json["authTag"])
        ciphertext = base64.b64decode(env_json["ciphertext"])

        aad = b"".join([
            b"WB-AAD\0",
            b"\x01",
            (5).to_bytes(4, "big") + b"WBEV1",
            (6).to_bytes(4, "big") + b"sym-v1",
            (1).to_bytes(4, "big"),
            (16).to_bytes(4, "big") + key_id.encode("ascii"),
            b"\x02\x00\x00"
        ])

        aes_key = hashlib.sha256(secret.encode("utf-8")).digest()
        aesgcm = AESGCM(aes_key)
        plaintext = aesgcm.decrypt(nonce, ciphertext + auth_tag, aad)
        return plaintext.decode("utf-8")
    except Exception as e:
        log("自动解密凭据失败: %s" % e)
        return None


# ---------------------------------------------------------------------------
# 登录缓存（浏览器弹窗登录 / DPAPI 加密）
# ---------------------------------------------------------------------------

def decode_jwt_payload(token):
    """不校验签名，仅解出 JWT payload 用于读取 exp/sub。"""
    if not isinstance(token, str):
        return {}
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    try:
        pad = "=" * (-len(parts[1]) % 4)
        raw = base64.urlsafe_b64decode(parts[1] + pad)
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def jwt_expiry_ms(token):
    exp = decode_jwt_payload(token).get("exp")
    try:
        return int(float(exp) * 1000)
    except (TypeError, ValueError):
        return 0


def jwt_uid(token):
    payload = decode_jwt_payload(token)
    return payload.get("sub") or payload.get("user_id") or payload.get("uid") or ""


def format_ts(ms):
    if not ms:
        return "未知"
    try:
        return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return "未知"


def remaining_text(ms):
    if not ms:
        return "未知"
    diff = ms - now_ms()
    if diff <= 0:
        return "已过期"
    if diff >= 86400000:
        return "%.1f 天" % (diff / 86400000)
    return "%.0f 小时" % (diff / 3600000)


def _cookie_header(cookies):
    return "; ".join("%s=%s" % (c.get("name"), c.get("value"))
                     for c in cookies if c.get("name"))


def _cookie_expiry_ms(cookies):
    preferred = [c for c in cookies
                 if c.get("name") in ("session", "session_2", "KEYCLOAK_SESSION")]
    values = []
    for c in (preferred or cookies):
        exp = c.get("expires")
        if isinstance(exp, (int, float)) and exp > 0:
            values.append(int(exp * 1000))
    return min(values) if values else 0


def _filter_auth_cookies(cookies):
    return [c for c in cookies
            if "workbuddy.cn" in (c.get("domain") or "")
            or "codebuddy.cn" in (c.get("domain") or "")]


def _http_json(method, url, headers, payload=None, timeout=20):
    """轻量 HTTP JSON 请求，返回 (status, obj, headers)。"""
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                obj = json.loads(raw) if raw else {}
            except Exception:
                obj = {"raw": raw[:500]}
            return resp.status, obj, dict(resp.headers)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            obj = json.loads(raw) if raw else {}
        except Exception:
            obj = {"raw": raw[:500]}
        return e.code, obj, dict(e.headers)
    except Exception as e:
        return -1, {"error": str(e)}, {}


def _token_headers(token):
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/json",
        "X-CodeBuddy-Request": "1",
        "User-Agent": BILLING_UA,
    }
    uid = jwt_uid(token)
    if uid:
        headers["X-User-Id"] = uid
    return headers


def _cookie_headers(cookies):
    return {
        "Cookie": _cookie_header(cookies),
        "Accept": "application/json",
        "x-client-platform": "web",
        "Referer": GROWTH_CENTER_URL,
        "User-Agent": CLIENT_UA,
    }


def validate_token(token):
    """验证 Bearer Token 是否能正常读取成长任务，返回 (ok, uid, detail)。"""
    uid = jwt_uid(token)
    last_status = 0
    for platform in (None, "miniprogram", "web"):
        headers = _token_headers(token)
        if platform:
            headers["X-Client-Platform"] = platform
        status, obj, resp_headers = _http_json("GET", CHAT_BASE + PATH_LIST_TASKS,
                                                headers, timeout=10)
        last_status = status
        if status == 200 and isinstance(obj, dict) and obj.get("code") in (0, "0", None):
            uid = uid or resp_headers.get("x-user-id") or resp_headers.get("X-User-Id") or ""
            return True, uid, "HTTP %s" % status
    return False, uid, "HTTP %s" % last_status


def validate_cookies(cookies):
    """验证 Web 登录 Cookie 是否能正常读取成长任务，返回 (ok, uid, detail)。"""
    status, obj, headers = _http_json("GET", WEB_BASE + PATH_LIST_TASKS,
                                      _cookie_headers(cookies), timeout=10)
    ok = status == 200 and isinstance(obj, dict) and obj.get("code") in (0, "0", None)
    uid = headers.get("x-user-id") or headers.get("X-User-Id") or ""
    return ok, uid, "HTTP %s" % status


def _plugin_token_from_cookies(cookies, state):
    """用 Web 登录会话尝试换取桌面端 Bearer Token（失败返回空串）。

    state 完成后 token 接口本身不需要 Cookie；先不带 Cookie 请求，
    避免旧/无效 Cookie 干扰，失败再带上 Cookie 重试。
    """
    if not state:
        return ""
    url = WEB_BASE + "/v2/plugin/auth/token?state=" + urllib.parse.quote(str(state))
    bare = {
        "Accept": "application/json",
        "X-No-Authorization": "true",
        "User-Agent": CLIENT_UA,
    }
    with_cookie = _cookie_headers(cookies or [])
    with_cookie["X-No-Authorization"] = "true"
    for headers in (bare, with_cookie):
        status, obj, _ = _http_json("GET", url, headers, timeout=10)
        if status != 200 or not isinstance(obj, dict):
            continue
        data = obj.get("data") if isinstance(obj.get("data"), dict) else obj
        for key in ("access_token", "accessToken", "token"):
            token = data.get(key) or obj.get(key)
            if isinstance(token, str) and token.count(".") == 2 and len(token) > 50:
                return token
    return ""


def _exchange_token_from_cookies(cookies):
    """用已登录的 Web 会话走 plugin auth state 流程，换取桌面端 Bearer Token。

    流程：
      1. POST /v2/plugin/auth/state?platform=workbuddy  -> state
      2. GET  /console/auth/login?...&state=...          -> 完成该 state
      3. GET  /v2/plugin/auth/token?state=...            -> accessToken
    返回 (token, uid)；失败返回 ("", "")。
    """
    if not cookies:
        return "", ""
    headers = _cookie_headers(cookies)
    headers["Content-Type"] = "application/json"
    headers["X-No-Authorization"] = "true"
    headers["X-Domain"] = "www.workbuddy.cn"
    status, obj, _ = _http_json(
        "POST", WEB_BASE + "/v2/plugin/auth/state?platform=workbuddy",
        headers, payload={}, timeout=10)
    if status != 200 or not isinstance(obj, dict):
        return "", ""
    data = obj.get("data") if isinstance(obj.get("data"), dict) else obj
    state = data.get("state") or obj.get("state") or ""
    if not state:
        return "", ""
    login_url = (WEB_BASE + "/console/auth/login?platform=workbuddy&state="
                 + urllib.parse.quote(str(state)) + "&domain=www.workbuddy.cn")
    _http_json("GET", login_url, headers, timeout=10)
    token = _plugin_token_from_cookies(cookies, state)
    if not token:
        return "", ""
    ok, uid, _ = validate_token(token)
    if not ok:
        return "", ""
    return token, uid


def _dpapi(data, protect):
    """Windows DPAPI 加/解密；非 Windows 或失败返回 None。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p

        buf = ctypes.create_string_buffer(data, len(data))
        blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        blob_out = DATA_BLOB()
        if protect:
            crypt32.CryptProtectData.argtypes = [
                ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR,
                ctypes.POINTER(DATA_BLOB), ctypes.c_void_p,
                ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
            crypt32.CryptProtectData.restype = wintypes.BOOL
            ok = crypt32.CryptProtectData(
                ctypes.byref(blob_in), None, None, None, None, 0x1,
                ctypes.byref(blob_out))
        else:
            crypt32.CryptUnprotectData.argtypes = [
                ctypes.POINTER(DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR),
                ctypes.POINTER(DATA_BLOB), ctypes.c_void_p,
                ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
            crypt32.CryptUnprotectData.restype = wintypes.BOOL
            ok = crypt32.CryptUnprotectData(
                ctypes.byref(blob_in), None, None, None, None, 0x1,
                ctypes.byref(blob_out))
        if not ok:
            return None
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            kernel32.LocalFree(blob_out.pbData)
    except Exception:
        return None


def save_auth_cache(data):
    """保存登录缓存；Windows 用 DPAPI 加密，其他平台以 0600 权限落盘。"""
    os.makedirs(AUTH_CACHE_DIR, exist_ok=True)
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    if sys.platform == "win32":
        enc = _dpapi(raw, protect=True)
        if enc:
            blob = b"WBD1\n" + enc
        else:
            log("警告: DPAPI 加密失败，登录缓存将以明文保存")
            blob = b"WBP1\n" + raw
    else:
        blob = b"WBP1\n" + raw
    tmp = AUTH_CACHE_FILE + ".tmp"
    with open(tmp, "wb") as f:
        f.write(blob)
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    os.replace(tmp, AUTH_CACHE_FILE)
    return AUTH_CACHE_FILE


def load_auth_cache():
    if not os.path.exists(AUTH_CACHE_FILE):
        return None
    try:
        with open(AUTH_CACHE_FILE, "rb") as f:
            blob = f.read()
        if blob.startswith(b"WBD1\n"):
            raw = _dpapi(blob[5:], protect=False)
            if raw is None:
                return None
        elif blob.startswith(b"WBP1\n"):
            raw = blob[5:]
        else:
            return None
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def clear_auth_cache():
    try:
        if os.path.exists(AUTH_CACHE_FILE):
            os.remove(AUTH_CACHE_FILE)
        return True
    except Exception:
        return False


# ---- 浏览器 CDP 最小客户端（零第三方依赖） ----

class _WebSocket:
    """只实现 CDP 需要的 WebSocket 文本帧收发。"""

    def __init__(self, ws_url, timeout=10):
        parsed = urllib.parse.urlparse(ws_url)
        self.sock = socket.create_connection((parsed.hostname, parsed.port), timeout=timeout)
        self.sock.settimeout(10)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        path = parsed.path + (("?" + parsed.query) if parsed.query else "")
        req = ("GET %s HTTP/1.1\r\nHost: %s:%s\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n"
               % (path, parsed.hostname, parsed.port, key))
        self.sock.sendall(req.encode("ascii"))
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket 握手连接被关闭")
            buf += chunk
        head, self.buf = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise ConnectionError("WebSocket 握手失败: %s"
                                  % head.split(b"\r\n")[0].decode("latin1"))

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("WebSocket 连接已关闭")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        header = bytearray([0x81])
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        self.sock.sendall(bytes(header) + bytes(
            b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self):
        chunks = []
        while True:
            b1, b2 = self._read(2)
            fin, opcode = b1 & 0x80, b1 & 0x0f
            masked, length = b2 & 0x80, b2 & 0x7f
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if masked else None
            payload = self._read(length)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 8:
                raise ConnectionError("WebSocket 收到关闭帧")
            if opcode == 9:
                header = bytearray([0x8A, 0x80 | len(payload)])
                pong_mask = os.urandom(4)
                header += pong_mask
                self.sock.sendall(bytes(header) + bytes(
                    b ^ pong_mask[i % 4] for i, b in enumerate(payload)))
                continue
            if opcode == 10:
                continue
            chunks.append(payload)
            if fin:
                return b"".join(chunks)

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


class _CDP:
    def __init__(self, ws_url):
        self.ws = _WebSocket(ws_url)
        self.next_id = 0

    def call(self, method, params=None, timeout=20):
        self.next_id += 1
        mid = self.next_id
        self.ws.send({"id": mid, "method": method, "params": params or {}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msg = json.loads(self.ws.recv().decode("utf-8"))
            except socket.timeout:
                continue
            if msg.get("id") == mid:
                if "error" in msg:
                    raise UpstreamError("CDP %s 失败: %s" % (method, msg["error"]))
                return msg.get("result") or {}
        raise UpstreamError("CDP %s 超时" % method)

    def close(self):
        self.ws.close()


def _find_browser(preferred=None):
    if preferred:
        if os.path.exists(preferred):
            return preferred
        name = preferred.lower()
        candidates = {
            "chrome": [
                os.path.join(os.environ.get("ProgramFiles", ""), "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
            ],
            "edge": [
                os.path.join(os.environ.get("ProgramFiles", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
                os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
            ],
        }.get(name, [])
        for p in candidates:
            if p and os.path.exists(p):
                return p
    for p in (
        os.path.join(os.environ.get("ProgramFiles", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ):
        if p and os.path.exists(p):
            return p
    for name in ("google-chrome", "chromium", "chromium-browser",
                 "microsoft-edge", "microsoft-edge-stable"):
        p = shutil.which(name)
        if p:
            return p
    raise UpstreamError("未找到 Chrome/Edge，请用 --browser 指定浏览器路径，或使用 --manual 手动粘贴 Token")


def _wait_devtools_port(profile, timeout=20):
    path = os.path.join(profile, "DevToolsActivePort")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    lines = f.read().splitlines()
                if lines:
                    return int(lines[0])
            except Exception:
                pass
        time.sleep(0.1)
    raise UpstreamError("浏览器调试端口启动超时")


def _page_targets(port):
    """列出当前浏览器里可调试的页面目标。"""
    try:
        status, targets, _ = _http_json(
            "GET", "http://127.0.0.1:%d/json/list" % port, {}, timeout=3)
    except Exception:
        return []
    if status != 200 or not isinstance(targets, list):
        return []
    return [t for t in targets
            if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]


def _wait_page_target(port, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        pages = _page_targets(port)
        if pages:
            for t in pages:
                if "workbuddy.cn" in (t.get("url") or ""):
                    return t
            return pages[0]
        time.sleep(0.2)
    raise UpstreamError("未找到浏览器页面调试目标")


def _kill_process_tree(proc):
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(3)
            except Exception:
                proc.kill()
    except Exception:
        pass
    if sys.platform == "win32" and proc.poll() is None:
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10)
        except Exception:
            pass


JS_COLLECT_AUTH = r"""
(() => {
  const out = {url: location.href, title: document.title, loggedIn: false, jwts: [], keys: []};
  const jwtRe = /eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}/;
  const body = (document.body && document.body.innerText) || "";
  out.loggedIn = (location.pathname.indexOf("growth-center") >= 0 &&
                  /个人版|成长计划|成长中心/.test(body));
  const add = (source, value) => {
    if (typeof value === "string" && value.length >= 20 && value.length < 8192) {
      out.jwts.push({source: source, token: value});
    }
  };
  const scan = (store, name) => {
    try {
      for (let i = 0; i < store.length && out.keys.length < 120; i++) {
        const k = store.key(i);
        const v = store.getItem(k) || "";
        out.keys.push(name + ":" + k);
        const m = v.match(jwtRe);
        if (m) add(name + ":" + k, m[0]);
        else if (/token|auth/i.test(k)) {
          add(name + ":" + k, v);
          try {
            const obj = JSON.parse(v);
            const tv = obj && (obj.token || obj.accessToken || obj.access_token);
            if (tv) add(name + ":" + k + ".token", tv);
          } catch (e) {}
        }
      }
    } catch (e) {}
  };
  scan(localStorage, "local");
  scan(sessionStorage, "session");
  try { add("session:growth-center-token", sessionStorage.getItem("growth-center-token")); } catch (e) {}
  const um = location.href.match(/[?&]token=([^&]+)/);
  if (um) add("url", decodeURIComponent(um[1]));
  return out;
})()
"""


JS_PLUGIN_STATE = r"""
(async () => {
  const out = {ok: false};
  try {
    const s = await fetch('/v2/plugin/auth/state?platform=workbuddy', {
      method: 'POST',
      credentials: 'include',
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'X-No-Authorization': 'true'
      },
      body: '{}'
    });
    out.stateStatus = s.status;
    const sj = await s.json().catch(() => ({}));
    const state = sj && sj.data && sj.data.state;
    if (!state) {
      out.error = 'no_state';
      out.body = JSON.stringify(sj).slice(0, 500);
      return out;
    }
    out.state = state;
    const l = await fetch('/console/auth/login?platform=workbuddy&state=' +
      encodeURIComponent(state) + '&domain=www.workbuddy.cn', {
      credentials: 'include',
      headers: {'Accept': 'application/json', 'X-Domain': 'www.workbuddy.cn'}
    });
    out.loginStatus = l.status;
    out.loginUrl = l.url;
    out.ok = true;
    return out;
  } catch (e) {
    out.error = String(e);
    return out;
  }
})()
"""


def _collect_page_state(cdp):
    result = cdp.call("Runtime.evaluate", {
        "expression": JS_COLLECT_AUTH,
        "returnByValue": True,
        "awaitPromise": True,
    })
    if result.get("exceptionDetails"):
        return {}
    return (result.get("result") or {}).get("value") or {}


def _plugin_state_from_page(cdp):
    """在浏览器页面内完成 plugin auth state 流程，返回 {state, ...}。"""
    try:
        result = cdp.call("Runtime.evaluate", {
            "expression": JS_PLUGIN_STATE,
            "returnByValue": True,
            "awaitPromise": True,
        })
    except Exception:
        return {}
    if result.get("exceptionDetails"):
        return {}
    value = (result.get("result") or {}).get("value") or {}
    return value if isinstance(value, dict) else {}


def _poll_plugin_token(state, timeout=30):
    """轮询 plugin token 接口；state 完成后该接口不需要 Cookie。"""
    if not state:
        return "", ""
    url = WEB_BASE + "/v2/plugin/auth/token?state=" + urllib.parse.quote(str(state))
    headers = {
        "Accept": "application/json",
        "X-No-Authorization": "true",
        "User-Agent": CLIENT_UA,
    }
    deadline = time.time() + max(3, int(timeout))
    last = ""
    while time.time() < deadline:
        status, obj, _ = _http_json("GET", url, headers, timeout=10)
        if status == 200 and isinstance(obj, dict):
            data = obj.get("data") if isinstance(obj.get("data"), dict) else obj
            for key in ("accessToken", "access_token", "token"):
                token = data.get(key) or obj.get(key)
                if (isinstance(token, str) and token.count(".") == 2
                        and len(token) > 50):
                    ok, uid, detail = validate_token(token)
                    if ok:
                        return token, uid
                    last = detail
        time.sleep(1)
    if last:
        log("[login] Token 校验未通过（%s）" % last)
    return "", ""


def _collect_cookies(cdp):
    result = cdp.call("Network.getAllCookies")
    return _filter_auth_cookies(result.get("cookies") or [])


def _save_browser_auth(token, cookies, uid):
    data = {
        "version": 1,
        "source": "browser" if token and cookies else ("browser-token" if token else "browser-cookie"),
        "capturedAt": now_ms(),
        "accessToken": token or "",
        "uid": uid or jwt_uid(token) or "",
        "domain": "www.workbuddy.cn",
        "cookies": cookies or [],
        "expiresAt": jwt_expiry_ms(token) if token else _cookie_expiry_ms(cookies),
    }
    return save_auth_cache(data)


def _manual_login():
    print()
    print("请在已登录 WorkBuddy 网页的浏览器中打开：")
    print("  " + GROWTH_CENTER_URL)
    print("按 F12 → Network → 任意 growth 请求 → 复制请求头里的")
    print("  Authorization: Bearer eyJ...")
    print()
    try:
        token = getpass.getpass("粘贴 Token（输入不回显）: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        log("已取消")
        return 1
    if token.lower().startswith("authorization:"):
        token = token.split(":", 1)[1].strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        log("未输入 Token")
        return 1
    ok, uid, detail = validate_token(token)
    if not ok:
        log("Token 验证失败（%s），请确认复制的是 Bearer Token 且未过期" % detail)
        return 1
    path = _save_browser_auth(token, [], uid)
    log("✓ Token 已验证并保存到登录缓存：%s" % path)
    log("  UID: %s" % (uid or "未知"))
    log("  到期: %s（剩余 %s）" % (format_ts(jwt_expiry_ms(token)),
                                  remaining_text(jwt_expiry_ms(token))))
    return 0


def browser_login(preferred=None, timeout=300, manual=False):
    """弹窗打开浏览器，等待用户登录，自动抓取 Token / Web Cookie 并缓存。"""
    if manual:
        return _manual_login()

    log("正在启动浏览器登录窗口...")
    proc = None
    profile = None
    cdp = None
    try:
        browser = _find_browser(preferred)
        profile = tempfile.mkdtemp(prefix="workbuddy-login-")
        args = [
            browser,
            "--remote-debugging-port=0",
            "--remote-allow-origins=*",
            "--user-data-dir=" + profile,
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            GROWTH_CENTER_URL,
        ]
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        port = _wait_devtools_port(profile)
        target = _wait_page_target(port)
        cdp = _CDP(target["webSocketDebuggerUrl"])
        cdp.call("Runtime.enable")
        cdp.call("Network.enable")
        log("浏览器已打开，请在弹窗中完成登录（扫码或账号登录均可）...")
        log("登录成功后会自动关闭窗口，无需手动操作。")

        deadline = time.time() + max(10, int(timeout))
        token = ""
        cookies = []
        uid = ""
        last_state = {}
        last_cookie_check = 0.0
        last_log = 0.0
        last_urls = []
        cookie_names = []
        last_cookie_names = []
        current_id = target.get("id") or ""
        while time.time() < deadline:
            if proc.poll() is not None:
                log("[login] 浏览器已关闭，停止等待")
                break
            pages = _page_targets(port)
            urls = [p.get("url") or "" for p in pages]
            if urls:
                last_urls = urls

            # 登录可能在新标签页完成；优先切到成长中心页面。
            growth = next((p for p in pages
                           if "/profile/growth-center" in (p.get("url") or "")), None)
            if growth and growth.get("id") != current_id:
                try:
                    new_cdp = _CDP(growth["webSocketDebuggerUrl"])
                    new_cdp.call("Runtime.enable")
                    new_cdp.call("Network.enable")
                    if cdp is not None:
                        cdp.close()
                    cdp = new_cdp
                    current_id = growth.get("id") or ""
                    log("[login] 已切换到成长中心页面")
                except Exception as e:
                    log("[login] 切换页面失败: %s" % e)

            state = {}
            try:
                state = _collect_page_state(cdp)
            except Exception:
                state = {}
            if state:
                last_state = state

            if time.time() - last_log >= 5:
                last_log = time.time()
                log("[login] 页面: %s" % (" | ".join(urls[:4]) or "未知"))
                if state:
                    log("[login] 登录标记: %s | Token候选: %d"
                        % ("是" if state.get("loggedIn") else "否",
                           len(state.get("jwts") or [])))

            for item in (state.get("jwts") or [])[:5]:
                if time.time() >= deadline:
                    break
                candidate = (item.get("token") or "").strip()
                if len(candidate) < 20 or candidate.startswith(("{", "[")):
                    continue
                if any(ch.isspace() for ch in candidate):
                    continue
                ok, token_uid, _ = validate_token(candidate)
                if ok:
                    token = candidate
                    uid = token_uid or uid
                    break
            if token:
                try:
                    cookies = _collect_cookies(cdp)
                except Exception:
                    cookies = []
                break

            if state.get("loggedIn") or time.time() - last_cookie_check > 5:
                last_cookie_check = time.time()
                try:
                    got = _collect_cookies(cdp)
                except Exception:
                    got = []
                if got:
                    cookie_names = sorted({c.get("name") for c in got
                                           if c.get("name")})
                    if cookie_names != last_cookie_names:
                        last_cookie_names = cookie_names
                        log("[login] Cookie: %s" % ", ".join(cookie_names))
                    ok, cookie_uid, _ = validate_cookies(got)
                    page_url = state.get("url") or ""
                    if (ok or state.get("loggedIn")
                            or "/profile/growth-center" in page_url):
                        # 优先在页面上下文里完成 plugin auth state 流程：
                        # 浏览器自己携带登录 Cookie，不需要读取 HttpOnly Cookie。
                        if "www.workbuddy.cn" in page_url:
                            ps = _plugin_state_from_page(cdp)
                            if ps.get("error"):
                                log("[login] plugin state 失败: %s" % ps.get("error"))
                            if ps.get("state"):
                                log("[login] 已建立 plugin state，正在换取 Bearer Token...")
                                wait_left = max(3, int(deadline - time.time()))
                                plugin_token, plugin_uid = _poll_plugin_token(
                                    ps["state"], timeout=min(20, wait_left))
                                if plugin_token:
                                    token = plugin_token
                                    cookies = got
                                    uid = plugin_uid or cookie_uid or uid
                                    log("[login] 已通过浏览器会话换取到 Bearer Token")
                                    break
                        # 回退：Python 侧用 Cookie 走同一套接口。
                        plugin_token, plugin_uid = _exchange_token_from_cookies(got)
                        if plugin_token:
                            token = plugin_token
                            cookies = got
                            uid = plugin_uid or cookie_uid or uid
                            log("[login] 已从 Web 会话换取到 Bearer Token")
                            break
                        if ok:
                            cookies = got
                            uid = cookie_uid or uid
                            break

                    # 有些 Web 登录只下发 SSO Cookie；尝试用 plugin 接口
                    # 换取桌面端 Bearer Token。
                    states = []
                    match = re.search(r"[?&]state=([^&]+)", state.get("url") or "")
                    if match:
                        states.append(urllib.parse.unquote(match.group(1)))
                    states.append("0")
                    for st in dict.fromkeys(states):
                        plugin_token = _plugin_token_from_cookies(got, st)
                        if not plugin_token:
                            continue
                        tok_ok, tok_uid, _ = validate_token(plugin_token)
                        if tok_ok:
                            token = plugin_token
                            uid = tok_uid or uid
                            cookies = got
                            log("[login] 已用 Web 会话换取到 Bearer Token")
                            break
                    if token:
                        break

            time.sleep(2)

        if not token and not cookies:
            log("登录等待超时（%d 秒），未获取到有效 Token 或 Cookie。" % timeout)
            if last_urls:
                log("最后页面: %s" % " | ".join(last_urls[:4]))
            if last_state:
                log("当前页面: %s" % (last_state.get("url") or ""))
                keys = last_state.get("keys") or []
                if keys:
                    log("页面存储键（前 20 个）: %s" % ", ".join(keys[:20]))
            if cookie_names:
                log("已拿到 Cookie 但无法读取成长任务: %s" % ", ".join(cookie_names))
                log("这些可能只是 SSO Cookie，业务会话还没有建立。")
            log("可重试，或运行：python workbuddy_task.py login --manual")
            return 1

        path = _save_browser_auth(token, cookies, uid)
        log("✓ 浏览器登录成功，凭据已写入登录缓存")
        log("  UID: %s" % (uid or "未知"))
        if token:
            exp = jwt_expiry_ms(token)
            log("  Token: 已获取（到期 %s，剩余 %s）"
                % (format_ts(exp), remaining_text(exp)))
        if cookies:
            exp = _cookie_expiry_ms(cookies)
            log("  Cookie: %d 个（最早到期 %s，剩余 %s）"
                % (len(cookies), format_ts(exp), remaining_text(exp)))
        if not token:
            log("⚠ 未换取到 Bearer Token，当前为 Web Cookie 模式：")
            log("  只能查看/报名/领奖；对话类任务需要 Token，可运行 login --manual 粘贴。")
        log("  缓存文件: %s" % path)
        return 0
    except UpstreamError as e:
        log("浏览器登录失败: %s" % e)
        return 1
    except Exception as e:
        log("浏览器登录失败: %s" % e)
        return 1
    finally:
        if cdp is not None:
            cdp.close()
        _kill_process_tree(proc)
        if profile:
            shutil.rmtree(profile, ignore_errors=True)


def cmd_status():
    cache = load_auth_cache()
    print("登录缓存: %s" % AUTH_CACHE_FILE)
    if not cache:
        log("未找到浏览器登录缓存。请运行：python workbuddy_task.py login")
        return 1
    log("来源: %s" % (cache.get("source") or "未知"))
    log("UID: %s" % (cache.get("uid") or "未知"))
    token = cache.get("accessToken") or ""
    cookies = cache.get("cookies") if isinstance(cache.get("cookies"), list) else []
    valid = False
    if token:
        exp = jwt_expiry_ms(token) or int(cache.get("expiresAt") or 0)
        left = remaining_text(exp)
        log("Token: %s（到期 %s，剩余 %s）"
            % ("已过期" if left == "已过期" else "有效", format_ts(exp), left))
        if left != "已过期" and exp - now_ms() < 7 * 86400000:
            log("⚠ Token 将在 %s 后过期，建议运行：python workbuddy_task.py login" % left)
        valid = valid or left != "已过期"
    if cookies:
        exp = _cookie_expiry_ms(cookies)
        left = remaining_text(exp)
        log("Cookie: %d 个（最早到期 %s，剩余 %s）"
            % (len(cookies), format_ts(exp), left))
        if left != "已过期" and exp - now_ms() < 3 * 86400000:
            log("⚠ Cookie 将在 %s 后过期，建议运行：python workbuddy_task.py login" % left)
        valid = valid or left != "已过期"
    if not valid:
        log("缓存中没有可用凭据，请重新运行 login")
        return 1
    return 0


def cmd_logout():
    if clear_auth_cache():
        log("已删除登录缓存：%s" % AUTH_CACHE_FILE)
        return 0
    log("删除登录缓存失败：%s" % AUTH_CACHE_FILE)
    return 1


def warn_auth_expiry(client):
    exp = client.auth_expires_at()
    if not exp:
        return
    left = remaining_text(exp)
    if left == "已过期":
        log("⚠ 登录凭据已过期，请运行：python workbuddy_task.py login")
    elif exp - now_ms() < (7 if client.token else 3) * 86400000:
        log("⚠ 登录凭据将在 %s 后过期，建议运行：python workbuddy_task.py login" % left)


# ---------------------------------------------------------------------------
# 上游错误
# ---------------------------------------------------------------------------

class UpstreamError(Exception):
    def __init__(self, msg, status=0, body=None):
        super().__init__(msg)
        self.status = status
        self.body = body


# ---------------------------------------------------------------------------
# API 客户端
# ---------------------------------------------------------------------------

class WorkBuddyClient:
    """封装 growth/tasks/report/chat 全部上游调用（对齐 Go upstream 包）。"""

    def __init__(self, session):
        auth = session.get("auth") or {}
        account = session.get("account") or {}
        self.token = auth.get("accessToken")
        raw_cookies = session.get("cookies") or []
        self.cookies = raw_cookies if isinstance(raw_cookies, list) else []
        self.expires_at = int(session.get("expiresAt") or 0)
        self.uid = account.get("uid")
        raw_nick = account.get("nickname")
        self.nickname = raw_nick if isinstance(raw_nick, str) else ""
        self.enterprise_id = account.get("enterpriseId") or ""
        self.domain = auth.get("domain") or "www.workbuddy.cn"
        if isinstance(self.token, dict):
            raise ValueError("ENCRYPTED_SESSION: 本地凭据为加密格式 ($wbEncrypted)，且未成功解密")
        if not self.token:
            self.token = None
        if self.token and not self.uid:
            self.uid = jwt_uid(self.token)
        if self.token and not self.uid:
            raise ValueError("NO_SESSION: Token 缺少 UID，请同时设置 WORKBUDDY_UID")
        if not self.token and not self.cookies:
            raise ValueError("NO_SESSION: 本地未找到有效登录会话")
        # realm：domain 后缀 .workbuddy.ai → global（本脚本只处理 CN 成长任务体系）
        self.is_global = self.domain.lower().endswith("workbuddy.ai")

    def auth_expires_at(self):
        if self.token:
            return jwt_expiry_ms(self.token) or self.expires_at
        return _cookie_expiry_ms(self.cookies)

    # ---- 头部 ----

    def billing_headers(self, mp=False):
        """growth 域 / billing 域请求头（对齐 Go BillingHeaders）。"""
        if not self.token:
            raise UpstreamError(
                "AUTH_REQUIRED: 该操作需要桌面端 Token；浏览器 Cookie 模式仅支持"
                "任务列表/报名/领奖。请运行 python workbuddy_task.py login 获取完整凭据。")
        h = {
            "Authorization": "Bearer " + self.token,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-CodeBuddy-Request": "1",
            "Accept-Language": "zh-CN",
            "User-Agent": BILLING_UA,
        }
        if self.uid:
            h["X-User-Id"] = self.uid
        if self.enterprise_id:
            h["X-Enterprise-Id"] = self.enterprise_id
            h["X-Tenant-Id"] = self.enterprise_id
        if self.domain:
            h["X-Domain"] = self.domain
        # 账号级稳定设备指纹（防多号设备关联风控）
        h["X-Machine-ID"] = derive_id(self.uid, "machine")
        h["X-Session-ID"] = derive_id(self.uid, "session")
        if mp:
            h["X-Client-Platform"] = MP_PLATFORM
        return h

    def claim_headers(self):
        """Web 成长中心请求头（Bearer 或浏览器 Cookie 两种形态）。"""
        h = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": WEB_BASE,
            "Referer": GROWTH_CENTER_URL,
            "x-client-platform": "web",
            "User-Agent": CLIENT_UA,
        }
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        if self.cookies:
            h["Cookie"] = _cookie_header(self.cookies)
        if not self.token and not self.cookies:
            raise UpstreamError("AUTH_REQUIRED: 没有可用的 Token 或 Web Cookie")
        if self.uid:
            h["X-User-Id"] = self.uid
        if self.enterprise_id:
            h["X-Enterprise-Id"] = self.enterprise_id
            h["X-Tenant-Id"] = self.enterprise_id
        if self.domain:
            h["X-Domain"] = self.domain
        return h

    def web_headers(self):
        """Web 成长中心列表/报名请求头（Cookie 模式）。"""
        h = self.claim_headers()
        h["Accept"] = "application/json, text/plain, */*"
        return h

    # ---- 底层请求 ----

    @staticmethod
    def _call(method, url, headers, payload=None, timeout=30):
        """发请求返回 (status, obj)；网络错误 status=-1。"""
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
                raw = resp.read().decode("utf-8", "replace")
                status = resp.status
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            status = e.code
        except Exception as e:
            return -1, {"error": str(e)}
        try:
            return status, json.loads(raw) if raw else {}
        except Exception:
            return status, {"raw": raw[:500]}

    @classmethod
    def _envelope(cls, method, url, headers, payload=None, timeout=30):
        """发请求并解 {code,msg,data} 信封，返回 data（非 2xx / code!=0 抛错）。"""
        status, obj = cls._call(method, url, headers, payload, timeout)
        if status < 0:
            raise UpstreamError("网络错误: %s" % brief(obj), -1, obj)
        if status >= 400:
            raise UpstreamError("HTTP %s: %s" % (status, brief(obj)), status, obj)
        if isinstance(obj, dict) and "code" in obj and obj.get("code") not in (0, "0", None):
            raise UpstreamError(
                "code=%s msg=%s" % (obj.get("code"), obj.get("msg")), status, obj)
        if isinstance(obj, dict) and "data" in obj:
            return obj["data"]
        return obj

    # ---- growth 任务接口 ----

    def list_tasks(self, mp=False):
        """GET /v2/activity/growth/tasks 全量任务列表（mp=True 走小程序口径）。"""
        if mp and not self.token:
            raise UpstreamError("AUTH_REQUIRED: 小程序任务需要桌面端 Token")
        if self.token:
            data = self._envelope("GET", CHAT_BASE + PATH_LIST_TASKS,
                                  self.billing_headers(mp=mp))
        else:
            data = self._envelope("GET", WEB_BASE + PATH_LIST_TASKS,
                                  self.web_headers())
        return parse_tasks(data)

    def accept_tasks(self, codes, mp=False):
        """POST accept 任务（not_accepted → accepted，幂等）。"""
        if mp and not self.token:
            raise UpstreamError("AUTH_REQUIRED: 小程序任务需要桌面端 Token")
        if self.token:
            self._envelope("POST", CHAT_BASE + PATH_ACCEPT_TASKS,
                           self.billing_headers(mp=mp), {"task_codes": list(codes)})
        else:
            self._envelope("POST", WEB_BASE + PATH_ACCEPT_TASKS_WEB,
                           self.web_headers(), {"task_codes": list(codes)})

    def claim_reward(self, code):
        """Web 成长中心领奖：POST {web}/activity/growth/tasks/{code}/claim。

        返回 (credit, energy, already)。重复领 already=True（幂等不报错）。
        """
        url = "%s/activity/growth/tasks/%s/claim" % (WEB_BASE, urllib.parse.quote(code))
        data = self._envelope("POST", url, self.claim_headers())
        data = data if isinstance(data, dict) else {}
        if data.get("already_claimed"):
            return 0, 0, True
        return int(data.get("credit") or 0), int(data.get("energy") or 0), False

    def claim_reward_mp(self, code):
        """mp 口径领奖：chat 域 + mp 头；400 时降级 Web 域领奖端点。"""
        url = "%s/activity/growth/tasks/%s/claim" % (CHAT_BASE, urllib.parse.quote(code))
        try:
            data = self._envelope("POST", url, self.billing_headers(mp=True))
        except UpstreamError as e:
            if e.status == 400:
                return self.claim_reward(code)
            raise
        data = data if isinstance(data, dict) else {}
        return int(data.get("credit") or 0), int(data.get("energy") or 0), False

    # ---- 任务查询辅助 ----

    def task_by_code(self, code, mp=False):
        """按 code 定位任务；未找到返回 None（不视为错误）。"""
        for t in self.list_tasks(mp=mp):
            if t["task_code"] == code:
                return t
        return None

    def task_by_code_waiting(self, code, mp=False):
        """回读任务，未达标则有界轮询等待异步计分落定。"""
        t = self.task_by_code(code, mp=mp)
        if t is None or t["claimable"] or t["claimed"]:
            return t
        for _ in range(1, CLAIM_POLL_ATTEMPTS):
            time.sleep(CLAIM_POLL_GAP)
            t2 = self.task_by_code(code, mp=mp)
            if t2 is None:
                continue
            t = t2
            if t["claimable"] or t["claimed"]:
                break
        return t

    def accept_with_verify_mp(self, code):
        """mp accept 并回读验证登记生效（上游存在 200+OK 但未落账形态）。"""
        for attempt in (1, 2):
            try:
                self.accept_tasks([code], mp=True)
            except UpstreamError as e:
                log("accept 尝试%d 失败: %s" % (attempt, e))
                continue
            time.sleep(MP_GAP)
            t = self.task_by_code(code, mp=True)
            if t is not None and t["accept_status"] not in ("not_accepted", ""):
                return True
            log("accept 尝试%d 未登记生效（回读=%s）"
                % (attempt, t["accept_status"] if t else "?"))
        return False

    # ---- 上报通道 1：billing 域对话活跃上报 ----

    def report_chat_activity(self, conversation_id, request_id=None,
                             model_id="deepseek-v4-flash",
                             model_name="DeepSeek V4 Flash", mode="craft"):
        """POST {billing}/v2/report 上报一条 chat_request_send（完整客户端形状）。"""
        request_id = request_id or conversation_id
        model_id = model_id or "deepseek-v4-flash"
        model_name = model_name or model_id
        now = now_ms()
        ev = {
            "eventCode": "chat_request_send", "timestamp": now, "reportDelay": 0,
            "mode": mode, "conversationId": conversation_id, "requestId": request_id,
            "inputLength": 12, "requestModelId": model_id,
            "requestModelName": model_name, "isPlan": False,
            "isAutoExecuteTerminal": False, "isAutoModify": False,
            "codebaseEnable": False, "maxToken": 0, "maxSteps": 0, "temperature": 0,
            "maxRetries": 0, "mentionContexts": [], "knowledgeId": [],
            "knowledgeName": [], "codebaseId": "", "mentionContextCount": 0,
            "command": "", "expertId": "", "recommendId": "", "skillId": "",
            "skillCount": 0, "totalCount": 0, "fileUri": "", "presentAt": now,
            "traceId": "", "rootRequestId": request_id,
            "parentConversationId": conversation_id,
            "agentName": "default", "agentType": "conversation",
            "userId": self.uid,
        }
        self._envelope("POST", BILLING_BASE + PATH_REPORT,
                       self.billing_headers(), [ev])

    # ---- 上报通道 2：桌面指纹事件（chat 域 /v2/report） ----

    def report_desktop_events(self, events):
        """以桌面客户端指纹批量上报事件（公共指纹注入，业务字段优先）。"""
        if not self.token:
            raise UpstreamError("AUTH_REQUIRED: 桌面事件上报需要桌面端 Token")
        if not events:
            raise UpstreamError("desktop report: no events")
        fp = self.desktop_fingerprint()
        arr = []
        for ev in events:
            m = dict(fp)
            m.update(ev)
            arr.append(m)
        headers = {
            "Authorization": "Bearer " + self.token,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "User-Agent": DESKTOP_UA,
            "X-Domain": CHAT_BASE,
            "X-Product": "SaaS",
            "X-Request-ID": derive_id(self.uid, "req") + str(now_ms() % 1000000),
        }
        if self.uid:
            headers["X-User-Id"] = self.uid
        self._envelope("POST", CHAT_BASE + PATH_REPORT, headers, arr)

    def desktop_fingerprint(self):
        """桌面端公共指纹字段（ideName=WorkBuddy、extName=workbuddy-desktop）。"""
        now = now_ms()
        return {
            "timezone": "Asia/Shanghai", "reportDelay": 2000,
            "userId": self.uid, "username": self.nickname,
            "userNickname": self.nickname, "product": "SaaS",
            "releaseDate": 1789036585355,
            "commit": "5f9692923c93033111c51ad7b003eb80204a9b75",
            "ideName": "WorkBuddy", "ideType": "WorkBuddy", "ideVersion": "5.5.6",
            "machineId": derive_id(self.uid, "machine"),
            "sessionId": derive_id(self.uid, "session"),
            "extName": "workbuddy-desktop", "extVersion": "5.5.6",
            "os": "win32", "arch": "x64", "osVersion": "10.0.26220",
            "cpuCores": 20, "memorySize": 24,
            "timestamp": now, "presentAt": now,
        }

    # ---- 上报通道 3：web 指纹（www.workbuddy.cn/v2/report） ----

    def report_web_event(self, event_code, page_url, element_id, element_name):
        """以浏览器指纹上报单事件（Library_read 等页面行为类任务判据）。"""
        ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")
        ev = {
            "eventCode": event_code, "timestamp": now_ms(), "reportDelay": 0,
            "pageURL": page_url, "elementId": element_id, "elementName": element_name,
            "os": "Win32", "arch": "", "osVersion": "10.0", "userAgent": ua,
            "machineId": derive_id(self.uid, "webmachine"), "userId": self.uid,
            "userNickname": self.nickname, "enterpriseId": self.enterprise_id,
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-client-platform": "web",
            "Origin": WEB_BASE,
            "Referer": page_url,
            "User-Agent": ua,
        }
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if self.cookies:
            headers["Cookie"] = _cookie_header(self.cookies)
        if not self.token and not self.cookies:
            raise UpstreamError("AUTH_REQUIRED: 没有可用的 Token 或 Web Cookie")
        if self.uid:
            headers["X-User-Id"] = self.uid
        self._envelope("POST", WEB_BASE + PATH_REPORT, headers, [ev])

    # ---- 上报通道 4：小程序指纹（billing 域 /v2/report） ----

    def report_mp_events(self, events):
        """以小程序指纹批量上报事件（school_season / Sequential_Tasks 判据）。"""
        if not self.token:
            raise UpstreamError("AUTH_REQUIRED: 小程序事件上报需要桌面端 Token")
        if not events:
            raise UpstreamError("mp report: no events")
        base = self.mp_fingerprint()
        arr = []
        for ev in events:
            m = dict(base)
            m.update(ev)
            arr.append(m)
        headers = {
            "Authorization": "Bearer " + self.token,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Client-Product": "workbuddy-mp",
            "X-Client-Version": "2.4.0",
            "X-Client-Platform": "mp-weixin",
            "X-Platform": "wechatmp",
        }
        if self.uid:
            headers["X-User-Id"] = self.uid
        self._envelope("POST", BILLING_BASE + PATH_REPORT, headers, arr)

    def mp_fingerprint(self):
        """小程序埋点公共指纹（appservice wQ()+Ao() 对齐）。"""
        return {
            "timestamp": now_ms(), "ideType": "WorkBuddy_MP", "ideVersion": "2.4.0",
            "extName": "workbuddy-mp", "extVersion": "2.4.0", "product": "SaaS",
            "ideName": "wx_app_cloud", "platform": "mini_program",
            "os": "windows", "osVersion": "11", "arch": "x64",
            "machineId": "0655736a-607f-4d9d-b430-58176ee9a090",
            "timezone": "Asia/Shanghai",
            "userId": self.uid, "userNickname": self.nickname,
        }

    # ---- 真实对话 / 专家市场 / Buddy（growth 域写接口） ----

    def chat_completion(self, model_id="glm-5.2", prompt="hi，请回复一句话",
                        timeout=90):
        """POST {chat}/v2/chat/completions 真实流式对话一次，读干 SSE。返回 (status, first_text)。"""
        body = {"model": model_id, "messages": [{"role": "user", "content": prompt}],
                "stream": True}
        headers = self.billing_headers()
        headers["Accept"] = "text/event-stream"
        headers["User-Agent"] = CLIENT_UA
        req = urllib.request.Request(CHAT_BASE + PATH_CHAT,
                                     data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     headers=headers, method="POST")
        first = ""
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
                status = r.status
                for raw in r:
                    line = raw.decode("utf-8", "replace")
                    if not line.startswith("data: "):
                        continue
                    payload = line[6:].strip()
                    if payload in ("[DONE]", ""):
                        continue
                    try:
                        obj = json.loads(payload)
                        delta = (obj.get("choices") or [{}])[0].get("delta") or {}
                        content = delta.get("content") or ""
                        if content and not first:
                            first = content
                    except Exception:
                        pass
                return status, first
        except urllib.error.HTTPError as e:
            t = e.read().decode("utf-8", "replace")
            return e.code, t[:200]
        except Exception as e:
            return -1, repr(e)[:200]

    def desktop_chat_with_expert(self, expert_id=""):
        """桌面指纹真实对话，从 SSE 抓服务端 requestId（JOIN 事件必须用真实 id）。"""
        if not self.token:
            raise UpstreamError("AUTH_REQUIRED: 真实对话需要桌面端 Token")
        conversation_id = "wb2api-conv-%d" % time.time_ns()
        body = {
            "model": "fast-model",
            "messages": [
                {"role": "system",
                 "content": "You are a helpful assistant. 当前处于中文环境，使用简体中文回答。"},
                {"role": "user", "content": "1+1等于几？直接回答。"},
            ],
            "agent": "cli", "temperature": 1, "stream": True,
            "stream_options": {"include_usage": True},
        }
        headers = {
            "Authorization": "Bearer " + self.token,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": DESKTOP_UA,
            "X-Domain": CHAT_BASE,
            "X-Product": "SaaS",
            "X-User-Id": self.uid,
            "X-Conversation-ID": conversation_id,
            "X-Request-ID": str(time.time_ns()),
            "X-Agent-Intent": "craft",
            "X-Agent-Type": "main",
            "X-IDE-Name": "WorkBuddy",
            "X-IDE-Type": "WorkBuddy",
            "X-IDE-Version": "5.5.6",
            "x-codebuddy-request": "1",
        }
        if expert_id:
            headers["X-Expert-Id"] = expert_id
        req = urllib.request.Request(CHAT_BASE + PATH_CHAT,
                                     data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=90, context=SSL_CTX) as resp:
                if resp.status != 200:
                    t = resp.read(200).decode("utf-8", "replace")
                    return "", "", UpstreamError("chat http %s: %s" % (resp.status, t),
                                                 resp.status)
                # 读干 SSE，抓第一个合法服务端 id
                buf = ""
                for raw in resp:
                    buf += raw.decode("utf-8", "replace")
                    m = re.search(r'"id":"([^"]+)"', buf)
                    if m and SERVER_ID_RE.match(m.group(1)):
                        return conversation_id, m.group(1), None
                    if len(buf) > 1 << 20:
                        break
        except urllib.error.HTTPError as e:
            t = e.read().decode("utf-8", "replace")
            return "", "", UpstreamError("chat http %s: %s" % (e.code, t[:200]), e.code)
        except Exception as e:
            return "", "", UpstreamError(repr(e)[:200], -1)
        return "", "", UpstreamError("SSE 中未找到服务端 requestId")

    def market_expert_list(self, expert_type=""):
        """POST 专家市场列表（expert_actual_use 判据要求真实 ex_ id）。"""
        body = {"page": 1, "page_size": 20, "sort_by": "reco_rank",
                "sort_order": "desc"}
        if expert_type:
            body["expert_type"] = expert_type
        headers = {
            "Authorization": "Bearer " + self.token,
            "Content-Type": "application/json",
            "User-Agent": DESKTOP_UA,
            "X-Domain": CHAT_BASE,
            "X-Product": "SaaS",
        }
        if self.uid:
            headers["X-User-Id"] = self.uid
        data = self._envelope("POST",
                              CHAT_BASE + "/portal/operation-platform/market/expert/list",
                              headers, body)
        return (data or {}).get("experts") or []

    def set_appearance_theme(self, resource_key):
        """POST /v2/user-asset/appearance/set 设置外观主题。"""
        headers = {
            "Authorization": "Bearer " + self.token,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "User-Agent": DESKTOP_UA,
            "X-Product": "SaaS",
        }
        if self.uid:
            headers["X-User-Id"] = self.uid
        self._envelope("POST", CHAT_BASE + "/v2/user-asset/appearance/set", headers,
                       {"kind": "theme", "resource_key": resource_key})

    def buddy_agreement(self):
        """POST /activity/growth/buddy/agreement 同意协议（幂等）。"""
        self._envelope("POST", CHAT_BASE + "/activity/growth/buddy/agreement",
                       self.billing_headers(), {"agree": True})

    def buddy_first(self):
        """POST /activity/growth/buddy/first 领养第一只 Buddy（+300 分）。"""
        self._envelope("POST", CHAT_BASE + "/activity/growth/buddy/first",
                       self.billing_headers(), {})


def parse_tasks(data):
    """解析 growth 任务列表（progress 可能是 {current,target} 对象或平铺字段）。"""
    raw = (data or {}).get("tasks") or []
    out = []
    for t in raw:
        cur, tgt = int(t.get("current") or 0), int(t.get("target") or 0)
        pr = t.get("progress")
        if isinstance(pr, dict) and (pr.get("target") or pr.get("current")):
            cur, tgt = int(pr.get("current") or 0), int(pr.get("target") or 0)
        accept_status = t.get("accept_status") or ""
        claimed = accept_status == "claimed"
        out.append({
            "task_code": t.get("task_code"),
            "locked": bool(t.get("locked")),
            "target": tgt, "current": cur,
            "accept_status": accept_status,
            "claimable": (not claimed) and tgt > 0 and cur >= tgt,
            "claimed": claimed,
        })
    return out


def task_progress_text(t):
    """任务进度的可读表示。"""
    if t is None:
        return "?"
    if t["target"] > 0:
        return "%d/%d" % (t["current"], t["target"])
    if t["claimed"]:
        return "claimed"
    return t["accept_status"] or "?"


# ---------------------------------------------------------------------------
# 事件链构造器（对齐 Go desktop.go / school.go，业务字段不含公共指纹）
# ---------------------------------------------------------------------------

def desktop_chat_sequence(conversation_id, request_id, message_id,
                          model_id, model_name):
    """桌面端「成功对话」完整事件链（RichMeow/template/playbook/canvas 等共用底座）。"""
    return [
        {"eventCode": "agent_task_created", "source": "LOCAL", "name": "working",
         "task_target": "local", "mode": "craft",
         "requestModelId": model_id, "requestModelName": model_name,
         "has_repo": False, "repo_type": "none", "workspace_type": "empty",
         "has_connector": False, "connector_types": [],
         "has_mention": False, "mention_types": [],
         "has_template": False, "action": "", "template_name": "",
         "has_expert": False, "expert_id": "", "expert_name": "",
         "expert_industry_id": "", "has_skill": False, "skill_names": [],
         "conversationId": conversation_id, "messageId": message_id,
         "buddyId": "", "buddyName": ""},
        {"eventCode": "chat_message_send", "messageId": message_id + "-assistant",
         "historyCount": 0, "isContextTruncated": False, "currentStepCount": 1,
         "traceId": request_id, "rootRequestId": request_id,
         "parentConversationId": conversation_id,
         "agentName": "cli", "agentType": "main"},
        {"eventCode": "chat_request_send", "inputLength": 24, "isPlan": False,
         "isAutoExecuteTerminal": False, "isAutoModify": False,
         "codebaseEnable": False, "maxToken": 0, "maxSteps": 500,
         "temperature": 0, "maxRetries": 0, "mentionContexts": [],
         "knowledgeId": [], "knowledgeName": [], "codebaseId": "",
         "mentionContextCount": 0, "command": "", "recommendId": "",
         "skillId": "", "skillCount": 0, "totalCount": 0,
         "traceId": request_id, "rootRequestId": request_id,
         "parentConversationId": conversation_id,
         "agentName": "cli", "agentType": "main",
         "codebuddy.session_id": conversation_id,
         "codebuddy.conversation_request_id": request_id},
        {"eventCode": "chat_message_response", "messageId": message_id + "-assistant",
         "responseModelId": model_id, "inputToken": 120, "outputToken": 80,
         "totalToken": 200, "cachedTokens": 0, "cachedWriteTokens": 0,
         "cachedMissTokens": 0, "isSuccessful": True, "messageErrorCode": "",
         "finishReason": "stop", "firstTokenAt": now_ms(),
         "traceId": request_id, "conversationId": conversation_id,
         "rootRequestId": request_id, "parentConversationId": conversation_id,
         "agentName": "cli", "agentType": "main",
         "codebuddy.session_id": conversation_id,
         "codebuddy.conversation_request_id": request_id},
        {"eventCode": "chat_message_status", "messageId": message_id + "-assistant",
         "messageErrorCode": "0", "traceId": request_id,
         "rootRequestId": request_id, "parentConversationId": conversation_id,
         "agentName": "cli", "agentType": "main"},
        {"eventCode": "chat_request_response", "mode": "craft", "toolCallCount": 0,
         "inputToken": 120, "outputToken": 80, "totalToken": 200,
         "cachedTokens": 0, "cachedWriteTokens": 0, "cachedMissTokens": 0,
         "isSuccessful": True, "messageErrorCode": "", "finishReason": "stop",
         "rootRequestId": request_id, "parentConversationId": conversation_id},
    ]


def desktop_buddy_app_sequence(buddy_id, buddy_name):
    """「进入 Buddy 应用」五连事件（Buddy_App / Buddy_App_QQ 共用）。"""
    def mk(code, extra=None):
        ev = {"eventCode": code, "mode": "LOCAL",
              "buddyId": buddy_id, "buddyName": buddy_name}
        if extra:
            ev.update(extra)
        return ev
    return [
        mk("buddyapp_discover_click"),
        mk("buddyapp_show", {"elementId": buddy_id, "elementName": buddy_name,
                             "position": 2}),
        mk("buddyapp_enter_click", {"elementId": buddy_id,
                                    "elementName": buddy_name, "position": 2,
                                    "isFirstPage": "1"}),
        mk("buddyapp_auth_confirm_click", {"elementId": buddy_id,
                                           "elementName": buddy_name}),
        mk("buddyapp_bindaccount_skip_click", {"elementId": buddy_id,
                                               "elementName": buddy_name}),
    ]


def desktop_automation_create_event(name):
    """「定时任务创建成功」事件（automation_1 / Sequential_Tasks_4 判据）。"""
    return {"eventCode": "automated_task_create_suc", "name": name,
            "source": "manually", "modelId": "fast-model",
            "modelIsThinking": True, "connectorCount": 0, "skills": "",
            "skillCount": 0, "scheduleType": "once", "mode": "LOCAL"}


def desktop_template_use_sequence(conversation_id, request_id,
                                  template_id, template_name):
    """「使用模板创建任务」事件组（template_5 计数）。"""
    events = desktop_chat_sequence(conversation_id, request_id,
                                   "msg-" + template_id, "fast-model", "fast-model")
    events.append({"eventCode": "agent_task_created_with_template",
                   "mode": "working", "isCustomModel": False,
                   "id": template_id, "name": template_name,
                   "requestId": request_id})
    events.append({"eventCode": "template_used", "template_id": template_id,
                   "task_mode": "working"})
    return events


def desktop_playbook_sequence(conversation_id, request_id, case_id, case_name):
    """「灵感案例做同款」事件组（playbook_prompt / Sequential_Tasks_7 primary）。"""
    events = desktop_chat_sequence(conversation_id, request_id, "msg-pb",
                                   "fast-model", "fast-model")
    payload = {"id": case_id, "name": case_name, "type": "document",
               "categoryId": "", "categoryName": ""}
    ev1 = {"eventCode": "web_element_click", "pageName": "playbook_detail",
           "elementId": "playbook_ctaClick", "elementName": case_name,
           "source": "discover"}
    ev2 = {"eventCode": "playbook_cta_click", "source": "discover", "position": 0}
    ev2.update(payload)
    ev3 = {"eventCode": "playbook_prompt_send", "conversationId": conversation_id,
           "requestId": request_id}
    ev3.update(payload)
    events.extend([ev1, ev2, ev3])
    return events


def desktop_design_canvas_sequence(conversation_id, request_id):
    """「设计创意画布」事件组（create_canvas 计数）。"""
    events = desktop_chat_sequence(conversation_id, request_id, "msg-canvas",
                                   "fast-model", "fast-model")
    tail = request_id[len(request_id) - 8:]
    events.append({"eventCode": "wbx_design_canvas_task_create",
                   "conversationId": conversation_id, "requestId": request_id,
                   "source": "summon_keyword", "cost": 12000,
                   "isSuccessful": True})
    events.append({"eventCode": "wbx_design_canvas_open",
                   "conversationId": conversation_id, "requestId": request_id,
                   "id": "ardot-file-" + tail, "source": "summon_keyword",
                   "type": "page", "cost": 13000, "isSuccessful": True})
    return events


def desktop_expert_summon_sequence(expert):
    """「召唤平台专家」事件组（web_element_click + summon_click + summoned）。"""
    cat = expert.get("categories") or ["expert-all"]
    if not isinstance(cat[0], str) or not cat[0]:
        cat = ["expert-all"]
    ver = expert.get("version") or "1.0.0"
    return [
        {"eventCode": "web_element_click", "source": expert["expert_id"],
         "type": cat[0], "version": ver, "elementId": "expert_summon_click",
         "elementName": "立即召唤",
         "pageURL": "/C:/Program%20Files/WorkBuddy/resources/app.asar/renderer/index.html"},
        {"eventCode": "expert_summon_click", "id": expert["expert_id"],
         "name": expert.get("display_name_zh", ""),
         "expertTitle": expert.get("profession_zh", ""),
         "type": "expert-all", "position": 0,
         "expertType": expert.get("expert_type", ""), "version": ver,
         "mode": "LOCAL"},
        {"eventCode": "expert_summoned", "id": expert["expert_id"],
         "name": expert.get("display_name_zh", ""),
         "expertTitle": expert.get("profession_zh", ""), "type": "expert-all"},
    ]


def desktop_expert_actual_use(expert, conversation_id, request_id, mode="craft",
                              override=None):
    """「专家真实使用」事件（expert_5 / Expert_team_use_3 / lighthouse 计数）。"""
    cat = expert.get("categories") or ["expert-all"]
    if not isinstance(cat[0], str) or not cat[0]:
        cat = ["expert-all"]
    ver = expert.get("version") or "1.0.0"
    tail = request_id[len(request_id) - 8:]
    ev = {
        "eventCode": "expert_actual_use",
        "id": expert["expert_id"], "name": expert.get("display_name_zh", ""),
        "expertTitle": expert.get("profession_zh", ""),
        "type": cat[0], "expertType": expert.get("expert_type", ""),
        "source": "builtin", "version": ver, "cost": 9000, "characterCount": 14,
        "conversationId": conversation_id, "requestId": request_id,
        "messageId": "msg-" + tail,
        "requestModelId": "fast-model", "requestModelName": "fast-model",
        "mode": mode,
    }
    if override:
        ev.update(override)
    return ev


# ---- mp / school 域事件 ----

def mp_chat_event(conversation_id):
    """mp 指纹 chat_request_send（chat_3_times / Sequential_Tasks_1/3 判据形状）。"""
    rid = "wb2api-" + str(now_ms())
    return {
        "eventCode": "chat_request_send", "inputLength": 14, "isPlan": False,
        "isAutoExecuteTerminal": False, "isAutoModify": False,
        "codebaseEnable": False, "maxToken": 0, "maxSteps": 500,
        "temperature": 0, "maxRetries": 0, "mentionContexts": [],
        "knowledgeId": [], "knowledgeName": [], "codebaseId": "",
        "mentionContextCount": 0, "command": "", "recommendId": "",
        "skillId": "", "skillCount": 0, "totalCount": 0,
        "traceId": rid, "rootRequestId": rid,
        "parentConversationId": conversation_id, "conversationId": conversation_id,
        "messageId": "msg-" + rid[len(rid) - 8:],
        "agentName": "mp", "agentType": "main",
        "codebuddy.session_id": conversation_id,
        "codebuddy.conversation_request_id": rid,
    }


def school_season_chat_event(conversation_id):
    """growth 域 school_season「校园日」判据事件（mini chat + activityId）。"""
    ev = mp_chat_event(conversation_id)
    ev["activityId"] = "school_open_day_2026"
    return ev


def mp_chat_model_event(conversation_id, model_id, model_name):
    """mp 对话事件 + 模型字段（Sequential_Tasks_5「使用 GLM5.2」primary）。"""
    ev = mp_chat_event(conversation_id)
    ev["requestModelId"] = model_id
    ev["requestModelName"] = model_name
    return ev


def mini_expert_use_event(expert_id, expert_name, expert_type="agent"):
    """mp 指纹 expert_actual_use（Sequential_Tasks_2 判据，无 conversationId）。"""
    return {
        "eventCode": "expert_actual_use", "reportDelay": 0,
        "extVersion": "2.2.8", "source": "mini_program",
        "id": expert_id, "name": expert_id,
        "expertTitle": expert_name or expert_id, "type": "send_message",
        "characterCount": 12, "expertType": expert_type or "agent",
    }


def mini_playbook_events(case_id, case_name):
    """mp 指纹灵感事件组（Sequential_Tasks_7 的 fallback）。"""
    base = {"id": case_id, "name": case_name, "type": "document",
            "categoryId": "", "categoryName": "", "skills": "",
            "skillNames": ""}
    cta = {"eventCode": "playbook_cta_click", "source": "discover",
           "position": 1, "extVersion": "2.2.8"}
    cta.update(base)
    send = {"eventCode": "playbook_prompt_send", "source": "discover",
            "promptLength": 96, "isOfficial": 1,
            "conversationId": "wb2api-mp-pb-" + str(now_ms()),
            "extVersion": "2.2.8"}
    send.update(base)
    return [cta, send]


def in_night_window():
    """夜猫子计数窗口 23:00–08:00（本地时区）。"""
    h = datetime.now().hour
    return h >= 23 or h < 8


def is_buddy_task_incomplete(err):
    """HTTP 400 + first_buddy 关键词 = 领养门槛未达标（当日不重试）。"""
    return (isinstance(err, UpstreamError) and err.status == 400
            and BUDDY_TASK_INCOMPLETE in str(err).lower())


# ---------------------------------------------------------------------------
# 任务动作实现（对齐 Go autotask.go；每个动作幂等）
# ---------------------------------------------------------------------------

def run_chat5(c):
    """chat_5：按差额补报 chat_request_send。"""
    t = c.task_by_code("chat_5")
    if t is None:
        raise UpstreamError("任务不存在")
    target = t["target"] or 5
    need = target - t["current"]
    if need <= 0:
        return "进度已达标，无需上报"
    for i in range(need):
        cid = "wb2api-chat5-%d-%d" % (now_ms(), i)
        c.report_chat_activity(cid, "")
        if i < need - 1:
            time.sleep(REPORT_GAP)
    return "已补报 %d 条对话事件" % need


def run_first_buddy(c):
    """first_buddy：report（解锁前置）→ agreement → first（+300 分）。"""
    c.report_chat_activity("wb2api-adopt-%d" % now_ms(), "")
    time.sleep(REPORT_GAP)
    c.buddy_agreement()
    try:
        c.buddy_first()
    except UpstreamError as e:
        if is_buddy_task_incomplete(e):
            return "前置已上报，但领养门槛未过（上游要求当日活跃），请稍后重试"
        raise
    return "已领取 Buddy（+300 分 +8 能量）"


def run_model_chat(c):
    """Model_chat_GLM5.2：accept → 真实对话 → 对齐模型上报。"""
    code = "Model_chat_GLM5.2"
    try:
        c.accept_tasks([code])
    except UpstreamError as e:
        log("accept %s: %s（继续走行为链路）" % (code, e))
    time.sleep(REPORT_GAP)
    status, text = c.chat_completion("glm-5.2", "hi，请回复一句话")
    if status < 0 or status >= 400:
        raise UpstreamError("对话失败 http=%s: %s" % (status, str(text)[:160]), status)
    time.sleep(REPORT_GAP)
    c.report_chat_activity("wb2api-glm52-%d" % now_ms(), "",
                           "glm-5.2", "GLM-5.2")
    return "已完成 glm-5.2 对话并上报"


def run_rich_meow(c):
    """RichMeow_Chat：桌面指纹完整对话事件链（三账号实测纯 API 点亮）。"""
    ms = now_ms()
    conv, req = "wb2api-rm-%d" % ms, "wb2api-rm-req-%d" % ms
    msg = "req-%d-user" % ms
    c.report_desktop_events(
        desktop_chat_sequence(conv, req, msg, "fast-model", "fast-model"))
    return "已按桌面端指纹上报完整对话事件链（agent_task_created→chat_response）"


def run_buddy_app(c):
    """Buddy_App / Buddy_App_QQ：buddyapp 进入五连事件（两任务共用）。"""
    c.report_desktop_events(desktop_buddy_app_sequence(
        "cb_y5Dy46tPQGGWtueMxXbe", "企鹅教师助手"))
    return "已上报 buddyapp 进入五连事件（同时覆盖 Buddy_App 与 Buddy_App_QQ）"


def run_automation_create(c):
    """automation_1：定时任务创建事件（纯 API 点亮）。"""
    c.report_desktop_events([desktop_automation_create_event("wb2api 自动化")])
    return "已上报定时任务创建事件"


def run_library_read(c):
    """Library_read：web 域资料库介绍阅读事件。"""
    doc_url = "https://www.workbuddy.cn/space/d/o0KWYeynteVv06UnAZqIFm"
    c.report_web_event("web_element_click", doc_url,
                       "library_doc_intro_click", "WorkBuddy资料库介绍")
    return "已上报资料库介绍阅读事件"


def run_template_use(c):
    """template_5：模板事件组 ×5（一次上报 5 组即 5/5）。"""
    templates = [("1", "深度研究"), ("2", "周报生成"), ("3", "竞品分析"),
                 ("4", "活动策划"), ("5", "代码评审")]
    for i, (tid, tname) in enumerate(templates):
        ms = now_ms()
        conv = "wb2api-tpl-%d-%d" % (ms, i)
        req = "wb2api-tpl-req-%d-%d" % (ms, i)
        c.report_desktop_events(
            desktop_template_use_sequence(conv, req, tid, tname))
        if i < len(templates) - 1:
            time.sleep(0.3)
    return "已上报 template_used ×5"


def run_playbook_prompt(c):
    """playbook_prompt：playbook_cta_click + playbook_prompt_send。"""
    ms = now_ms()
    c.report_desktop_events(desktop_playbook_sequence(
        "wb2api-pb-%d" % ms, "wb2api-pb-req-%d" % ms,
        "pm-gtm-launch-plan", "新产品上市 GTM 发布计划一页纸"))
    return "已上报 playbook_cta_click + playbook_prompt_send"


def run_create_canvas(c):
    """create_canvas：wbx_design_canvas_task_create/open（+300 分）。"""
    ms = now_ms()
    c.report_desktop_events(desktop_design_canvas_sequence(
        "wb2api-canvas-%d" % ms, "wb2api-canvas-req-%d" % ms))
    return "已上报 wbx_design_canvas_task_create/open"


def run_expert_batch(c, expert_type, count):
    """专家召唤+使用公共实现：真实列表 → 召唤链 → 真实 chat → 使用事件。"""
    experts = c.market_expert_list(expert_type)
    if not experts:
        raise UpstreamError("专家市场列表为空")
    ok, fail = 0, 0
    for i, e in enumerate(experts):
        if ok >= count:
            break
        try:
            c.report_desktop_events(desktop_expert_summon_sequence(e))
        except UpstreamError:
            fail += 1
            continue
        conv, req, err = c.desktop_chat_with_expert(e["expert_id"])
        if err is not None:
            fail += 1
            continue
        events = desktop_chat_sequence(conv, req, "msg-" + req[len(req) - 8:],
                                       "fast-model", "fast-model")
        events.append(desktop_expert_actual_use(e, conv, req))
        try:
            c.report_desktop_events(events)
        except UpstreamError:
            fail += 1
            continue
        ok += 1
        if i < len(experts) - 1:
            time.sleep(EXPERT_GAP)
    return "已对 %d 位真实专家完成召唤+使用链（类型 %s）" % (ok, expert_type)


def run_expert_use(c):
    """expert_5：使用 5 个平台专家（三账号实测）。"""
    return run_expert_batch(c, "agent", 5)


def run_expert_team_use(c):
    """Expert_team_use_3：使用 3 个专家团。"""
    return run_expert_batch(c, "team", 3)


def run_appearance(c):
    """Hp_Appearance：set 主题 API 留痕 + 皮肤生效事件。"""
    theme_key = "theme-tkmw7j"  # 和平精英激战金秋
    c.set_appearance_theme(theme_key)
    time.sleep(2)
    c.report_desktop_events([{
        "eventCode": "appearance_skin_apply", "action": "apply",
        "source": "settings_close", "id": theme_key, "vipLevel": 0,
        "series": "", "type": "unknown",
    }])
    return "已设置主题并上报皮肤生效事件"


def run_skill_fresh(c):
    """skill_1：真实对话 + skill_info 技能加载事件。"""
    conv, req, err = c.desktop_chat_with_expert("")
    if err is not None:
        raise UpstreamError("真实对话: %s" % err)
    msg_id = "msg-" + req[len(req) - 8:]
    events = desktop_chat_sequence(conv, req, msg_id, "fast-model", "fast-model")
    for ev in events:
        if ev["eventCode"] == "chat_message_response":
            ev["finishReason"] = "tool_calls"  # 模型发起工具调用语义
    events.append({
        "eventCode": "skill_info", "id": "润泽小馆·日报撰写",
        "skillId": "skill_2097350077599879168", "skillVersion": "1.0.0",
        "toolStatus": "success", "fileCount": 56,
        "source": "workbuddy-desktop",
        "conversationId": conv, "requestId": req, "messageId": msg_id,
        "requestModelId": "fast-model", "requestModelName": "fast-model",
        "traceId": req,
    })
    c.report_desktop_events(events)
    return "已上报真实对话 + skill_info 技能加载事件"


def run_expert_lighthouse(c):
    """Expert_lighthouse：轻量云专家召唤+使用链（chat 链带 has_expert）。"""
    lh_id = "ex_2cvvUZQhDyeJ"
    lh = {"expert_id": lh_id, "expert_type": "agent",
          "display_name_zh": "腾讯轻量云专家", "profession_zh": "腾讯轻量云专家",
          "version": "1.0.2", "categories": []}
    try:
        for e in c.market_expert_list("agent"):
            if e["expert_id"] == lh_id:
                lh = e
                break
    except UpstreamError:
        pass
    c.report_desktop_events(desktop_expert_summon_sequence(lh))
    conv, req, err = c.desktop_chat_with_expert(lh_id)
    if err is not None:
        raise UpstreamError("真实对话: %s" % err)
    events = desktop_chat_sequence(conv, req, "msg-" + req[len(req) - 8:],
                                   "fast-model", "fast-model")
    for ev in events:
        if ev["eventCode"] == "agent_task_created":
            ev["has_expert"] = True
            ev["expert_id"] = lh["expert_id"]
            ev["expert_name"] = lh.get("display_name_zh", "")
            ev["expert_industry_id"] = ""
    # 轻量云样本：type 为空、cost=0、mode=LOCAL
    events.append(desktop_expert_actual_use(
        lh, conv, req, mode="LOCAL", override={"type": "", "cost": 0}))
    c.report_desktop_events(events)
    return "已上报轻量云专家召唤+使用链（真实对话 requestId）"


def run_black_cat(c):
    """black_cat：23:00–08:00 窗口内 glm-5.2 对话补足（窗口外提示稍后）。"""
    if not in_night_window():
        return "当前不在 23:00–08:00 计数窗口，行为不计分；请在夜间窗口重试"
    t = c.task_by_code("black_cat")
    if t is None:
        return "该账号无此任务"
    if t["claimed"] or t["current"] >= t["target"]:
        return "进度已达标，无需补足"
    need = int(t["target"] - t["current"])
    for i in range(need):
        status, _ = c.chat_completion("glm-5.2", "1+1等于几？直接回答。")
        if status < 0 or status >= 400:
            return "完成 %d/%d 次后中断（对话 http=%s）" % (i, need, status)
        c.report_chat_activity("wb2api-night-%d-%d" % (now_ms(), i), "",
                               "glm-5.2", "GLM-5.2")
        if i < need - 1:
            time.sleep(4)
    return "已完成 %d 次夜间对话并上报" % need


# ---------------------------------------------------------------------------
# mp 口径任务通用骨架
# ---------------------------------------------------------------------------

def run_mp_mini_chat_task(c, code, with_activity_id):
    """mp 限定任务闭环：mp 查询 → accept（带验证）→ 按差额补 mini chat → 回读 → 领奖。"""
    t = c.task_by_code(code, mp=True)
    if t is None:
        return "mp 口径未下发该任务（活动可能已结束）"
    if t["claimed"]:
        return "已领取"
    if t["accept_status"] in ("not_accepted", ""):
        if not c.accept_with_verify_mp(code):
            return "accept 未登记生效（上游 200+OK 但未落账形态），待下次重试"
    target = t["target"] or 1
    if t["current"] >= target or t["accept_status"] == "completed":
        credit, energy, _ = c.claim_reward_mp(code)
        return "已领取奖励（+%dc +%de）" % (credit, energy)
    # 判据上报：按差额补 mini chat 事件
    need = target - t["current"]
    for i in range(need):
        conv = "wb2api-mp-%d-%d" % (now_ms(), i)
        ev = (school_season_chat_event(conv) if with_activity_id
              else mp_chat_event(conv))
        try:
            c.report_mp_events([ev])
        except UpstreamError as e:
            return "完成 %d/%d 次上报后中断: %s" % (i, need, e)
        time.sleep(MP_GAP)
    # 回读（异步计分，两轮各隔 3s）
    for _ in range(2):
        time.sleep(CLAIM_POLL_GAP)
        t2 = c.task_by_code(code, mp=True)
        if t2 is None:
            continue
        t = t2
        if t["claimable"] or t["claimed"] or t["current"] >= target:
            break
    if t["claimed"]:
        return "本轮已入账（claimed）"
    if t["current"] < target:
        return "已上报 %d 次但进度未达 %d/%d（异步计分未归账，下次重试）" % (
            need, t["current"], target)
    credit, energy, _ = c.claim_reward_mp(code)
    return "任务点亮并领取奖励（+%dc +%de）" % (credit, energy)


def run_sequential_event_task(c, code, primary, fallback=None):
    """Sequential 链通用骨架：mp 查询 → accept → 判据事件（可补 fallback）→ 回读 → 领奖。"""
    t = c.task_by_code(code, mp=True)
    if t is None:
        return "mp 口径未下发该任务（前置任务未完成或活动未开始）"
    if t["claimed"]:
        return "已领取"
    target = t["target"] or 1
    if t["current"] >= target or t["accept_status"] == "completed":
        credit, energy, _ = c.claim_reward_mp(code)
        return "已领取奖励（+%dc +%de）" % (credit, energy)
    if t["accept_status"] in ("not_accepted", ""):
        if not c.accept_with_verify_mp(code):
            return "accept 未登记生效（任务可能处于每日锁定窗口，等解锁后自动重试）"
    try:
        primary()
    except UpstreamError as e:
        return "判据上报失败: %s" % e
    # 回读（两轮各隔 3s）；未点亮且有 fallback 时补报一轮再读
    for rnd in range(2):
        time.sleep(CLAIM_POLL_GAP)
        t2 = c.task_by_code(code, mp=True)
        if t2 is None:
            continue
        t = t2
        if t["claimable"] or t["claimed"] or t["current"] >= target:
            break
        if rnd == 0 and fallback is not None:
            try:
                fallback()
            except UpstreamError as e:
                return "备选判据上报失败: %s" % e
    if t["claimed"]:
        return "本轮已入账（claimed）"
    if t["current"] < target:
        return "已上报但进度未点亮（判据形态待校正，下次重试）"
    credit, energy, _ = c.claim_reward_mp(code)
    return "任务点亮并领取奖励（+%dc +%de）" % (credit, energy)


def run_school_season(c):
    """school_season「校园日」：mini chat + activityId 上报。"""
    return run_mp_mini_chat_task(c, "school_season", True)


def run_sequential_chat1(c):
    """Sequential_Tasks_1 小程序首对话。"""
    return run_mp_mini_chat_task(c, "Sequential_Tasks_1", False)


def run_sequential_chat5(c):
    """Sequential_Tasks_3 小程序五次对话。"""
    return run_mp_mini_chat_task(c, "Sequential_Tasks_3", False)


def run_sequential_chat10(c):
    """Sequential_Tasks_6 小程序十次对话。"""
    return run_mp_mini_chat_task(c, "Sequential_Tasks_6", False)


def run_sequential_automation(c):
    """Sequential_Tasks_4 小程序定时任务（PC 同源事件判据）。"""
    return run_sequential_event_task(
        c, "Sequential_Tasks_4",
        lambda: c.report_desktop_events(
            [desktop_automation_create_event("wb2api 自动化")]))


def run_sequential_model_chat(c):
    """Sequential_Tasks_5 小程序使用 GLM5.2（primary=mp 模型事件）。"""
    def primary():
        c.report_mp_events([mp_chat_model_event(
            "wb2api-mp-glm-%d" % now_ms(), "glm-5.2", "GLM-5.2")])

    def fallback():
        c.report_chat_activity("wb2api-mp-glm-%d" % now_ms(), "",
                               "glm-5.2", "GLM-5.2")

    return run_sequential_event_task(c, "Sequential_Tasks_5", primary, fallback)


def run_sequential_playbook(c):
    """Sequential_Tasks_7 体验灵感功能（primary=PC 灵感事件组）。"""
    def primary():
        ms = now_ms()
        c.report_desktop_events(desktop_playbook_sequence(
            "wb2api-pb-%d" % ms, "wb2api-pb-req-%d" % ms,
            "pm-gtm-launch-plan", "新产品上市 GTM 发布计划一页纸"))

    def fallback():
        c.report_mp_events(mini_playbook_events(
            "pm-gtm-launch-plan", "新产品上市 GTM 发布计划一页纸"))

    return run_sequential_event_task(c, "Sequential_Tasks_7", primary, fallback)


def run_mini_expert(c):
    """Sequential_Tasks_2 小程序选专家对话：市场 id 前置 → accept → expert_actual_use。"""
    code = "Sequential_Tasks_2"
    t = c.task_by_code(code, mp=True)
    if t is None:
        return "mp 口径未下发该任务（活动可能已结束）"
    if t["claimed"]:
        return "已领取"
    target = t["target"] or 1
    if t["current"] >= target or t["accept_status"] == "completed":
        credit, energy, _ = c.claim_reward_mp(code)
        return "已领取奖励（+%dc +%de）" % (credit, energy)
    # 判据载体前置（accept 之前）：市场真实专家 id
    try:
        experts = c.market_expert_list("")
    except UpstreamError as e:
        return "专家市场不可用（%s），跳过以防半程态" % e
    if not experts:
        return "专家市场列表为空，跳过以防半程态"
    e = experts[0]
    name = e.get("display_name_zh") or e.get("profession_zh") or e["expert_id"]
    if t["accept_status"] in ("not_accepted", ""):
        if not c.accept_with_verify_mp(code):
            return "accept 未登记生效（上游 200+OK 但未落账形态），待下次重试"
    try:
        c.report_mp_events([mini_expert_use_event(
            e["expert_id"], name, e.get("expert_type") or "agent")])
    except UpstreamError as err:
        return "上报 expert_actual_use 失败: %s" % err
    for _ in range(2):
        time.sleep(CLAIM_POLL_GAP)
        t2 = c.task_by_code(code, mp=True)
        if t2 is None:
            continue
        t = t2
        if t["claimable"] or t["claimed"] or t["current"] >= target:
            break
    if t["claimed"]:
        return "本轮已入账（claimed）"
    if t["current"] < target:
        return "已上报但进度未归账（异步计分，下次重试）"
    credit, energy, _ = c.claim_reward_mp(code)
    return "任务点亮并领取奖励（+%dc +%de）" % (credit, energy)


# ---------------------------------------------------------------------------
# 任务表：编号 → 任务（对齐 Go autoActions 表；顺序即执行顺序）
# ---------------------------------------------------------------------------

# (编号, task_code, 说明, 动作函数, 是否尝试型)
TASK_TABLE = [
    (1,  "chat_5",             "上报 5 条对话活跃事件（自动补足差额）", run_chat5, False),
    (2,  "first_buddy",        "上报解锁 → 同意协议 → 领取第一只 Buddy（+300 分）", run_first_buddy, False),
    (3,  "Model_chat_GLM5.2",  "接受任务 → glm-5.2 真实对话一次 → 对齐模型上报", run_model_chat, False),
    (4,  "RichMeow_Chat",      "桌面指纹事件链上报（已验证：纯 API 可点亮）", run_rich_meow, False),
    (5,  "Buddy_App",          "上报「进入 Buddy 应用」事件链", run_buddy_app, False),
    (6,  "Buddy_App_QQ",       "上报「进入企鹅教师助手」事件链", run_buddy_app, False),
    (7,  "automation_1",       "上报「定时任务创建」事件", run_automation_create, False),
    (8,  "Library_read",       "上报「读资料库介绍」事件", run_library_read, False),
    (9,  "template_5",         "上报「使用模板创建任务」事件组 ×5", run_template_use, False),
    (10, "playbook_prompt",    "上报「灵感案例做同款发送 Prompt」事件组", run_playbook_prompt, False),
    (11, "create_canvas",      "上报「设计创意画布创建」事件组（+300 分）", run_create_canvas, False),
    (12, "expert_5",           "真实专家召唤+使用链 ×5（专家市场+真实 chat）", run_expert_use, False),
    (13, "Expert_team_use_3",  "真实专家团召唤+使用链 ×3", run_expert_team_use, False),
    (14, "Hp_Appearance",      "设置主题 API + 皮肤生效事件", run_appearance, False),
    (15, "skill_1",            "真实对话 + skill_info 技能加载事件", run_skill_fresh, False),
    (16, "Expert_lighthouse",  "真实轻量云专家召唤+使用链（chat 链带 has_expert）", run_expert_lighthouse, False),
    (17, "black_cat",          "夜猫子：23:00–08:00 窗口内 glm-5.2 对话补足", run_black_cat, True),
    (18, "school_season",      "校园日（小程序口径）：mini 对话+activityId 上报（+100c+5e）", run_school_season, False),
    (19, "Sequential_Tasks_1", "小程序首对话（+100c+5e）", run_sequential_chat1, False),
    (20, "Sequential_Tasks_2", "小程序选专家对话（市场专家 id → expert_actual_use）（+200c+5e）", run_mini_expert, False),
    (21, "Sequential_Tasks_3", "小程序五次对话（自动补差额）（+300c+5e）", run_sequential_chat5, False),
    (22, "Sequential_Tasks_4", "小程序定时任务（每日零点解锁一环）：PC 同源事件", run_sequential_automation, False),
    (23, "Sequential_Tasks_5", "小程序使用 GLM5.2（mp 模型字段对话事件）", run_sequential_model_chat, False),
    (24, "Sequential_Tasks_6", "小程序十次对话（自动补差额）", run_sequential_chat10, False),
    (25, "Sequential_Tasks_7", "体验灵感功能（PC+mp 双形态事件组）", run_sequential_playbook, False),
]

BY_NUMBER = {row[0]: row for row in TASK_TABLE}
BY_CODE = {row[1]: row for row in TASK_TABLE}
MP_SUFFIX = " [小程序]"


# ---------------------------------------------------------------------------
# 执行器（对齐 Go accountTaskAuto / runAutoAll）
# ---------------------------------------------------------------------------

def accept_pending(c):
    """阶段 0：批量接受尚未接受的任务（默认 + mp 双口径；失败不阻塞）。"""
    for mp in ((False, True) if c.token else (False,)):
        try:
            tasks = c.list_tasks(mp=mp)
        except UpstreamError as e:
            log("拉取%s任务列表失败（不阻塞）: %s" % ("mp" if mp else "", e))
            continue
        codes = [t["task_code"] for t in tasks
                 if t["task_code"] and not t["claimed"] and not t["locked"]
                 and t["accept_status"] not in ("accepted", "completed")]
        if not codes:
            continue
        tag = "(批量接受-mp)" if mp else "(批量接受)"
        try:
            c.accept_tasks(codes, mp=mp)
            log("%s 已接受 %d 个任务" % (tag, len(codes)))
            time.sleep(REPORT_GAP)
        except UpstreamError as e:
            log("%s 接受任务失败（不阻塞后续）: %s" % (tag, e))


def execute_task(c, row):
    """执行单个任务：查状态 → 动作 → 回读 → 自动领奖。返回结果 dict。"""
    num, code, desc, run, attempt = row
    item = {"number": num, "task_code": code, "desc": desc,
            "attempt": attempt, "status": "", "message": ""}
    try:
        before = c.task_by_code(code)
    except UpstreamError as e:
        item["status"], item["message"] = "error", "查询失败: %s" % e
        return item
    if before is None:
        # mp 专属任务回落 mp 列表（双口径）
        try:
            before = c.task_by_code(code, mp=True)
        except UpstreamError as e:
            item["status"], item["message"] = "error", "查询失败: %s" % e
            return item
    if before is None:
        item["status"], item["message"] = "skipped", "该账号无此任务"
        return item
    if before["claimed"] or (before["target"] > 0 and before["current"] >= before["target"]):
        item["status"] = "skipped"
        item["message"] = "已完成（%s）" % task_progress_text(before)
        return item

    is_mp = code in MP_TASK_CODES
    item["progress_before"] = task_progress_text(before)
    try:
        msg = run(c)
    except UpstreamError as e:
        item["status"], item["message"] = "error", str(e)
        return item
    except Exception as e:  # 防御：单任务异常不中断整批
        item["status"], item["message"] = "error", repr(e)
        return item

    # 回读（异步计分有界轮询）
    try:
        after = c.task_by_code_waiting(code, mp=is_mp)
    except UpstreamError:
        after = None
    item["status"] = "done"
    item["message"] = msg
    if after is not None:
        item["progress_after"] = task_progress_text(after)
        # 达标即自动领奖（mp 走 chat 域 mp 口径，其余 Web 端接口）
        if after["claimable"]:
            item["claimable"] = True
            try:
                credit, energy, already = (c.claim_reward_mp(code) if is_mp
                                           else c.claim_reward(code))
                item["claimed"] = True
                item["credit"], item["energy"] = credit, energy
                if already or (credit == 0 and energy == 0):
                    item["message"] = msg + "；奖励此前已领取"
                else:
                    item["message"] = msg + "；已自动领奖 +%d 分 +%d 能" % (
                        credit, energy)
            except UpstreamError as e:
                item["claim_error"] = str(e)
                item["message"] = msg + "；达标但领奖失败（可稍后重试）"
    return item


def format_result_line(r):
    """把单个任务结果渲染为带状态图标的单行文本。"""
    status, msg = r.get("status"), r.get("message") or ""
    prog = r.get("progress_after") or r.get("progress_before") or ""
    if status == "skipped":
        return "   ⏭  跳过: %s" % msg
    if status == "error":
        return "   ✗  失败: %s" % msg
    tail = "（进度 %s）" % prog if prog else ""
    return "   ✓  完成: %s%s" % (msg, tail)


def run_numbers(c, numbers, do_accept=True):
    """按编号顺序执行（队列语义：先批量 accept，再逐项动作，项间节流）。"""
    if not c.token:
        log("提示: 当前为浏览器 Cookie 模式，仅支持任务列表/报名/领奖；"
            "对话和事件上报类任务需要桌面端 Token。")
    if do_accept:
        log("\n准备执行 %d 项任务，先批量接受未接受的任务..." % len(numbers))
        accept_pending(c)
    results = []
    total = len(numbers)
    for seq, n in enumerate(numbers, start=1):
        row = BY_NUMBER.get(n)
        if row is None:
            r = {"number": n, "task_code": "?", "status": "error",
                 "message": "未知编号"}
            results.append(r)
            log("[%d/%d] 任务: %s" % (seq, total, r["task_code"]))
            log(format_result_line(r))
            continue
        log("[%d/%d] 任务: %s" % (seq, total, row[1]))
        r = execute_task(c, row)
        results.append(r)
        log(format_result_line(r))
        time.sleep(REPORT_GAP)  # 项间节流
    return results


def parse_selection(tokens):
    """解析命令行选择：编号 / 区间 / all / 任务 code。返回编号列表（保序去重）。"""
    numbers = []
    for tok in tokens:
        t = tok.strip()
        if not t:
            continue
        if t.lower() == "all":
            for row in TASK_TABLE:
                if row[0] not in numbers:
                    numbers.append(row[0])
            continue
        if "-" in t and t.replace("-", "").isdigit():
            a, _, b = t.partition("-")
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                raise SystemExit("无效区间: %s" % tok)
            for n in range(min(lo, hi), max(lo, hi) + 1):
                if n in BY_NUMBER and n not in numbers:
                    numbers.append(n)
            continue
        if t.isdigit():
            n = int(t)
            if n not in BY_NUMBER:
                raise SystemExit("无效编号: %s（可用 1-%d 或 list 查看）"
                                 % (t, len(TASK_TABLE)))
            if n not in numbers:
                numbers.append(n)
            continue
        if t in BY_CODE:
            n = BY_CODE[t][0]
            if n not in numbers:
                numbers.append(n)
            continue
        raise SystemExit("无法识别的选择: %s（支持编号/区间/all/任务code）" % tok)
    if not numbers:
        raise SystemExit("未选择任何任务")
    return numbers


MENU_WIDTH = 74


def print_menu():
    """打印带边框的任务菜单（CJK 宽度对齐）。"""
    line = "=" * MENU_WIDTH
    print()
    print(line)
    print("  WorkBuddy 成长任务一键完成")
    print(line)
    print("  %s  %s  说明" % (_rjust("编号", 4), _ljust("任务代码", 30)))
    print("-" * MENU_WIDTH)
    for num, code, desc, _run, attempt in TASK_TABLE:
        mark = MP_SUFFIX if code in MP_TASK_CODES else ""
        label = desc + ("（尝试型）" if attempt else "")
        print("  %s  %s  %s" % (
            _rjust(str(num), 4), _ljust(code + mark, 30), _trunc(label, 34)))
    print("-" * MENU_WIDTH)
    print("  输入示例：")
    print("    1               执行第 1 项")
    print("    1 3 5 / 1,3,5   执行第 1、3、5 项")
    print("    1-10            执行第 1~10 项")
    print("    all             执行全部任务")
    print("    list            仅列出任务（不执行）")
    print("    q / 回车         退出")
    print(line)


def summarize(results):
    """汇总结果为一行中文报告 + 计数。"""
    done = [r for r in results if r["status"] == "done"]
    skipped = [r for r in results if r["status"] == "skipped"]
    errors = [r for r in results if r["status"] == "error"]
    claimed = [r for r in results if r.get("claimed")]
    credits = sum(int(r.get("credit") or 0) for r in results)
    energies = sum(int(r.get("energy") or 0) for r in results)
    parts = ["执行 %d 项" % len(results), "完成 %d" % len(done)]
    if skipped:
        parts.append("跳过 %d" % len(skipped))
    if errors:
        parts.append("失败 %d" % len(errors))
    if claimed:
        parts.append("自动领奖 %d 项" % len(claimed))
    if credits or energies:
        parts.append("共 +%d 分 +%d 能" % (credits, energies))
    return "，".join(parts), {"done": len(done), "skipped": len(skipped),
                              "errors": len(errors), "claimed": len(claimed),
                              "credit": credits, "energy": energies}


def build_client():
    """加载桌面端会话并构造客户端；失败返回 (None, 报告文本, 退出码)。"""
    # 1. 优先支持通过环境变量传入明文 Token 和 UID
    env_token = os.environ.get("WORKBUDDY_TOKEN") or os.environ.get("WORKBUDDY_ACCESS_TOKEN")
    env_uid = os.environ.get("WORKBUDDY_UID")
    if env_token:
        if env_token.lower().startswith("bearer "):
            env_token = env_token[7:].strip()
        uid = env_uid or ""
        domain = os.environ.get("WORKBUDDY_DOMAIN") or "www.workbuddy.cn"
        auth_file = find_auth_file()
        if not uid and auth_file and os.path.exists(auth_file):
            try:
                s = load_session(auth_file)
                uid = s.get("account", {}).get("uid") or ""
                domain = s.get("auth", {}).get("domain") or domain
            except Exception:
                pass
        session = {
            "auth": {"accessToken": env_token, "domain": domain},
            "account": {"uid": uid, "nickname": os.environ.get("WORKBUDDY_NICKNAME", "")}
        }
        try:
            client = WorkBuddyClient(session)
            warn_auth_expiry(client)
            return client, "", 0
        except ValueError as e:
            return None, str(e), 1

    # 2. 浏览器登录缓存：Token 优先；仅 Cookie 时先保留，尝试桌面端 Token
    cached = load_auth_cache() or {}
    cached_token = cached.get("accessToken") or ""
    cached_cookies = cached.get("cookies") if isinstance(cached.get("cookies"), list) else []
    token_exp = jwt_expiry_ms(cached_token) if cached_token else 0
    cookie_exp = _cookie_expiry_ms(cached_cookies) if cached_cookies else 0
    if token_exp and token_exp <= now_ms():
        cached_token = ""
    if cookie_exp and cookie_exp <= now_ms():
        cached_cookies = []
    pending_cookies = cached_cookies

    if cached_token:
        cached_uid = cached.get("uid") or jwt_uid(cached_token) or ""
        session = {
            "auth": {"accessToken": cached_token,
                     "domain": cached.get("domain") or "www.workbuddy.cn"},
            "account": {"uid": cached_uid,
                        "nickname": cached.get("nickname") or ""},
            "cookies": pending_cookies,
            "expiresAt": cached.get("expiresAt") or 0,
        }
        try:
            client = WorkBuddyClient(session)
            if client.is_global:
                return None, "global 账号无 CN 成长任务体系，本脚本仅支持 CN 账号", 1
            log("已加载浏览器登录缓存（%s）" % (cached.get("source") or "browser"))
            warn_auth_expiry(client)
            return client, "", 0
        except ValueError as e:
            log("登录缓存不可用: %s" % e)

    def build_cookie_client():
        if not pending_cookies:
            return None
        session = {
            "auth": {"accessToken": None,
                     "domain": cached.get("domain") or "www.workbuddy.cn"},
            "account": {"uid": cached.get("uid") or "",
                        "nickname": cached.get("nickname") or ""},
            "cookies": pending_cookies,
            "expiresAt": cached.get("expiresAt") or 0,
        }
        try:
            client = WorkBuddyClient(session)
            if client.is_global:
                log("global 账号无 CN 成长任务体系，本脚本仅支持 CN 账号")
                return None
            log("已加载浏览器 Web Cookie 登录缓存（仅 Web 成长中心接口可用）")
            warn_auth_expiry(client)
            return client, "", 0
        except ValueError as e:
            log("登录缓存不可用: %s" % e)
            return None

    # 3. 探测桌面端凭据文件
    auth_file = find_auth_file()
    if not auth_file or not os.path.exists(auth_file):
        fallback = build_cookie_client()
        if fallback:
            return fallback
        return None, ("未找到 WorkBuddy 登录凭据。请先在本机登录 WorkBuddy 桌面端，"
                      "或运行 python workbuddy_task.py login 通过浏览器登录，"
                      "或设置环境变量 WORKBUDDY_TOKEN 直接设置明文 Token。"), 2
    try:
        session = load_session(auth_file)
    except Exception as e:
        fallback = build_cookie_client()
        if fallback:
            return fallback
        return None, "读取 WorkBuddy 凭据文件失败: %s" % e, 2
    if not isinstance(session, dict):
        fallback = build_cookie_client()
        if fallback:
            return fallback
        return None, "WorkBuddy 凭据文件格式不正确（顶层不是 JSON 对象）", 2

    auth = session.get("auth") or {}
    raw_token = auth.get("accessToken")

    # 4. 如果是 WorkBuddy 5.6.0+ 的加密信封 ($wbEncrypted)，尝试自动解密
    if isinstance(raw_token, dict) and raw_token.get("$wbEncrypted"):
        log("检测到 WorkBuddy 5.6.0+ 本地加密凭据 ($wbEncrypted)，尝试自动解密...")
        decrypted = try_decrypt_at_rest_token(raw_token)
        if decrypted:
            log("✓ 成功从运行中的 WorkBuddy 进程内存中提取密钥，完成本地凭据自动解密！")
            auth["accessToken"] = decrypted
        else:
            fallback = build_cookie_client()
            if fallback:
                return fallback
            account = session.get("account") or {}
            uid_hint = account.get("uid") or ""
            return None, (
                "WorkBuddy 桌面端凭据已加密 ($wbEncrypted)。\n"
                "自动解密失败（可能原因：未安装 cryptography、WorkBuddy 未运行/无权限，"
                "或凭据版本不匹配）。\n"
                "更推荐运行浏览器登录获取凭据：\n"
                "  python workbuddy_task.py login\n"
                "也可以直接设置环境变量传入明文 Token：\n"
                "  $env:WORKBUDDY_TOKEN = '你的明文Token(以eyJ开头)'"
                + (("\n  $env:WORKBUDDY_UID   = '%s'" % uid_hint) if uid_hint else "")
            ), 1

    if pending_cookies:
        session["cookies"] = pending_cookies
        session["expiresAt"] = cached.get("expiresAt") or 0
    try:
        client = WorkBuddyClient(session)
    except ValueError as e:
        fallback = build_cookie_client()
        if fallback:
            return fallback
        return None, str(e), 1
    if client.is_global:
        return None, "global 账号无 CN 成长任务体系，本脚本仅支持 CN 账号", 1
    warn_auth_expiry(client)
    return client, "", 0


def run_and_report(client, numbers, emit_json=True):
    """执行选定任务；emit_json=True 时额外输出机器可解析的 JSON 明细。

    交互模式（emit_json=False）下只保留逐项日志与收益汇总，便于紧接着询问
    用户是否继续；命令行模式仍输出完整 JSON。
    """
    results = run_numbers(client, numbers)
    report, counts = summarize(results)
    if counts["credit"] or counts["energy"]:
        log("本次共获得：+%d 积分 +%d 能量" % (counts["credit"], counts["energy"]))
    if emit_json:
        log("执行完成 · %s" % report)
        out = {"result": "OK" if counts["errors"] == 0 else "PARTIAL",
               "report": report, "counts": counts, "results": results}
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if counts["errors"] == 0 else 1


def cmd_login(argv):
    preferred = None
    timeout = 300
    manual = False
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--browser", "-b") and i + 1 < len(argv):
            preferred = argv[i + 1]
            i += 2
        elif arg == "--timeout" and i + 1 < len(argv):
            try:
                timeout = int(argv[i + 1])
            except ValueError:
                log("--timeout 需要整数秒")
                return 2
            i += 2
        elif arg in ("--manual", "-m"):
            manual = True
            i += 1
        elif arg in ("-h", "--help"):
            print("用法: python workbuddy_task.py login [--browser chrome|edge|路径] "
                  "[--timeout 300] [--manual]")
            return 0
        else:
            log("未知参数: %s" % arg)
            return 2
    return browser_login(preferred=preferred, timeout=timeout, manual=manual)


def main(argv):
    args = [a for a in argv if a.strip()]

    # list 模式无需登录即可查看菜单
    if args and args[0].lower() in ("list", "menu", "-l"):
        print_menu()
        return 0

    # 浏览器弹窗登录 / 登录状态 / 退出登录
    if args and args[0].lower() in ("login", "auth"):
        return cmd_login(args[1:])
    if args and args[0].lower() in ("status", "whoami"):
        return cmd_status()
    if args and args[0].lower() in ("logout", "signout"):
        return cmd_logout()

    client, err, code = build_client()
    if client is None:
        print(json.dumps({"result": "NO_AUTH", "report": err}, ensure_ascii=False))
        return code

    # 命令行参数模式：一次性执行后退出
    if args:
        try:
            numbers = parse_selection(args)
        except SystemExit as e:
            log(str(e))
            return 1
        return run_and_report(client, numbers)

    # 交互模式：执行完保留本次状态，再询问是否继续；q/quit/exit 退出
    while True:
        print_menu()
        try:
            raw = input("\n请输入选项: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            log("已退出")
            return 0
        if raw.lower() in ("q", "quit", "exit", "退出"):
            log("已退出")
            return 0
        if raw.lower() in ("list", "menu", "-l"):
            continue
        try:
            numbers = parse_selection(re.split(r"[,\s]+", raw))
        except SystemExit as e:
            log(str(e))
            continue
        run_and_report(client, numbers, emit_json=False)
        try:
            again = input("\n继续执行更多任务？[y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            log("已退出")
            return 0
        if again not in ("y", "yes", "是"):
            log("已退出")
            return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main(sys.argv[1:]))
