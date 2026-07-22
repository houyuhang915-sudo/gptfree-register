"""OpenAI Sentinel SDK adapter.

The active provider loads the current SDK, runs its P/Turnstile/SO state
machine in Node, and binds every challenge to one device ID and flow.  The
older Python VM helpers remain below for fixture decoding and regression use.

sentinel token 结构:
  {p: enforcement_token, t: turnstile_proof, c: server_challenge, id: device_id, flow: flow_name}

生成流程:
  1. 由实时 SDK 生成 requirements token
  2. 使用一致的浏览器 persona 和 oai-did 请求 challenge
  3. POST {p, id, flow} → sentinel backend → 获取 chatReq
  4. 从 chatReq 提取 token(c), proofofwork(seed+difficulty), turnstile(dx)
  5. 由 SDK 生成 enforcement token (p) 和 turnstile proof (t)
  6. 执行 collector → 行为流 → snapshot 生成 SO
  7. 构造主/SO 双 header
"""

import json
import base64
import uuid
import random
import time
import math
import asyncio
from datetime import datetime, timezone
from typing import Optional
from curl_cffi import requests


# ============================================================
# FNV-1a 32-bit Hash (与 SDK 中一致)
# ============================================================
def fnv1a_32(s: str) -> str:
    """FNV-1a 32-bit hash, 返回 8 位 hex"""
    h = 2166136261
    for c in s:
        h ^= ord(c)
        h = (h * 16777619) & 0xFFFFFFFF
    h ^= h >> 16
    h = (h * 2246822507) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * 3266489909) & 0xFFFFFFFF
    h ^= h >> 16
    return f"{h:08x}"


# ============================================================
# 浏览器指纹数据 (伪造)
# ============================================================
FAKE_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
FAKE_SCRIPT_URL = "https://sentinel.openai.com/sentinel/20260219f9f6/sdk.js"
FAKE_DATA_BUILD = "070b149f77ea"

# 预定义的 document/window keys 池
DOCUMENT_KEYS = ["location", "referrer", "cookie", "title", "URL", "domain", "body", "head", "documentElement", "scripts", "images", "forms", "links", "anchors", "embeds", "plugins", "applets", "all", "readyState", "designMode", "dir", "lastModified", "height", "width", "visibilityState", "hidden", "fullscreen", "fullscreenEnabled", "pointerLockElement", "pictureInPictureElement", "pictureInPictureEnabled", "rootElement", "activeElement", "styleSheets", "fonts", "fullscreenElement", "characterSet", "charset", "inputEncoding", "compatMode", "contentType", "doctype", "xmlEncoding", "xmlVersion", "xmlStandalone", "implementation", "defaultView", "firstChild", "lastChild", "childElementCount", "children", "firstElementChild", "lastElementChild", "nextSibling", "previousSibling", "nodeName", "nodeValue", "nodeType", "ownerDocument", "parentNode", "parentElement", "textContent", "baseURI", "isConnected", "innerHTML", "outerHTML"]

WINDOW_KEYS = ["location", "navigator", "history", "screen", "document", "window", "self", "top", "parent", "frames", "opener", "closed", "length", "name", "status", "origin", "href", "pathname", "search", "hash", "host", "hostname", "port", "protocol", "performance", "crypto", "fetch", "XMLHttpRequest", "WebSocket", "localStorage", "sessionStorage", "indexedDB", "console", "alert", "confirm", "prompt", "atob", "btoa", "setTimeout", "setInterval", "clearTimeout", "clearInterval", "requestAnimationFrame", "cancelAnimationFrame", "requestIdleCallback", "cancelIdleCallback", "postMessage", "addEventListener", "removeEventListener", "dispatchEvent", "getComputedStyle", "matchMedia", "open", "close", "stop", "focus", "blur", "print", "scroll", "scrollTo", "scrollBy", "scrollX", "scrollY", "pageXOffset", "pageYOffset", "innerWidth", "innerHeight", "outerWidth", "outerHeight", "devicePixelRatio", "screenX", "screenY", "screenLeft", "screenTop", "styleMedia", "visualViewport", "crossOriginIsolated", "isSecureContext", "originAgentCluster"]

NAVIGATOR_KEYS = ["userAgent", "language", "languages", "platform", "vendor", "vendorSub", "product", "productSub", "appName", "appVersion", "appCodeName", "hardwareConcurrency", "deviceMemory", "maxTouchPoints", "cookieEnabled", "onLine", "doNotTrack", "geolocation", "mediaDevices", "permissions", "clipboard", "credentials", "keyboard", "locks", "serviceWorker", "presentation", "usb", "bluetooth", "gamepadInputSource", "wakeLock", "deviceMemory", "connection", "plugins", "mimeTypes", "pdfViewerEnabled", "webkitPersistentStorage", "webkitTemporaryStorage"]

