"""
账号导出 — 把 success.txt + phone_bindings.json 里的 ChatGPT Plus 账号
导成多种网关 / CLI 兼容格式，跟 _aBaiAutoplus-ref/application/account_exports.py
对齐（去掉了 Kiro / Grok 等本项目用不上的平台）。

支持格式（key → 用途）:

| key          | 文件                              | 用途 |
|---           |---                                |--- |
| json         | accounts_<ts>.json                | 通用备份 |
| csv          | accounts_<ts>.csv                 | Excel / 表格软件 |
| cpa          | cpa_tokens_<ts>.zip / <email>.json| CPA 网关「批量导入账号」 |
| sub2api      | sub2api_<ts>.json                 | sub2api 网关 admin 配置 |
| cockpit      | cockpit_tokens_<ts>.json          | Cockpit Codex 池 |
| any2api      | any2api_admin_<ts>.json           | Any2API 网关 admin.json |
| codex_rt     | openai_refresh_tokens_<ts>.txt    | sub2api「OpenAI RT 手动输入」一行一个 RT |
| codex_rt_em  | openai_refresh_tokens_with_email_<ts>.txt | email----rt 回查 |
| at_5parts    | at_export_5parts_<ts>.txt         | email|password|access_token|client_id|refresh_token |
| at_5dashes   | at_export_5parts_dashes_<ts>.txt  | email----password----client_id----refresh_token----access_token |
| at_6dashes   | at_export_6parts_dashes_<ts>.txt  | 上一个加上 ----phone（绑过号才有） |
| email_phone  | email_phone_<ts>.txt              | 一行一个 email----phone（绑号成功列表） |
| phone_only   | phones_only_<ts>.txt              | 一行一个 phone（去重） |

按 selection 过滤：
  - all                 全部账号
  - bound_only          只导绑过手机号的账号（默认推荐）
  - unbound_only        只导没绑过手机号的账号
  - emails              指定邮箱列表

导出源：
  - access_token / 注册三件套 → output/success.txt（output_writer.write_success 写的）
  - phone / sms_api / refresh_token → output/phone_bindings.json（phone_binding 写的）
  - 兜底 4 段（password/client_id/outlook refresh）→ output/outlook_accounts_success.txt
"""
from __future__ import annotations

import base64
import csv
import io
import json
import logging
import re
import time
import zipfile
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

log = logging.getLogger("account_exports")


ROOT = Path(__file__).parent
OUTPUT_DIR = ROOT / "output"
SUCCESS_FILE = OUTPUT_DIR / "success.txt"
OUTLOOK_SUCCESS_FILE = OUTPUT_DIR / "outlook_accounts_success.txt"
PHONE_BIND_STORE_FILE = OUTPUT_DIR / "phone_bindings.json"
AGENT_IDENTITY_STORE_FILE = OUTPUT_DIR / "agent_identities.json"
EXPORTS_DIR = OUTPUT_DIR / "exports"

# OpenAI 在 Stripe 上常用的 client_id；导出 CPA / Sub2API 时如果 JWT 里没带就用这个兜底
DEFAULT_CHATGPT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"


# ============================================================
#  数据模型
# ============================================================

@dataclass
class ExportRecord:
    """导出用的统一账号视图（用 access_token JWT 做主数据，phone_bindings 补充）。"""
    email: str
    password: str = ""
    access_token: str = ""
    session_token: str = ""           # chatgpt.com __Secure-next-auth.session-token
    refresh_token: str = ""           # OpenAI Codex refresh_token（绑号成功后或单独跑 Codex OAuth 拿到）
    id_token: str = ""
    client_id: str = ""
    chatgpt_account_id: str = ""
    chatgpt_user_id: str = ""
    workspace_id: str = ""
    plan_type: str = ""               # 'plus' / 'free'
    free_trial_status: str = ""        # eligible / not_eligible / unknown
    free_trial_eligible: bool | None = None
    free_trial_campaign: str = ""
    free_trial_checked_at: str = ""
    free_trial_error: str = ""
    expires_at_iso: str = ""
    expires_at_unix: int = 0
    last_refresh_iso: str = ""
    # phone bind 部分
    phone: str = ""
    sms_api: str = ""
    bound_at: str = ""
    # 兜底来源 (outlook_accounts_success.txt)
    outlook_client_id: str = ""
    outlook_refresh_token: str = ""
    # Agent Identity（Codex auth.json）
    agent_runtime_id: str = ""
    agent_private_key: str = ""
    agent_account_id: str = ""
    agent_user_id: str = ""

    # ---- 派生属性 ----
    @property
    def is_bound(self) -> bool:
        return bool(self.phone)

    @property
    def email_key(self) -> str:
        return self.email.replace("@", "_").replace(".", "_")

    @property
    def has_agent_identity(self) -> bool:
        return bool(self.agent_runtime_id and self.agent_private_key)


@dataclass
class ExportArtifact:
    filename: str
    media_type: str
    content: str | bytes


@dataclass
class ExportSelection:
    mode: str = "all"                                # all / bound_only / unbound_only / emails
    emails: list[str] = field(default_factory=list)
    plan_filter: str = ""                            # "plus" / "free" / "" (任意)


# ============================================================
#  JWT helpers
# ============================================================

