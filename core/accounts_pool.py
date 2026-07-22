"""
统一邮箱号池：outlook（4 段 IMAP）+ icloud（2 段 relay URL）共存。

文件来源：
  outlook_accounts.txt   每行 email----password----client_id----refresh_token   → kind="outlook"
  icloud_accounts.txt    每行 email----relay_url                                → kind="icloud"

公开 API:
    Account                        — 标准化的账号对象
    load_pool(kind=None)           — 加载全部 / 指定类型的池
    pick_account(...)              — 跟旧 _load_outlook 兼容的辅助
    to_outlook_creds(account)      — 适配 pipeline.login(outlook_creds=...)
    save_account_to_file(account)  — 写文件（webui 增删用）

跟 pipeline / phone_binding / codex_oauth 的对接点：
  - pipeline.login(outlook_creds=...) 字段：email / password / client_id / refresh_token
    icloud 账号 refresh_token 字段填 relay_url，email_provider.fetch_otp 自动识别
    URL 形态走 fetch_otp_relay。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

log = logging.getLogger("accounts_pool")

ROOT = Path(__file__).parent
OUTLOOK_FILE = ROOT / "outlook_accounts.txt"
ICLOUD_FILE = ROOT / "icloud_accounts.txt"


@dataclass
class Account:
    email: str
    kind: str                       # "outlook" | "icloud"
    # outlook 字段（icloud 时这些字段空）
    password: str = ""
    client_id: str = ""
    refresh_token: str = ""
    # icloud 字段（outlook 时空）
    relay_url: str = ""
    # 元数据
    raw_line: str = ""              # 原始文件行（用于精确删除 / 比对）
    source_file: str = ""           # 源文件名
    id: int = 0                      # 加密账号仓储主键

    @property
    def has_otp_creds(self) -> bool:
        if self.kind == "outlook":
            return bool(self.refresh_token and self.email)
        if self.kind == "icloud":
            return bool(self.relay_url and self.email)
        return False

    def to_outlook_creds(self) -> dict:
        """适配 pipeline.login(outlook_creds=...)：
        outlook → 原样；icloud → 把 relay_url 放进 refresh_token 字段，让
        email_provider.fetch_otp 自动识别 URL 走 relay 路径。
        """
        if self.kind == "icloud":
            return {
                "email": self.email,
                "password": "",
                "client_id": "",
                "refresh_token": self.relay_url,    # ← URL，会走 fetch_otp_relay
            }
        return {
            "email": self.email,
            "password": self.password,
            "client_id": self.client_id,
            "refresh_token": self.refresh_token,
        }


# ============================================================
#  解析
# ============================================================


def _parse_outlook_line(line: str) -> Account | None:
    """email----password----client_id----refresh_token"""
    parts = line.split("----")
    if len(parts) < 4:
        return None
    em = parts[0].strip()
    if not em:
        return None
    return Account(
        email=em,
        kind="outlook",
        password=parts[1].strip(),
        client_id=parts[2].strip(),
        refresh_token=parts[3].strip(),
        raw_line=line,
    )


def _parse_icloud_line(line: str) -> Account | None:
    """email----relay_url"""
    parts = line.split("----", 1)
    if len(parts) < 2:
        return None
    em = parts[0].strip()
    relay = parts[1].strip()
    if not em or not relay:
        return None
    if not relay.startswith(("http://", "https://")):
        # 不像 URL → 跳过
        return None
    return Account(
        email=em,
        kind="icloud",
        relay_url=relay,
        raw_line=line,
    )


def _load_file(path: Path, kind: str) -> list[Account]:
    if not path.exists():
        return []
    out: list[Account] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if kind == "outlook":
            a = _parse_outlook_line(s)
        elif kind == "icloud":
            a = _parse_icloud_line(s)
        else:
            a = None
        if a is None:
            continue
        a.source_file = path.name
        out.append(a)
    return out


def load_pool(kind: str | None = None) -> list[Account]:
    """加载邮箱池。加密 vault 为主真源，返回值保持历史接口不变。"""
    from account_vault import get_account_vault

    selected_kind = kind if kind in {"outlook", "icloud"} else None
    records = get_account_vault().list_accounts(selected_kind)
    return [
        Account(
            email=record.email,
            kind=record.kind,
            password=record.password,
            client_id=record.client_id,
            refresh_token=record.refresh_token,
            relay_url=record.relay_url,
            raw_line=record.raw_line,
            source_file=record.source_file,
            id=record.id,
        )
        for record in records
    ]


def find_by_email(email: str) -> Account | None:
    if not email:
        return None
    target = email.strip().lower()
    for a in load_pool():
        if a.email.lower() == target:
            return a
    return None


def stats() -> dict:
    """供 webui 仪表盘 / 账号 tab 顶部统计用。"""
    out = load_pool("outlook")
    ic = load_pool("icloud")
    return {
        "outlook": len(out),
        "icloud": len(ic),
        "total": len(out) + len(ic),
    }


# ============================================================
#  写入（webui 增删）
# ============================================================


def append_outlook_line(line: str) -> bool:
    from account_vault import ImportValidationError, get_account_vault

    try:
        result = get_account_vault().import_text(line, forced_kind="outlook")
    except (ImportValidationError, ValueError):
        return False
    return bool(result["added"])


def append_icloud_line(line: str) -> bool:
    from account_vault import ImportValidationError, get_account_vault

    try:
        result = get_account_vault().import_text(line, forced_kind="icloud")
    except (ImportValidationError, ValueError):
        return False
    return bool(result["added"])


def delete_account(email: str) -> bool:
    """从 vault 删除账号，并原子刷新历史兼容文件。"""
    from account_vault import AccountNotFound, get_account_vault

    target = (email or "").strip()
    if not target:
        return False
    try:
        get_account_vault().delete(target)
    except AccountNotFound:
        return False
    return True