SCRIPT_SRCS = [
    "https://sentinel.openai.com/sentinel/20260219f9f6/sdk.js",
    "https://cdn.oaistatic.com/assets/manifest.js",
    "https://cdn.oaistatic.com/assets/vendor.js",
    "https://cdn.oaistatic.com/assets/main.js",
    "https://cdn.oaistatic.com/assets/runtime.js",
    "https://cdn.oaistatic.com/assets/polyfills.js",
]


def random_from_list(lst):
    return lst[random.randint(0, len(lst) - 1)] if lst else ""


def gather_fingerprint_data(sid: str) -> list:
    """
    收集浏览器指纹数据 (25项), 与 SDK initializeAndGatherData() 一致。
    索引 [3] 和 [9] 会被 PoW 覆写。
    """
    now_str = str(datetime.now(timezone.utc).astimezone())
    perf_now = round(time.time() * 1000 - 1000000 + random.uniform(1000, 5000), 1)
    time_origin = round(time.time() * 1000 - 50000, 1)

    nav_prop = random_from_list(NAVIGATOR_KEYS)
    nav_val = _fake_navigator_value(nav_prop)

    return [
        1920 + 1080,                          # [0] screen.width + screen.height
        now_str,                                # [1] "" + new Date()
        4294705152,                             # [2] performance.memory.jsHeapSizeLimit
        0,                                      # [3] Math.random() → 覆写为 nonce
        FAKE_USER_AGENT,                        # [4] navigator.userAgent
        random_from_list(SCRIPT_SRCS),          # [5] random script src
        None,                                   # [6] data-build (null if not found)
        "en-US",                                # [7] navigator.language
        "en-US,en",                             # [8] navigator.languages.join(",")
        0,                                      # [9] Math.random() → 覆写为 elapsed time
        f"{nav_prop}\u2212{nav_val}",           # [10] random navigator prop + "−" + value
        random_from_list(DOCUMENT_KEYS),        # [11] random document key
        random_from_list(WINDOW_KEYS),          # [12] random window key
        perf_now,                               # [13] performance.now()
        sid,                                    # [14] session ID (UUID)
        "",                                     # [15] URL search params keys
        8,                                      # [16] navigator.hardwareConcurrency
        time_origin,                            # [17] performance.timeOrigin
        0,                                      # [18] "ai" in window
        0,                                      # [19] "answers" in window
        0,                                      # [20] "cache" in window
        0,                                      # [21] "data" in window
        0,                                      # [22] "required" in window
        0,                                      # [23] "match" in window
        0,                                      # [24] "stringify" in window
    ]


def _fake_navigator_value(prop: str) -> str:
    """伪造 navigator 属性值"""
    values = {
        "userAgent": FAKE_USER_AGENT,
        "language": "en-US",
        "languages": "en-US,en",
        "platform": "Win32",
        "vendor": "Google Inc.",
        "vendorSub": "",
        "product": "Gecko",
        "productSub": "20030107",
        "appName": "Netscape",
        "appVersion": "5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
        "hardwareConcurrency": "8",
        "deviceMemory": "8",
        "maxTouchPoints": "0",
        "cookieEnabled": "true",
        "onLine": "true",
        "doNotTrack": "null",
        "pdfViewerEnabled": "true",
    }
    return values.get(prop, "undefined")


def encode_data(data: list) -> str:
    """N(data): JSON.stringify → TextEncoder.encode → btoa(String.fromCharCode(...))"""
    json_str = json.dumps(data, separators=(",", ":"))
    # base64 encode UTF-8 bytes
    encoded = base64.b64encode(json_str.encode("utf-8")).decode("ascii")
    return encoded


# ============================================================
# PoW Solver
# ============================================================
def solve_pow(seed: str, difficulty: str, data: list, max_attempts: int = 500000) -> str:
    """
    PoW 求解: 找到 nonce 使 fnv1a(seed + encode(data)) 的前缀 <= difficulty
    返回: encoded_data + "~S" 或 error token
    """
    start_time = time.perf_counter()
    try:
        for i in range(max_attempts):
            data[3] = i  # nonce
            data[9] = round((time.perf_counter() - start_time) * 1000)  # elapsed ms
            encoded = encode_data(data)
            h = fnv1a_32(seed + encoded)
            if h[:len(difficulty)] <= difficulty:
                return encoded + "~S"
    except Exception as e:
        return "gAAAAAB" + encode_data(str(e))

    # 超过最大尝试次数
    return "gAAAAAB" + encode_data("e")