def _decode_jwt_payload(token: str) -> dict:
    if not token or token.count(".") < 2:
        return {}
    payload = token.split(".")[1]
    pad = "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload + pad).decode("utf-8"))
    except Exception:
        return {}


def _jwt_chatgpt_auth(access_token: str, id_token: str = "") -> dict:
    out: dict = {}
    for tok in (access_token, id_token):
        payload = _decode_jwt_payload(tok)
        auth = payload.get("https://api.openai.com/auth", {})
        if isinstance(auth, dict):
            for k, v in auth.items():
                if v not in (None, "", [], {}):
                    out[k] = v
    return out


def _now_utc_iso() -> str:
    n = datetime.now(timezone.utc)
    return n.strftime("%Y-%m-%dT%H:%M:%S.") + f"{n.microsecond // 1000:03d}Z"


def _ts(ts: int) -> str:
    """ISO 8601 with milliseconds + Z (匹配 sub2api / cockpit 期望的 expires_at 格式)。"""
    if not ts:
        return ""
    n = datetime.fromtimestamp(ts, tz=timezone.utc)
    return n.strftime("%Y-%m-%dT%H:%M:%S.") + f"{n.microsecond // 1000:03d}Z"


def _timestamp_name(prefix: str, suffix: str) -> str:
    return f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{suffix}"


# ============================================================
#  数据源解析
# ============================================================

# success.txt 是「等号块 + 中文键值对」的格式（output_writer.write_success），
# 每个块用 ===== 分隔。我们抽出 email/password/access_token/refresh_token/client_id。

_BLOCK_SPLIT = re.compile(r"={20,}")


def _parse_success_blocks(text: str) -> list[dict]:
    blocks = _BLOCK_SPLIT.split(text or "")
    rows: list[dict] = []
    for b in blocks:
        em = re.search(r"邮箱[:：]\s*(\S+@\S+)", b)
        if not em:
            continue
        pw = re.search(r"密码[:：]\s*(\S+)", b)
        # access_token 后面一行（可能跨多行被换行）→ 截到下个键名 / 块尾
        at_match = re.search(
            r"access_token[^\n]*\n([\s\S]*?)(?=refresh_token|session_token|client_id:|openai_refresh_token|={5}|$)",
            b,
        )
        access_token = ""
        if at_match:
            access_token = re.sub(r"\s+", "", at_match.group(1))
            if access_token.count(".") < 2 or len(access_token) < 100:
                access_token = ""

        # chatgpt session cookie（free 协议注册会写 session_token: xxx）
        session_token = ""
        m_st = re.search(r"session_token[:：]\s*(\S+)", b)
        if m_st:
            session_token = m_st.group(1).strip()

        # outlook 4 段（refresh_token / client_id 行）
        client_id = ""
        outlook_rt = ""
        m_cid = re.search(r"client_id[:：]\s*(\S+)", b)
        if m_cid:
            client_id = m_cid.group(1).strip()
        m_rt = re.search(r"refresh_token[:：]\s*(\S+)", b)
        if m_rt:
            outlook_rt = m_rt.group(1).strip()

        trial_status = ""
        trial_eligible: bool | None = None
        trial_label = re.search(r"免费试用[:：]\s*(有|无|未知)", b)
        if trial_label:
            trial_eligible = (
                True if trial_label.group(1) == "有"
                else (False if trial_label.group(1) == "无" else None)
            )
        m_trial_status = re.search(r"试用状态[:：]\s*(\S+)", b)
        if m_trial_status:
            trial_status = m_trial_status.group(1).strip().lower()
        m_trial_campaign = re.search(r"试用活动[:：]\s*(\S+)", b)
        m_trial_checked = re.search(r"试用检测时间[:：]\s*(\S+)", b)
        m_trial_error = re.search(r"试用检测错误[:：]\s*(.+)", b)

        rows.append({
            "email": em.group(1).strip(),
            "password": pw.group(1).strip() if pw else "",
            "access_token": access_token,
            "session_token": session_token,
            "client_id": client_id,
            "outlook_refresh_token": outlook_rt,
            "free_trial_status": trial_status,
            "free_trial_eligible": trial_eligible,
            "free_trial_campaign": (
                m_trial_campaign.group(1).strip() if m_trial_campaign else ""
            ),
            "free_trial_checked_at": (
                m_trial_checked.group(1).strip() if m_trial_checked else ""
            ),
            "free_trial_error": (
                m_trial_error.group(1).strip() if m_trial_error else ""
            ),
        })
    return rows


def _load_outlook_success_lookup() -> dict[str, dict]:
    """outlook_accounts_success.txt 里 email → {password, client_id, refresh_token, phone}"""
    out: dict[str, dict] = {}
    if not OUTLOOK_SUCCESS_FILE.exists():
        return out
    for ln in OUTLOOK_SUCCESS_FILE.read_text(encoding="utf-8").splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split("----")
        if len(parts) < 2:
            continue
        email = parts[0].strip().lower()
        if not email:
            continue
        out[email] = {
            "password": parts[1].strip() if len(parts) > 1 else "",
            "client_id": parts[2].strip() if len(parts) > 2 else "",
            "refresh_token": parts[3].strip() if len(parts) > 3 else "",
            "phone": parts[4].strip() if len(parts) > 4 else "",
        }
    return out


