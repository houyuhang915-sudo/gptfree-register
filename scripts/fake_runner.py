#!/usr/bin/env python3
"""Deterministic local runner used by UI tests and screenshots."""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--accounts-file", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--method", default="protocol")
    parser.add_argument("--workers", default="3")
    args = parser.parse_args()
    rows = [
        line.strip() for line in Path(args.accounts_file).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    for index, row in enumerate(rows, 1):
        email = row.split("----", 1)[0]
        print(f"[#{index}/{len(rows)}] auth_csrf_pending · {email}", flush=True)
        time.sleep(0.12)
        print(f"[#{index}/{len(rows)}] otp_received · create_account_pending", flush=True)
        time.sleep(0.12)
        item = {
            "email": email,
            "ok": True,
            "status": "agent_ready",
            "method": args.method,
            "protocol_engine": "mail_auth" if args.method == "protocol" else "",
            "proxy_region": "JP",
            "duration_ms": 1320 + index * 237,
            "registered_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "plan_type": "free",
            "registration_ok": True,
            "agent_identity_ok": True,
            "free_trial": {"status": "eligible" if index % 2 else "not_eligible", "eligible": index % 2 == 1},
        }
        with out.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        print(f"[#{index}/{len(rows)}] ✓ Agent Identity 就绪", flush=True)
    print(f"完成 | ok={len(rows)} fail=0", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