def generate_requirements_token(sid: str) -> str:
    """
    生成 requirements token (内部 seed, difficulty "0")
    返回: "gAAAAAC" + PoW answer
    """
    seed = str(random.random())  # this.requirementsSeed
    data = gather_fingerprint_data(sid)
    answer = solve_pow(seed, "0", data)
    return "gAAAAAC" + answer


def generate_enforcement_token(chat_req: dict, sid: str) -> str:
    """
    生成 enforcement token (server seed + difficulty)
    返回: "gAAAAAB" + PoW answer
    """
    pow_info = chat_req.get("proofofwork", {})
    seed = pow_info.get("seed", "")
    difficulty = pow_info.get("difficulty", "0")

    if not seed or not difficulty:
        return "gAAAAAB" + encode_data("e")

    data = gather_fingerprint_data(sid)
    answer = solve_pow(seed, difficulty, data)
    return "gAAAAAB" + answer


# ============================================================
# Sentinel VM (逆向自 sentinel SDK)
# ============================================================

# 操作码常量 (与 SDK 中一致)
OP_NESTED_VM    = 0   # $t: 运行子 VM (nested dx)
OP_XOR          = 1   # Ft: XOR 解密
OP_SET          = 2   # Lt: 设置值
OP_RESOLVE      = 3   # Jt: resolve 回调
OP_REJECT       = 4   # Gt: reject 回调
OP_PUSH_CONCAT  = 5   # Wt: 数组 push / 字符串拼接
OP_PROP_ACCESS  = 6   # zt: 属性访问 obj[key]
OP_CALL         = 7   # Vt: 调用函数
OP_COPY         = 8   # Bt: 复制值
OP_QUEUE        = 9   # Zt: 指令队列
OP_WINDOW       = 10  # Kt: window (global)
OP_FIND_SCRIPT  = 11  # Qt: 按 src 查找 script
OP_GET_MAP      = 12  # Yt: 获取 VM Map 本身
OP_TRY_IGNORE   = 13  # Xt: try/catch 忽略错误
OP_JSON_STRINGIFY = 14  # tn: JSON.stringify
OP_JSON_PARSE   = 15  # nn: JSON.parse
OP_KEY          = 16  # en: XOR key (cachedProof)
OP_TRY_CALL     = 17  # rn: try/call (async aware)
OP_ATOB         = 18  # on: atob (base64 decode)
OP_BTOA         = 19  # cn: btoa (base64 encode)
OP_EQ_CHECK    = 20  # un: 相等判断 (条件调用)
OP_APPROX_CHECK = 21 # an: 近似判断 (Math.abs)
OP_RUN_NESTED  = 22  # fn: 运行嵌套 VM
OP_UNDEF_CHECK = 23  # sn: undefined 判断
OP_PROP_BIND   = 24  # Ht: 属性方法 bind
OP_NOOP1       = 25  # ln: noop
OP_NOOP2       = 26  # dn: noop
OP_SPLICE_SUB  = 27  # hn: splice/subtract
OP_NOOP3       = 28  # pn: noop
OP_LT          = 29  # mn: less-than
OP_DEFINE_FUNC = 30  # gn: 定义函数 (带闭包)
OP_MULTIPLY    = 33  # wn: 乘法
OP_PROMISE_RES = 34  # yn: Promise.resolve
OP_DIVIDE      = 35  # vn: 除法


def _xor_decrypt(data: str, key: str) -> str:
    """XOR 解密 (对应 SDK 中的 Tn 函数)"""
    result = []
    for i, ch in enumerate(data):
        result.append(chr(ord(ch) ^ ord(key[i % len(key)])))
    return "".join(result)