def _load_phone_bindings() -> dict[str, dict]:
    if not PHONE_BIND_STORE_FILE.exists():
        return {}
    try:
        raw = json.loads(PHONE_BIND_STORE_FILE.read_text(encoding="utf-8"))
        return {k.lower(): v for k, v in raw.items() if isinstance(v, dict)}
    except Exception:
        return {}


def _load_agent_identities() -> dict[str, dict]:
    if not AGENT_IDENTITY_STORE_FILE.exists():
        return {}
    try:
        raw = json.loads(AGENT_IDENTITY_STORE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(key).strip().lower(): value
        for key, value in raw.items()
        if str(key).strip() and isinstance(value, dict)
    }


def _load_k12_export_records() -> dict[str, dict]:
    records: dict[str, dict] = {}
    if not EXPORTS_DIR.exists():
        return records

    def merge(email: str, entry: dict) -> None:
        key = (email or "").strip().lower()
        if not key:
            return
        current = records.setdefault(key, {"email": email.strip()})
        for field in (
            "access_token", "refresh_token", "id_token", "client_id",
            "chatgpt_account_id", "chatgpt_user_id", "workspace_id",
            "plan_type", "source_file",
        ):
            value = entry.get(field)
            if value and not current.get(field):
                current[field] = value

    def add_from_credentials(creds: dict, extra: dict, path: Path) -> None:
        plan = str(creds.get("plan_type") or extra.get("plan_type") or "").strip().lower()
        if plan != "k12":
            return
        email = creds.get("email") or extra.get("email") or ""
        access_token = creds.get("access_token") or creds.get("token") or ""
        auth = _jwt_chatgpt_auth(access_token)
        merge(email, {
            "access_token": access_token,
            "refresh_token": creds.get("refresh_token") or "",
            "id_token": creds.get("id_token") or "",
            "client_id": creds.get("client_id") or DEFAULT_CHATGPT_CLIENT_ID,
            "chatgpt_account_id": creds.get("account_id") or extra.get("account_id") or auth.get("chatgpt_account_id") or "",
            "chatgpt_user_id": creds.get("user_id") or extra.get("user_id") or auth.get("chatgpt_user_id") or auth.get("user_id") or "",
            "workspace_id": creds.get("workspace_id") or extra.get("workspace_id") or auth.get("organization_id") or "",
            "plan_type": "k12",
            "source_file": str(path),
        })

    for path in sorted(EXPORTS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        if not (path.name.startswith("sub2api_") or path.name.startswith("cpa_tokens_") or "k12" in path.name.lower()):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        items = data.get("accounts")
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                creds = item.get("credentials") if isinstance(item.get("credentials"), dict) else item
                extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
                add_from_credentials(creds, extra, path)
            continue
        creds = data.get("credentials") if isinstance(data.get("credentials"), dict) else data
        add_from_credentials(creds, data, path)
    return records


def load_records() -> list[ExportRecord]:
    """合并 success.txt + outlook_accounts_success.txt + phone_bindings.json。

    去重：以 email（小写）为 key；同 email 取最新（success.txt 后写的那块）。
    """
    rows_by_email: dict[str, dict] = {}
    if SUCCESS_FILE.exists():
        for r in _parse_success_blocks(SUCCESS_FILE.read_text(encoding="utf-8")):
            rows_by_email[r["email"].lower()] = r

    outlook = _load_outlook_success_lookup()
    bindings = _load_phone_bindings()
    agents = _load_agent_identities()
    k12_exports = _load_k12_export_records()

    # 把 outlook + bindings + K12 导出里有但 success.txt 没的 email 也加进来
    for email_key in set(outlook.keys()) | set(bindings.keys()) | set(agents.keys()) | set(k12_exports.keys()):
        if email_key in rows_by_email:
            continue
        rows_by_email[email_key] = {
            "email": email_key, "password": "", "access_token": "",
            "session_token": "", "client_id": "", "outlook_refresh_token": "",
        }

    records: list[ExportRecord] = []
    for email_key, raw in rows_by_email.items():
        ol = outlook.get(email_key, {})
        bind = bindings.get(email_key, {})
        agent = agents.get(email_key, {})
        k12_export = k12_exports.get(email_key, {})
        bind_plan = str(bind.get("plan_type") or k12_export.get("plan_type") or "").strip().lower()
        bind_is_k12 = bind_plan == "k12" or bool(bind.get("k12_updated_at")) or bool(bind.get("k12_export_file")) or bool(k12_export)
        if bind_is_k12:
            access_token = bind.get("access_token") or k12_export.get("access_token") or ""
        else:
            access_token = raw.get("access_token") or bind.get("access_token") or ""
        session_token = (
            raw.get("session_token")
            or bind.get("session_token")
            or bind.get("sessionToken")
            or ""
        )
        jwt_payload = _decode_jwt_payload(access_token) if access_token else {}
        auth = _jwt_chatgpt_auth(access_token, bind.get("id_token", ""))
        exp_unix = int(jwt_payload.get("exp", 0) or 0)
        iat_unix = int(jwt_payload.get("iat", 0) or 0)
        trial_source = bind if bind.get("free_trial_status") else raw

        record = ExportRecord(
            email=raw.get("email") or bind.get("email") or email_key,
            password=raw.get("password") or ol.get("password", ""),
            access_token=access_token,
            session_token=session_token,
            refresh_token=bind.get("refresh_token") or "",
            id_token=bind.get("id_token") or "",
            client_id=k12_export.get("client_id") or raw.get("client_id") or jwt_payload.get("client_id", "") or DEFAULT_CHATGPT_CLIENT_ID,
            chatgpt_account_id=str((bind.get("chatgpt_account_id") if bind_is_k12 else "") or k12_export.get("chatgpt_account_id") or bind.get("chatgpt_account_id") or auth.get("chatgpt_account_id", "")),
            chatgpt_user_id=str((bind.get("chatgpt_user_id") if bind_is_k12 else "") or k12_export.get("chatgpt_user_id") or bind.get("chatgpt_user_id") or auth.get("chatgpt_user_id", "") or auth.get("user_id", "")),
            workspace_id=str((bind.get("workspace_id") if bind_is_k12 else "") or k12_export.get("workspace_id") or bind.get("workspace_id") or auth.get("organization_id", "")),
            plan_type=str(("k12" if bind_is_k12 else "") or bind.get("plan_type") or k12_export.get("plan_type") or auth.get("chatgpt_plan_type", "") or "free"),
            free_trial_status=str(trial_source.get("free_trial_status") or ""),
            free_trial_eligible=trial_source.get("free_trial_eligible"),
            free_trial_campaign=str(trial_source.get("free_trial_campaign") or ""),
            free_trial_checked_at=str(trial_source.get("free_trial_checked_at") or ""),
            free_trial_error=str(trial_source.get("free_trial_error") or ""),
            expires_at_iso=_ts(exp_unix),
            expires_at_unix=exp_unix,
            last_refresh_iso=_ts(iat_unix),
            phone=bind.get("phone") or ol.get("phone", "") or "",
            sms_api=bind.get("sms_api") or "",
            bound_at=bind.get("bound_at") or "",
            outlook_client_id=ol.get("client_id") or raw.get("client_id") or "",
            outlook_refresh_token=ol.get("refresh_token") or raw.get("outlook_refresh_token") or "",
            agent_runtime_id=str(agent.get("agent_runtime_id") or ""),
            agent_private_key=str(agent.get("agent_private_key") or ""),
            agent_account_id=str(agent.get("account_id") or ""),
            agent_user_id=str(agent.get("user_id") or ""),
        )
        records.append(record)
    return records


def select_records(records: Iterable[ExportRecord],
                   selection: ExportSelection | None = None) -> list[ExportRecord]:
    sel = selection or ExportSelection()
    out = list(records)
    if sel.mode == "bound_only":
        out = [r for r in out if r.is_bound]
    elif sel.mode == "unbound_only":
        out = [r for r in out if not r.is_bound]
    elif sel.mode == "emails":
        wanted = {e.strip().lower() for e in (sel.emails or []) if e.strip()}
        out = [r for r in out if r.email.lower() in wanted]
    if sel.plan_filter:
        out = [r for r in out if (r.plan_type or "").lower() == sel.plan_filter.lower()]
    out.sort(key=lambda r: (not r.is_bound, r.email.lower()))
    return out


# ============================================================
#  导出器
# ============================================================

def _record_to_flat_dict(r: ExportRecord) -> dict:
    d = asdict(r)
    d["is_bound"] = r.is_bound
    return d


def export_json(records: list[ExportRecord]) -> ExportArtifact:
    payload = {
        "exported_at": _now_utc_iso(),
        "count": len(records),
        "accounts": [_record_to_flat_dict(r) for r in records],
    }
    return ExportArtifact(
        filename=_timestamp_name("accounts", "json"),
        media_type="application/json",
        content=json.dumps(payload, ensure_ascii=False, indent=2),
    )


def export_csv(records: list[ExportRecord]) -> ExportArtifact:
    cols = [
        "email", "password", "phone", "plan_type",
        "is_bound", "bound_at",
        "access_token", "refresh_token", "id_token",
        "client_id", "chatgpt_account_id", "workspace_id",
        "expires_at_iso", "expires_at_unix",
        "outlook_client_id", "outlook_refresh_token", "sms_api",
    ]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in records:
        w.writerow(_record_to_flat_dict(r))
    return ExportArtifact(
        filename=_timestamp_name("accounts", "csv"),
        media_type="text/csv",
        content=buf.getvalue(),
    )


# ----- CPA token JSON -----
def _cpa_token_json(r: ExportRecord) -> dict:
    """CPA 网关「批量导入账号」格式（每个账号一份 <email>.json）。

    格式跟 _aBaiAutoplus-ref/platforms/chatgpt/cpa_upload.generate_token_json 同：
    平铺 access_token / refresh_token / 用户标识 / 过期时间。
    """
    return {
        "email": r.email,
        "password": r.password,
        "access_token": r.access_token,
        "refresh_token": r.refresh_token,
        "id_token": r.id_token,
        "session_token": "",
        "user_id": r.chatgpt_user_id,
        "account_id": r.chatgpt_account_id,
        "workspace_id": r.workspace_id,
        "client_id": r.client_id,
        "plan_type": r.plan_type,
        "expired": r.expires_at_iso,
        "last_refresh": r.last_refresh_iso,
        "phone": r.phone,
        "phone_bound_at": r.bound_at,
    }


def export_cpa(records: list[ExportRecord]) -> ExportArtifact:
    if len(records) == 1:
        r = records[0]
        return ExportArtifact(
            filename=f"{r.email}.json",
            media_type="application/json",
            content=json.dumps(_cpa_token_json(r), ensure_ascii=False, indent=2),
        )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for r in records:
            zf.writestr(f"{r.email}.json",
                        json.dumps(_cpa_token_json(r), ensure_ascii=False, indent=2))
    buf.seek(0)
    return ExportArtifact(
        filename=_timestamp_name("cpa_tokens", "zip"),
        media_type="application/zip",
        content=buf.read(),
    )


# ----- sub2api admin.json -----
def _sub2api_account(r: ExportRecord) -> dict:
    """Sub2API 账号格式 — 严格按下游期望的 shape：

        {
          "name": email, "platform": "openai", "type": "oauth",
          "concurrency": 10, "priority": 1,
          "credentials": {
            access_token, refresh_token, id_token,
            chatgpt_account_id, chatgpt_user_id, email,
            expires_at (ISO 8601), expires_in, plan_type
          },
          "extra": { email, email_key, name, source, last_refresh }
        }
    """
    expires_in = max(0, r.expires_at_unix - int(time.time())) if r.expires_at_unix else 0
    last_refresh = r.last_refresh_iso or _now_utc_iso()
    return {
        "name": r.email,
        "platform": "openai",
        "type": "oauth",
        "concurrency": 10,
        "priority": 1,
        "credentials": {
            "access_token": r.access_token,
            "refresh_token": r.refresh_token,
            "id_token": r.id_token,
            "chatgpt_account_id": r.chatgpt_account_id,
            "chatgpt_user_id": r.chatgpt_user_id,
            "email": r.email,
            "expires_at": r.expires_at_iso,
            "expires_in": expires_in,
            "plan_type": r.plan_type,
        },
        "extra": {
            "email": r.email,
            "email_key": r.email.lower().replace("@", "_").replace(".", "_"),
            "name": r.email,
            "source": "chatgpt_web_session",
            "last_refresh": last_refresh,
        },
    }


def export_sub2api(records: list[ExportRecord]) -> ExportArtifact:
    payload = {
        "exported_at": _now_utc_iso(),
        "proxies": [],
        "accounts": [_sub2api_account(r) for r in records],
    }
    return ExportArtifact(
        filename=_timestamp_name("sub2api", "json"),
        media_type="application/json",
        content=json.dumps(payload, ensure_ascii=False, indent=2),
    )


# ----- Cockpit -----
def _cockpit_token(r: ExportRecord) -> dict:
    return {
        "type": "codex",
        "id_token": r.id_token,
        "access_token": r.access_token,
        "refresh_token": r.refresh_token,
        "account_id": r.chatgpt_account_id,
        "last_refresh": r.last_refresh_iso,
        "email": r.email,
        "expired": r.expires_at_iso,
        "phone": r.phone,
        "account_note": "",
    }


def export_cockpit(records: list[ExportRecord]) -> ExportArtifact:
    payload: dict | list = (
        _cockpit_token(records[0]) if len(records) == 1
        else [_cockpit_token(r) for r in records]
    )
    return ExportArtifact(
        filename=_timestamp_name("cockpit_tokens", "json"),
        media_type="application/json",
        content=json.dumps(payload, ensure_ascii=False, indent=2),
    )


# ----- Any2API admin.json -----
def export_any2api(records: list[ExportRecord]) -> ExportArtifact:
    """Any2API 多平台 admin.json；本项目只产 chatgptAccounts 段。"""
    chatgpt_accounts = [_sub2api_account(r) for r in records]
    payload = {
        "exported_at": _now_utc_iso(),
        "chatgptAccounts": chatgpt_accounts,
    }
    return ExportArtifact(
        filename=_timestamp_name("any2api_admin", "json"),
        media_type="application/json",
        content=json.dumps(payload, ensure_ascii=False, indent=2),
    )


# ----- Codex refresh_token 行式 -----
def export_codex_rt(records: list[ExportRecord]) -> ExportArtifact:
    rts = [r.refresh_token for r in records if r.refresh_token]
    return ExportArtifact(
        filename=_timestamp_name("openai_refresh_tokens", "txt"),
        media_type="text/plain",
        content="\n".join(rts) + ("\n" if rts else ""),
    )


def export_codex_rt_with_email(records: list[ExportRecord]) -> ExportArtifact:
    lines = [f"{r.email}----{r.refresh_token}" for r in records if r.refresh_token]
    return ExportArtifact(
        filename=_timestamp_name("openai_refresh_tokens_with_email", "txt"),
        media_type="text/plain",
        content="\n".join(lines) + ("\n" if lines else ""),
    )


# ----- 5/6 段平铺格式 -----
def export_at_5parts(records: list[ExportRecord]) -> ExportArtifact:
    """email|password|access_token|client_id|outlook_refresh_token"""
    lines = []
    for r in records:
        if not r.access_token:
            continue
        lines.append("|".join([
            r.email, r.password, r.access_token,
            r.outlook_client_id or r.client_id, r.outlook_refresh_token,
        ]))
    return ExportArtifact(
        filename=_timestamp_name("at_export_5parts", "txt"),
        media_type="text/plain",
        content="\n".join(lines) + ("\n" if lines else ""),
    )


def export_at_5dashes(records: list[ExportRecord]) -> ExportArtifact:
    """email----password----client_id----outlook_refresh_token----access_token"""
    lines = []
    for r in records:
        if not r.access_token:
            continue
        lines.append("----".join([
            r.email, r.password,
            r.outlook_client_id or r.client_id, r.outlook_refresh_token,
            r.access_token,
        ]))
    return ExportArtifact(
        filename=_timestamp_name("at_export_5parts_dashes", "txt"),
        media_type="text/plain",
        content="\n".join(lines) + ("\n" if lines else ""),
    )


def export_at_4dashes(records: list[ExportRecord]) -> ExportArtifact:
    """email----password----client_id----refresh_token

    原版 plus 4 段格式（和 outlook_accounts_success.txt 完全一致）：
    没有 access_token 字段，纯邮箱-密码-客户端ID-refresh_token，
    适合直接喂给只需重登邮箱的下游脚本。

    密码：优先 outlook 真密码（重登邮箱用），缺失时回退 ChatGPT 注册密码。
    refresh_token：必须存在（无 RT 的账号会被 needs_rt 过滤掉）。
    """
    lines = []
    for r in records:
        if not r.outlook_refresh_token:
            continue
        # 优先 outlook 真密码（这是重登邮箱用的密码），fallback 到注册密码
        pwd = getattr(r, "outlook_password", None) or r.password
        client_id = r.outlook_client_id or r.client_id
        lines.append("----".join([
            r.email, pwd, client_id, r.outlook_refresh_token,
        ]))
    header = (
        "# 已成功开通 Plus 的账号（原始 4 段格式：email----password----client_id----refresh_token）\n"
        f"# 共 {len(lines)} 行（密码=outlook 真密码，refresh_token=outlook 邮箱重登 RT）\n"
    )
    return ExportArtifact(
        filename=_timestamp_name("plus_accounts_4parts_dashes", "txt"),
        media_type="text/plain",
        content=header + "\n".join(lines) + ("\n" if lines else ""),
    )


def export_at_6dashes(records: list[ExportRecord]) -> ExportArtifact:
    """email----password----client_id----outlook_refresh_token----access_token----phone

    只导带 phone 的账号（没绑过的不出现，避免 6 段位 phone 字段是空字符串）。
    """
    lines = []
    for r in records:
        if not r.access_token or not r.phone:
            continue
        lines.append("----".join([
            r.email, r.password,
            r.outlook_client_id or r.client_id, r.outlook_refresh_token,
            r.access_token, r.phone,
        ]))
    return ExportArtifact(
        filename=_timestamp_name("at_export_6parts_dashes", "txt"),
        media_type="text/plain",
        content="\n".join(lines) + ("\n" if lines else ""),
    )


def export_email_phone(records: list[ExportRecord]) -> ExportArtifact:
    lines = [f"{r.email}----{r.phone}" for r in records if r.phone]
    return ExportArtifact(
        filename=_timestamp_name("email_phone", "txt"),
        media_type="text/plain",
        content="\n".join(lines) + ("\n" if lines else ""),
    )


def export_phone_only(records: list[ExportRecord]) -> ExportArtifact:
    seen = []
    for r in records:
        if r.phone and r.phone not in seen:
            seen.append(r.phone)
    return ExportArtifact(
        filename=_timestamp_name("phones_only", "txt"),
        media_type="text/plain",
        content="\n".join(seen) + ("\n" if seen else ""),
    )


# ----- Codex Agent Identity auth.json -----
def _agent_auth_json(r: ExportRecord) -> dict:
    from agent_identity import build_auth_json

    return build_auth_json(
        agent_runtime_id=r.agent_runtime_id,
        private_key_b64=r.agent_private_key,
        account_id=r.agent_account_id or r.chatgpt_account_id,
        user_id=r.agent_user_id or r.chatgpt_user_id,
        email=r.email,
        plan_type=r.plan_type or "free",
    )


def export_codex_auth(records: list[ExportRecord]) -> ExportArtifact:
    if len(records) == 1:
        return ExportArtifact(
            filename="auth.json",
            media_type="application/json",
            content=json.dumps(_agent_auth_json(records[0]), ensure_ascii=False, indent=2),
        )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for record in records:
            archive.writestr(
                f"{record.email}/auth.json",
                json.dumps(_agent_auth_json(record), ensure_ascii=False, indent=2),
            )
    return ExportArtifact(
        filename=_timestamp_name("codex_agent_auth", "zip"),
        media_type="application/zip",
        content=buf.getvalue(),
    )


def _sub2api_agent_data_account(record: ExportRecord) -> dict:
    """生成 Sub2API 管理后台数据导入文件中的 accounts[] 条目。"""
    identity = _agent_auth_json(record)["agent_identity"]
    credentials = {
        "auth_mode": "agent_identity",
        "agent_runtime_id": identity.get("agent_runtime_id") or "",
        "agent_private_key": identity.get("agent_private_key") or "",
        "chatgpt_account_id": identity.get("account_id") or "",
        "chatgpt_user_id": identity.get("chatgpt_user_id") or "",
        "email": identity.get("email") or record.email,
        "plan_type": identity.get("plan_type") or record.plan_type or "free",
        "chatgpt_account_is_fedramp": bool(
            identity.get("chatgpt_account_is_fedramp", False)
        ),
    }
    return {
        "name": record.email or "codex-agent",
        "platform": "openai",
        "type": "oauth",
        "credentials": credentials,
        "extra": {
            "email": record.email,
            "import_source": "codex_agent_identity",
        },
        "concurrency": 10,
        "priority": 1,
        "auto_pause_on_expired": False,
    }


def export_sub2api_agent(records: list[ExportRecord]) -> ExportArtifact:
    """导出管理后台「导入数据」弹窗可直接读取的 DataPayload。"""
    payload = {
        "type": "sub2api-data",
        "version": 1,
        "exported_at": _now_utc_iso(),
        "proxies": [],
        "accounts": [_sub2api_agent_data_account(record) for record in records],
    }
    return ExportArtifact(
        filename=_timestamp_name("sub2api-data-agent", "json"),
        media_type="application/json",
        content=json.dumps(payload, ensure_ascii=False, indent=2),
    )


# ============================================================
#  分发
# ============================================================

EXPORT_FORMATS: dict[str, dict] = {
    "json":         {"label": "JSON 完整备份 (.json)", "fn": export_json,
                     "needs_token": False, "needs_phone": False},
    "csv":          {"label": "CSV 表格 (.csv)", "fn": export_csv,
                     "needs_token": False, "needs_phone": False},
    "cpa":          {"label": "CPA 网关批量导入 (cpa_tokens.zip)", "fn": export_cpa,
                     "needs_token": True, "needs_phone": False},
    "sub2api":      {"label": "Sub2API admin.json", "fn": export_sub2api,
                     "needs_token": True, "needs_phone": False},
    "cockpit":      {"label": "Cockpit codex 池 (cockpit_tokens.json)", "fn": export_cockpit,
                     "needs_token": True, "needs_phone": False},
    "any2api":      {"label": "Any2API admin.json", "fn": export_any2api,
                     "needs_token": True, "needs_phone": False},
    "codex_rt":     {"label": "OpenAI refresh_token 一行一个 (.txt)", "fn": export_codex_rt,
                     "needs_token": False, "needs_phone": False, "needs_rt": True},
    "codex_rt_em":  {"label": "OpenAI rt+email (.txt)", "fn": export_codex_rt_with_email,
                     "needs_token": False, "needs_phone": False, "needs_rt": True},
    "at_5parts":    {"label": "5 段竖线 email|password|access_token|client_id|rt", "fn": export_at_5parts,
                     "needs_token": True, "needs_phone": False},
    "at_5dashes":   {"label": "5 段 ---- (兼容旧 export)", "fn": export_at_5dashes,
                     "needs_token": True, "needs_phone": False},
    "at_4dashes_plus": {"label": "4 段 ---- (原版 Plus 格式 email----password----client_id----rt)",
                     "fn": export_at_4dashes,
                     "needs_token": False, "needs_phone": False, "needs_rt": True},
    "at_6dashes":   {"label": "6 段 ---- 加 phone（只导绑过号的账号）", "fn": export_at_6dashes,
                     "needs_token": True, "needs_phone": True},
    "email_phone":  {"label": "email----phone (绑号成功列表)", "fn": export_email_phone,
                     "needs_token": False, "needs_phone": True},
    "phone_only":   {"label": "phone 一行一个 (去重)", "fn": export_phone_only,
                     "needs_token": False, "needs_phone": True},
    "codex_auth":   {"label": "Codex Agent Identity auth.json", "fn": export_codex_auth,
                     "needs_token": False, "needs_phone": False, "needs_agent": True},
    "sub2api_agent": {"label": "Sub2API Agent Identity 数据文件 (.json)",
                      "fn": export_sub2api_agent,
                      "needs_token": False, "needs_phone": False, "needs_agent": True},
}


def export(format_key: str, selection: ExportSelection | None = None,
           records: list[ExportRecord] | None = None,
           *, refresh_tokens: bool | None = None) -> ExportArtifact:
    """生成导出 artifact。

    refresh_tokens:
        True  → 导出前用 refresh_token 刷一次 access_token（保证下游不 401）
        False → 不刷，直接用本地存的
        None  → 默认行为：sub2api / cockpit / any2api / cpa / at_5parts / at_5dashes /
                 at_6dashes 这些"下游会拿 access_token 调 API"的格式自动刷；
                 其它格式（json/csv/codex_rt/email_phone/phone_only）不刷
    """
    if format_key not in EXPORT_FORMATS:
        raise ValueError(f"unknown export format: {format_key}")
    spec = EXPORT_FORMATS[format_key]
    rs = records if records is not None else load_records()
    rs = select_records(rs, selection)
    if spec.get("needs_phone"):
        rs = [r for r in rs if r.phone]
    if spec.get("needs_token"):
        rs = [r for r in rs if r.access_token]
    if spec.get("needs_rt"):
        rs = [r for r in rs if r.refresh_token]
    if spec.get("needs_agent"):
        rs = [r for r in rs if r.has_agent_identity]
    if not rs:
        raise ValueError(f"no records match selection for format={format_key}")

    # 默认：导 sub2api / cockpit / any2api / cpa / at_5parts / at_5dashes / at_6dashes
    # 之前自动用 refresh_token 刷 access_token，避免下游拿到陈旧 token 401。
    auto_refresh_formats = {
        "sub2api", "cockpit", "any2api", "cpa",
        "at_5parts", "at_5dashes", "at_6dashes",
    }
    do_refresh = (
        False
        if spec.get("needs_agent")
        else (refresh_tokens if refresh_tokens is not None else (format_key in auto_refresh_formats))
    )
    if do_refresh:
        rs = _refresh_records_in_place(rs)

    artifact: ExportArtifact = spec["fn"](rs)
    return artifact


def _refresh_records_in_place(records: list[ExportRecord]) -> list[ExportRecord]:
    """对每个有 refresh_token 的 record 调 codex_oauth.refresh_access_token，
    用新拿到的 access_token / id_token 替换 record 字段，并把新值持久化到
    phone_binding.binding_store（这样 webui 看到的也是最新的）。

    没 refresh_token 的 record 原样返回。
    """
    try:
        from codex_oauth import refresh_access_token
    except Exception as exc:
        log.warning(f"  [exports] codex_oauth 不可用，跳过 access_token 刷新: {exc}")
        return records

    try:
        from phone_binding import binding_store
    except Exception:
        binding_store = None   # type: ignore

    refreshed = 0
    for r in records:
        if (r.plan_type or "").strip().lower() == "k12":
            continue
        if not r.refresh_token:
            continue
        try:
            new_tokens = refresh_access_token(r.refresh_token)
        except Exception as exc:
            log.warning(f"  [exports] 刷新 {r.email} access_token 失败（沿用旧的）: {exc}")
            continue
        new_access = (new_tokens.get("access_token") or "").strip()
        if not new_access:
            log.warning(f"  [exports] {r.email} 刷新响应缺 access_token，沿用旧的: {str(new_tokens)[:200]}")
            continue
        # 替换 record 字段
        r.access_token = new_access
        new_id = (new_tokens.get("id_token") or "").strip()
        if new_id:
            r.id_token = new_id
        # refresh_token 一般不变，但 OAuth2 允许 server 轮换，新 RT 优先
        new_rt = (new_tokens.get("refresh_token") or "").strip()
        if new_rt and new_rt != r.refresh_token:
            r.refresh_token = new_rt
        # 重算过期时间
        expires_in = int(new_tokens.get("expires_in") or 0)
        if expires_in > 0:
            r.expires_at_unix = int(time.time()) + expires_in
            r.expires_at_iso = _ts(r.expires_at_unix)
        # 同步 last_refresh
        r.last_refresh_iso = _now_utc_iso()
        # 重算 chatgpt_account_id / plan_type 等（从新 id_token / access_token 解析）
        for tok in (r.access_token, r.id_token):
            payload = _decode_jwt_payload(tok)
            auth = payload.get("https://api.openai.com/auth", {})
            if isinstance(auth, dict):
                if auth.get("chatgpt_account_id") and not r.chatgpt_account_id:
                    r.chatgpt_account_id = str(auth["chatgpt_account_id"])
                if auth.get("chatgpt_user_id") and not r.chatgpt_user_id:
                    r.chatgpt_user_id = str(auth["chatgpt_user_id"])
                if auth.get("chatgpt_plan_type"):
                    r.plan_type = str(auth["chatgpt_plan_type"])
        # 持久化到 binding_store（如果该 email 已经在里面）
        if binding_store is not None:
            try:
                binding_store.record_binding(
                    email=r.email,
                    access_token=r.access_token,
                    refresh_token=r.refresh_token,
                    id_token=r.id_token,
                    client_id=r.outlook_client_id or r.client_id,
                    outlook_refresh_token=r.outlook_refresh_token,
                )
            except Exception as exc:
                log.debug(f"  [exports] 持久化刷新结果失败（不致命）: {exc}")
        refreshed += 1
    if refreshed:
        log.info(f"  [exports] 已刷新 {refreshed} 个账号的 access_token")
    return records


def write_artifact(artifact: ExportArtifact, dest_dir: Path | None = None) -> Path:
    """把 artifact 写到 dest_dir（默认 output/exports/）。"""
    target_dir = dest_dir or (OUTPUT_DIR / "exports")
    target_dir.mkdir(parents=True, exist_ok=True)
    p = target_dir / artifact.filename
    if isinstance(artifact.content, bytes):
        p.write_bytes(artifact.content)
    elif isinstance(artifact.content, io.IOBase):
        p.write_bytes(artifact.content.read())  # type: ignore
    else:
        p.write_text(artifact.content, encoding="utf-8")
    try:
        p.chmod(0o600)
    except OSError:
        pass
    return p


def stats() -> dict:
    """返回当前可导出账号的统计概览（给 webui 用）。"""
    rs = load_records()
    bound = [r for r in rs if r.is_bound]
    plus = [r for r in rs if r.plan_type == "plus"]
    bound_plus = [r for r in bound if r.plan_type == "plus"]
    has_token = [r for r in rs if r.access_token]
    has_rt = [r for r in rs if r.refresh_token]
    has_agent = [r for r in rs if r.has_agent_identity]
    return {
        "total": len(rs),
        "bound": len(bound),
        "plus": len(plus),
        "bound_plus": len(bound_plus),
        "with_access_token": len(has_token),
        "with_refresh_token": len(has_rt),
        "with_agent_identity": len(has_agent),
        "formats": {k: v["label"] for k, v in EXPORT_FORMATS.items()},
    }