def _run_vm(instructions: list, xor_key: str, chat_req: dict) -> str:
    """
    执行 VM 指令序列。
    返回 base64 编码的结果字符串。
    """
    import base64

    # VM 状态: Map 存储变量
    vm_map = {}
    vm_map[OP_KEY] = xor_key
    vm_map[OP_WINDOW] = {}  # 模拟 window
    vm_map[OP_QUEUE] = []   # 指令队列

    # 设置 resolve/reject 回调
    result_holder = {"value": None, "error": None}

    def resolve(val):
        if result_holder["value"] is None:
            result_holder["value"] = val

    def reject(val):
        if result_holder["error"] is None:
            result_holder["error"] = val

    vm_map[OP_RESOLVE] = resolve
    vm_map[OP_REJECT] = reject

    # 注册操作码处理函数
    def op_nested_vm(dest, dx_val):
        """运行子 VM"""
        try:
            decoded = base64.b64decode(dx_val).decode("utf-8", errors="replace")
            decrypted = _xor_decrypt(decoded, str(vm_map.get(OP_KEY, "")))
            sub_instrs = json.loads(decrypted)
            sub_result = _run_vm(sub_instrs, str(vm_map.get(OP_KEY, "")), chat_req)
            vm_map[dest] = sub_result
        except Exception as e:
            vm_map[dest] = str(e)

    def op_xor(dest, src_key):
        """XOR 解密"""
        data = str(vm_map.get(dest, ""))
        key = str(vm_map.get(src_key, ""))
        vm_map[dest] = _xor_decrypt(data, key)

    def op_set(dest, val):
        """设置值"""
        vm_map[dest] = val

    def op_push_concat(dest, src):
        """数组 push / 字符串拼接"""
        cur = vm_map.get(dest)
        val = vm_map.get(src)
        if isinstance(cur, list):
            cur.append(val)
        else:
            vm_map[dest] = str(cur) + str(val)

    def op_prop_access(dest, obj_key, prop_key):
        """属性访问 obj[key]"""
        obj = vm_map.get(obj_key, {})
        prop = vm_map.get(prop_key, "")
        try:
            if isinstance(obj, dict):
                vm_map[dest] = obj.get(prop, None)
            elif isinstance(obj, str):
                vm_map[dest] = obj[int(prop)] if prop.isdigit() else None
            else:
                vm_map[dest] = getattr(obj, str(prop), None)
        except Exception:
            vm_map[dest] = None

    def op_call(func_key, *arg_keys):
        """调用函数"""
        func = vm_map.get(func_key)
        args = [vm_map.get(k) for k in arg_keys]
        if callable(func):
            try:
                result = func(*args)
                return result
            except Exception as e:
                return str(e)
        return None

    def op_try_call(dest, func_key, *arg_keys):
        """try/call (async aware)"""
        func = vm_map.get(func_key)
        args = [vm_map.get(k) for k in arg_keys]
        if callable(func):
            try:
                result = func(*args)
                vm_map[dest] = result
            except Exception as e:
                vm_map[dest] = str(e)
        else:
            vm_map[dest] = None

    def op_try_ignore(dest, func_key, *arg_keys):
        """try/catch 忽略错误"""
        func = vm_map.get(func_key)
        args = [vm_map.get(k) for k in arg_keys]
        if callable(func):
            try:
                func(*args)
            except Exception:
                pass

    def op_copy(dest, src):
        """复制值"""
        vm_map[dest] = vm_map.get(src)

    def op_json_stringify(dest, src):
        """JSON.stringify"""
        vm_map[dest] = json.dumps(vm_map.get(src), ensure_ascii=False)

    def op_json_parse(dest, src):
        """JSON.parse"""
        try:
            vm_map[dest] = json.loads(vm_map.get(src))
        except Exception:
            vm_map[dest] = None

    def op_atob(dest):
        """base64 decode"""
        try:
            vm_map[dest] = base64.b64decode(str(vm_map.get(dest, ""))).decode("utf-8", errors="replace")
        except Exception:
            pass

    def op_btoa(dest):
        """base64 encode"""
        try:
            vm_map[dest] = base64.b64encode(str(vm_map.get(dest, "")).encode("utf-8")).decode("utf-8")
        except Exception:
            pass

    def op_eq_check(dest, a_key, b_key, func_key, *extra):
        """相等判断"""
        if vm_map.get(dest) == vm_map.get(a_key):
            func = vm_map.get(b_key)
            if callable(func):
                func(*[vm_map.get(k) for k in extra])

    def op_undef_check(dest, func_key, *arg_keys):
        """undefined 判断"""
        if vm_map.get(dest) is not None:
            func = vm_map.get(func_key)
            if callable(func):
                func(*[vm_map.get(k) for k in arg_keys])

    def op_prop_bind(dest, obj_key, prop_key):
        """属性方法 bind"""
        obj = vm_map.get(obj_key, {})
        prop = vm_map.get(prop_key, "")
        try:
            if isinstance(obj, dict):
                val = obj.get(prop, None)
            else:
                val = getattr(obj, str(prop), None)
            if callable(val):
                vm_map[dest] = val
            else:
                vm_map[dest] = val
        except Exception:
            vm_map[dest] = None

    def op_splice_sub(dest, src):
        """splice/subtract"""
        cur = vm_map.get(dest)
        val = vm_map.get(src)
        if isinstance(cur, list):
            try:
                cur.remove(val)
            except ValueError:
                pass
        else:
            try:
                vm_map[dest] = cur - val
            except Exception:
                vm_map[dest] = 0

    def op_lt(dest, a_key, b_key):
        """less-than"""
        vm_map[dest] = vm_map.get(a_key) < vm_map.get(b_key)

    def op_multiply(dest, a_key, b_key):
        """乘法"""
        try:
            vm_map[dest] = float(vm_map.get(a_key, 0)) * float(vm_map.get(b_key, 0))
        except Exception:
            vm_map[dest] = 0

    def op_divide(dest, a_key, b_key):
        """除法"""
        try:
            b = float(vm_map.get(b_key, 0))
            vm_map[dest] = 0 if b == 0 else float(vm_map.get(a_key, 0)) / b
        except Exception:
            vm_map[dest] = 0

    def op_promise_resolve(dest, val):
        """Promise.resolve (简化为直接赋值)"""
        vm_map[dest] = val

    def op_define_func(dest, ret_key, *closure_keys):
        """定义函数 (带闭包)"""
        is_array = isinstance(closure_keys[-1], list) if closure_keys else False
        if is_array:
            params = closure_keys[-2] if len(closure_keys) >= 2 else []
            body_instrs = closure_keys[-1]
        else:
            params = []
            body_instrs = closure_keys[-1] if closure_keys else []

        def vm_func(*args):
            saved_queue = vm_map.get(OP_QUEUE, [])
            for i, p in enumerate(params):
                vm_map[p] = args[i] if i < len(args) else None
            vm_map[OP_QUEUE] = list(body_instrs)
            _execute_queue(vm_map, op_handlers)
            result = vm_map.get(ret_key)
            vm_map[OP_QUEUE] = saved_queue
            return result

        vm_map[dest] = vm_func

    def op_run_nested(dest, *args):
        """运行嵌套 VM"""
        try:
            saved_queue = list(vm_map.get(OP_QUEUE, []))
            vm_map[OP_QUEUE] = list(args)
            _execute_queue(vm_map, op_handlers)
            result = vm_map.get(dest)
            vm_map[OP_QUEUE] = saved_queue
            vm_map[dest] = str(result) if result is not None else ""
        except Exception as e:
            vm_map[dest] = str(e)

    # 操作码映射表
    op_handlers = {
        OP_NESTED_VM: op_nested_vm,
        OP_XOR: op_xor,
        OP_SET: op_set,
        OP_RESOLVE: resolve,
        OP_REJECT: reject,
        OP_PUSH_CONCAT: op_push_concat,
        OP_PROP_ACCESS: op_prop_access,
        OP_CALL: op_call,
        OP_COPY: op_copy,
        OP_TRY_CALL: op_try_call,
        OP_TRY_IGNORE: op_try_ignore,
        OP_JSON_STRINGIFY: op_json_stringify,
        OP_JSON_PARSE: op_json_parse,
        OP_ATOB: op_atob,
        OP_BTOA: op_btoa,
        OP_EQ_CHECK: op_eq_check,
        OP_UNDEF_CHECK: op_undef_check,
        OP_PROP_BIND: op_prop_bind,
        OP_SPLICE_SUB: op_splice_sub,
        OP_LT: op_lt,
        OP_MULTIPLY: op_multiply,
        OP_DIVIDE: op_divide,
        OP_PROMISE_RES: op_promise_resolve,
        OP_DEFINE_FUNC: op_define_func,
        OP_RUN_NESTED: op_run_nested,
    }

    # 将所有 handler 注册到 vm_map (使 OP_COPY 可以复制它们到动态 key)
    for op_code, handler in op_handlers.items():
        vm_map[op_code] = handler

    # 执行指令
    vm_map[OP_QUEUE] = list(instructions)
    _execute_queue(vm_map, op_handlers)

    if result_holder["error"]:
        return base64.b64encode(str(result_holder["error"]).encode("utf-8")).decode("utf-8")
    if result_holder["value"] is not None:
        val = result_holder["value"]
        if isinstance(val, str):
            return base64.b64encode(val.encode("utf-8")).decode("utf-8")
        return base64.b64encode(str(val).encode("utf-8")).decode("utf-8")
    return ""


def _execute_queue(vm_map: dict, handlers: dict):
    """执行指令队列 - opcodes 可以是动态浮点数 key (通过 OP_COPY 创建)"""
    queue = vm_map.get(OP_QUEUE, [])
    while queue:
        instr = queue.pop(0)
        if not isinstance(instr, list) or len(instr) == 0:
            continue
        opcode = instr[0]
        args = instr[1:]
        # 先从 vm_map 查找 handler (可能是动态创建的浮点数 key)
        handler = vm_map.get(opcode)
        # 如果 vm_map 中没有, 尝试从固定 handlers 中查找
        if not callable(handler):
            handler = handlers.get(opcode)
        if callable(handler):
            try:
                handler(*args)
            except Exception:
                pass


# ============================================================
# Turnstile VM
# ============================================================
def run_turnstile_vm(chat_req: dict, dx: str) -> Optional[str]:
    """
    运行 turnstile VM 生成 t 字段。
    dx: base64 编码的 XOR 加密字节码
    key: cachedProof (requirements token)
    """
    import base64
    # 获取 XOR key (从 WeakMap 中获取, 对应 cachedProof)
    # 在 _init 中 cachedProof 被设置为 requirements token
    # 这里需要从外部传入, 暂时用 chat_req 中的 token 作为 key
    # 实际上 key 是 cachedProof (gAAAAAC... 开头)

    # 我们需要修改调用方式来传入 key
    # 暂时返回 None, 由 get_token 传入
    return None


def run_turnstile_vm_with_key(chat_req: dict, dx: str, xor_key: str, flow: str = "oauth_create_account") -> Optional[str]:
    """运行 turnstile VM (通过 Node.js + jsdom 执行 SDK)"""
    return _run_vm_via_node(chat_req, xor_key, "turnstile", flow)


def _run_vm_via_node(chat_req: dict, xor_key: str, vm_type: str, flow: str = "oauth_create_account") -> Optional[str]:
    """通过 Node.js 运行 sentinel SDK VM, 返回 t 或 so"""
    import subprocess, os, tempfile

    base_dir = os.path.dirname(os.path.abspath(__file__))
    # Prefer sibling gen_token_jsdom.js; fall back to sentinel_vm/ subdir
    candidates = [
        os.path.join(base_dir, "gen_token_jsdom.js"),
        os.path.join(base_dir, "sentinel_vm", "gen_token_jsdom.js"),
    ]
    gen_script = next((p for p in candidates if os.path.isfile(p)), None)
    if not gen_script:
        return None
    work_dir = os.path.dirname(gen_script)

    # 准备输入
    input_data = {
        "chatReq": chat_req,
        "flow": flow,
        "deviceId": str(uuid.uuid4()),
        "cachedProof": xor_key,
    }

    # 写唯一临时文件 (避免并发冲突)
    fd, input_file = tempfile.mkstemp(suffix=".json", prefix="sentinel_", dir=work_dir)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(input_data, f)

        result = subprocess.run(
            ["node", gen_script, input_file],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=base_dir,
        )

        # 从输出中提取 JSON_OUTPUT
        output = result.stdout
        marker = "=== JSON_OUTPUT ==="
        if marker in output:
            json_str = output[output.index(marker) + len(marker):].strip()
            data = json.loads(json_str)
            if vm_type == "so":
                so = data.get("so")
                return so if so else None
            else:
                t = data.get("t")
                return t if t else None
        else:
            if result.stderr:
                print(f"    [SENTINEL] Node.js stderr: {result.stderr[:200]}")
            return None
    except subprocess.TimeoutExpired:
        print("    [SENTINEL] Node.js VM timeout")
        return None
    except Exception as e:
        print(f"    [SENTINEL] Node.js VM error: {e}")
        return None
    finally:
        try:
            os.unlink(input_file)
        except OSError:
            pass


# ============================================================
# Session Observer VM
# ============================================================
def run_session_observer_vm(collector_dx: str) -> Optional[str]:
    """运行 session observer VM (旧接口, 返回 None)"""
    return None


def run_session_observer_vm_with_key(collector_dx: str, xor_key: str, chat_req: dict = None, flow: str = "oauth_create_account") -> Optional[str]:
    """运行 session observer VM (通过 Node.js)"""
    if chat_req:
        return _run_vm_via_node(chat_req, xor_key, "so", flow)
    return None


# ============================================================
# Sentinel Token Provider
# ============================================================
class SentinelTokenProvider:
    """
    OpenAI Sentinel Token 生成器 (纯算法)。

    流程:
      1. 生成 requirements token
      2. POST 到 sentinel backend 获取 chatReq
      3. 生成 enforcement token
      4. 构造 {p, t, c, id, flow}
    """

    BACKEND_URL = "https://sentinel.openai.com/backend-api/sentinel/"
    FRAME_REFERER = "https://sentinel.openai.com/backend-api/sentinel/frame.html?sv=20260219f9f6"

    def __init__(self, impersonate: str = "chrome136", cookies: dict = None):
        self.impersonate = impersonate
        self._session: Optional[requests.AsyncSession] = None
        self.sid = str(uuid.uuid4())
        self._cookies = cookies or {}
        # 缓存
        self._cached_proof: Optional[str] = None
        self._cached_chat_req: Optional[dict] = None
        self._last_fetch_time: float = 0
        self._device_id: str = ""
        self._cached_flow: str = ""
        self._cached_main_token: Optional[dict] = None
        self._cached_so_token: Optional[dict] = None
        self._sdk_url: str = ""
        self._sdk_source: str = ""

    def _browser_profile(self, device_id: str):
        from gpt_trial_protocol.models import BrowserProfile

        return BrowserProfile(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/136.0.0.0 Safari/537.36"
            ),
            sec_ch_ua='"Chromium";v="136", "Not=A?Brand";v="24", "Google Chrome";v="136"',
            language="en-US",
            device_id=device_id,
        )

    @staticmethod
    def _flow_page(flow: str) -> tuple[str, str]:
        if flow == "username_password_create":
            return (
                "https://auth.openai.com/create-account/password",
                "https://auth.openai.com/email-verification",
            )
        return (
            "https://auth.openai.com/about-you",
            "https://auth.openai.com/create-account/password",
        )

    async def _get_session(self) -> requests.AsyncSession:
        if not self._session:
            self._session = requests.AsyncSession(impersonate=self.impersonate)
        return self._session

    async def _post_proof(self, proof: str, flow: str) -> dict:
        """POST proof 到 sentinel backend, 获取 chatReq"""
        s = await self._get_session()
        body = json.dumps({"p": proof, "id": self._device_id, "flow": flow})
        url = self.BACKEND_URL + "req"
        from gpt_trial_protocol.sentinel_http import sentinel_headers

        profile = self._browser_profile(self._device_id)
        for attempt in range(3):
            try:
                resp = await s.post(url, data=body, headers={
                    **sentinel_headers(
                        profile,
                        referer=self.FRAME_REFERER,
                        content_type="text/plain;charset=UTF-8",
                    ),
                    "origin": "https://sentinel.openai.com",
                }, cookies=self._cookies)
                data = resp.json()
                if not isinstance(data, dict):
                    return {"error": "sentinel response is not an object"}
                return data
            except Exception as e:
                if attempt >= 2:
                    return {"error": str(e), "p": proof}
        return {"error": "max retries", "p": proof}

    async def _init(self, flow: str, device_id: str) -> None:
        """Load the live SDK, generate requirements P, and fetch one flow challenge."""
        from gpt_trial_protocol.sentinel_http import (
            DEFAULT_SENTINEL_SDK_URL,
            SENTINEL_BOOTSTRAP_URL,
            SentinelSdkExecutor,
            extract_sentinel_sdk_url,
            sentinel_headers,
            sentinel_version_from_url,
        )

        self._device_id = device_id
        profile = self._browser_profile(device_id)
        session = await self._get_session()
        bootstrap_response = await session.get(
            SENTINEL_BOOTSTRAP_URL,
            headers=sentinel_headers(profile, referer="https://auth.openai.com/"),
        )
        bootstrap = str(bootstrap_response.text or "")
        sdk_url = extract_sentinel_sdk_url(bootstrap) or DEFAULT_SENTINEL_SDK_URL
        sdk_response = await session.get(
            sdk_url,
            headers=sentinel_headers(profile, referer="https://auth.openai.com/"),
        )
        sdk_source = str(sdk_response.text or "")
        if not sdk_source or "SentinelSDK" not in sdk_source:
            raise RuntimeError("live Sentinel SDK source is empty or invalid")
        version = sentinel_version_from_url(sdk_url) or ""
        self.FRAME_REFERER = (
            "https://sentinel.openai.com/backend-api/sentinel/frame.html"
            f"?sv={version}"
        )
        cookie_header = "; ".join(
            f"{cookie.name}={cookie.value}" for cookie in session.cookies.jar
        )
        if "oai-did=" not in cookie_header:
            cookie_header = "; ".join(
                part for part in (cookie_header, f"oai-did={device_id}") if part
            )
        page_url, referer = self._flow_page(flow)
        executor = SentinelSdkExecutor()
        proof = await asyncio.to_thread(
            executor.proof,
            sdk_source=sdk_source,
            sdk_url=sdk_url,
            flow=flow,
            profile=profile,
            document_cookie=cookie_header,
            page_url=page_url,
            referer=referer,
        )
        self._cached_proof = proof
        chat_req = await self._post_proof(proof, flow)
        self._cached_chat_req = chat_req
        self._cached_flow = flow
        self._cached_main_token = None
        self._cached_so_token = None
        self._sdk_url = sdk_url
        self._sdk_source = sdk_source
        self._last_fetch_time = time.time()

    def invalidate_cache(self) -> None:
        """丢弃 chatReq 缓存，下次 get_token 会重新向 sentinel backend 申请。"""
        self._cached_chat_req = None
        self._cached_proof = None
        self._cached_flow = ""
        self._cached_main_token = None
        self._cached_so_token = None
        self._last_fetch_time = 0

    async def get_token(self, flow: str, device_id: str, *, force_refresh: bool = False) -> Optional[dict]:
        """
        生成 sentinel token。
        flow: "authorize_continue" | "username_password_create" | "oauth_create_account"
        """
        if force_refresh:
            self.invalidate_cache()
        # Challenges are flow-bound. Never reuse a password challenge for create_account.
        if (
            not self._cached_chat_req
            or self._cached_flow != flow
            or time.time() - self._last_fetch_time > 540
        ):
            await self._init(flow, device_id)

        chat_req = self._cached_chat_req
        if not chat_req or "error" in chat_req:
            return None

        from gpt_trial_protocol.sentinel_http import SentinelSdkExecutor

        profile = self._browser_profile(device_id)
        session = await self._get_session()
        cookie_header = "; ".join(
            f"{cookie.name}={cookie.value}" for cookie in session.cookies.jar
        )
        if "oai-did=" not in cookie_header:
            cookie_header = "; ".join(
                part for part in (cookie_header, f"oai-did={device_id}") if part
            )
        page_url, referer = self._flow_page(flow)
        solved = await asyncio.to_thread(
            SentinelSdkExecutor().solve,
            sdk_source=self._sdk_source,
            sdk_url=self._sdk_url,
            flow=flow,
            profile=profile,
            document_cookie=cookie_header,
            cached_chat_req=chat_req,
            cached_proof=self._cached_proof or "",
            page_url=page_url,
            referer=referer,
        )
        try:
            token = json.loads(solved["token"])
        except Exception as exc:
            raise RuntimeError(f"invalid Sentinel main token JSON: {exc}") from exc
        if not isinstance(token, dict):
            raise RuntimeError("invalid Sentinel main token shape")
        required_fields = {"p", "c", "id", "flow"}
        if not required_fields.issubset(token):
            raise RuntimeError(f"Sentinel main token missing fields: {required_fields - set(token)}")
        if chat_req.get("turnstile", {}).get("required") and not token.get("t"):
            token["_turnstile_missing"] = True
        self._cached_main_token = token
        so_raw = solved.get("so_token") or ""
        try:
            so_token = json.loads(so_raw) if so_raw else None
        except Exception as exc:
            raise RuntimeError(f"invalid Sentinel SO token JSON: {exc}") from exc
        self._cached_so_token = so_token if isinstance(so_token, dict) else None
        return dict(token)

    async def get_so_token(self, flow: str, device_id: str) -> Optional[dict]:
        """生成 sentinel so-token (session observer)"""
        if self._cached_flow != flow or self._cached_main_token is None:
            await self.get_token(flow, device_id)
        return dict(self._cached_so_token) if self._cached_so_token else None

    async def close(self):
        if self._session:
            await self._session.close()


# ============================================================
# 测试
# ============================================================
async def test():
    print("=== Sentinel Token 生成器测试 ===\n")

    provider = SentinelTokenProvider(cookies={"oai-did": str(uuid.uuid4())})
    device_id = str(uuid.uuid4())

    print(f"[1] 生成 requirements token...")
    proof = generate_requirements_token(provider.sid)
    print(f"    ✓ proof: {proof[:60]}...")

    print(f"\n[2] POST 到 sentinel backend...")
    provider._device_id = device_id
    chat_req = await provider._post_proof(proof, "authorize_continue")
    print(f"    ✓ response keys: {list(chat_req.keys()) if isinstance(chat_req, dict) else 'N/A'}")
    print(f"    ✓ token: {str(chat_req.get('token', ''))[:60]}...")
    print(f"    ✓ proofofwork: {chat_req.get('proofofwork', {})}")
    print(f"    ✓ turnstile: {chat_req.get('turnstile', {})}")
    print(f"    ✓ so: {chat_req.get('so', {})}")
    if 'error' in chat_req:
        print(f"    ✗ error: {chat_req.get('error')}")
        print(f"    full response: {json.dumps(chat_req)[:200]}")

    if chat_req.get("proofofwork"):
        print(f"\n[3] 生成 enforcement token...")
        p_token = generate_enforcement_token(chat_req, provider.sid)
        print(f"    ✓ p_token: {p_token[:60]}...")

        print(f"\n[4] 构造完整 sentinel token...")
        token = {
            "p": p_token,
            "c": chat_req.get("token", ""),
            "id": device_id,
            "flow": "authorize_continue",
        }
        print(f"    ✓ token: {json.dumps(token)[:100]}...")

    await provider.close()
    print("\n=== 测试完成 ===")


if __name__ == "__main__":
    import sys
    with open("sentinel_test_output.txt", "w", encoding="utf-8") as f:
        sys.stdout = f
        asyncio.run(test())
    sys.stdout = sys.__stdout__
    print("Done, see sentinel_test_output.txt")
