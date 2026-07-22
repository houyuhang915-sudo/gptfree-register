from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any

from .service import (
    EmailItem,
    FreeRunOptions,
    RunOptions,
    append_jsonl,
    generate_email_prefix,
    normalize_email_item,
    parse_email_items,
    run_one,
    run_one_free,
)
from .sms import HttpSmsConfig, HttpSmsProvider, SmsProvider, load_legacy_default


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _add_common_email_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--email", action="append", default=[], help="single email; can be used multiple times")
    p.add_argument("--emails", default="", help="newline/semicolon separated email list")
    p.add_argument("--emails-file", type=Path, help="file containing one email per line")
    p.add_argument("--generate-email", action="store_true", help="generate one mailbox local-part; intended for --email-type custom/icloud")
    p.add_argument("--generated-email-prefix", default="lu", help="prefix for --generate-email, default lu")
    p.add_argument("--login-existing", action="store_true", help="login existing accounts instead of creating account profile")
    p.add_argument("--proxy", help="HTTP/SOCKS proxy for ChatGPT/Auth/Sentinel")
    p.add_argument("--email-type", choices=["auto", "icloud", "custom"], default="auto", help="mailbox type")
    p.add_argument("--email-code-provider", choices=["auto", "extract_json", "openai_code_json"], default="auto")
    p.add_argument("--email-code-base-url", default=None)
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--code-timeout", type=float, default=90.0)
    p.add_argument("--trace-dir", type=Path, default=Path("runtime/traces"))
    p.add_argument("--no-trace", action="store_true")
    p.add_argument("--trace-sensitive", action="store_true")
    p.add_argument("--backend", choices=["curl_cffi", "httpx"], default="curl_cffi")
    p.add_argument("--birthdate", default="2000-01-01")
    p.add_argument("--display-name-prefix", default="Lu")
    p.add_argument("--session-output-dir", type=Path, default=None)


def _add_sms_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--bind-phone",
        action="store_true",
        help="after registration, run the HTTP add-phone flow using the configured SMS provider",
    )
    p.add_argument(
        "--sms-source",
        choices=["legacy", "http", "none"],
        default="legacy",
        help=(
            "where to load the SMS provider from. "
            "'legacy' (default) reads project-level config (sms_provider.get_sms_provider). "
            "'http' uses the --sms-* URL flags. 'none' disables phone binding."
        ),
    )
    p.add_argument("--sms-name", default="http", help="display name for --sms-source=http")
    p.add_argument("--sms-acquire-url", default=None, help="GET URL → returns a phone (E.164 plain text or JSON {phone:...})")
    p.add_argument("--sms-static-phone", default=None, help="reuse one fixed number for every activation (skip acquire)")
    p.add_argument("--sms-poll-url", default=None, help="GET URL → returns latest SMS body. {phone} is substituted")
    p.add_argument("--sms-release-complete-url", default=None, help="optional GET URL fired on success")
    p.add_argument("--sms-release-cancel-url", default=None, help="optional GET URL fired on cancel")
    p.add_argument("--sms-code-regex", default=r"\b(\d{4,8})\b", help="regex to pull the OTP from the poll body")
    p.add_argument("--sms-poll-interval", type=float, default=4.0)
    p.add_argument("--sms-timeout", type=float, default=20.0, help="per-request HTTP timeout for the SMS API")
    p.add_argument("--sms-header", action="append", default=[], metavar="HEADER:VALUE", help="extra HTTP header (repeatable)")
    p.add_argument("--sms-max-attempts", type=int, default=3, help="how many phone numbers to try")
    p.add_argument("--sms-otp-timeout", type=float, default=30.0, help="seconds to wait for each SMS before cancelling and rotating")
    p.add_argument("--sms-max-otp-retries", type=int, default=2, help="OTP resends before rotating to a new number")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gpt-trial",
        description="Protocol-only ChatGPT registrar (paid checkout via `run`, free + add-phone via `free`).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="register/login account(s) and generate trial checkout link")
    _add_common_email_args(run)
    run.add_argument("--checkout-country", default="US")
    run.add_argument("--checkout-currency", default="USD")
    run.add_argument("--out", type=Path, default=Path("runtime/results.jsonl"))

    free = sub.add_parser("free", help="register free account(s); optionally bind phone via SMS provider")
    _add_common_email_args(free)
    _add_sms_args(free)
    free.add_argument("--out", type=Path, default=Path("runtime/results_free.jsonl"))

    return parser


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def collect_email_items(args: argparse.Namespace) -> list[EmailItem]:
    text_parts: list[str] = []
    text_parts.extend(args.email or [])
    if args.emails:
        text_parts.append(args.emails)
    if args.emails_file:
        text_parts.append(args.emails_file.read_text(encoding="utf-8"))
    if args.generate_email:
        text_parts.append(generate_email_prefix(prefix=args.generated_email_prefix))
    items = parse_email_items("\n".join(text_parts))
    if not items:
        raise SystemExit("no email provided")
    return items


def emit(event: dict[str, Any]) -> None:
    print(json.dumps(event, ensure_ascii=False, separators=(",", ":")), flush=True)


def _parse_headers(values: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for raw in values or []:
        if ":" not in raw:
            raise SystemExit(f"invalid --sms-header (need 'Name: value'): {raw}")
        name, _, value = raw.partition(":")
        headers[name.strip()] = value.strip()
    return headers


def build_sms_provider(args: argparse.Namespace) -> SmsProvider | None:
    """Resolve the SMS provider for `gpt-trial free --bind-phone`.

    Returns None when no provider should run (--bind-phone omitted, --sms-source=none).
    Raises SystemExit when --bind-phone is requested but configuration is incomplete.
    """
    if not getattr(args, "bind_phone", False):
        return None
    source = (getattr(args, "sms_source", "legacy") or "legacy").lower()
    if source == "none":
        return None
    if source == "http":
        if not args.sms_poll_url:
            raise SystemExit("--sms-source=http requires --sms-poll-url")
        if not args.sms_acquire_url and not args.sms_static_phone:
            raise SystemExit("--sms-source=http requires --sms-acquire-url or --sms-static-phone")
        cfg = HttpSmsConfig(
            name=args.sms_name,
            acquire_url=args.sms_acquire_url,
            static_phone=args.sms_static_phone,
            poll_url=args.sms_poll_url,
            release_url_complete=args.sms_release_complete_url,
            release_url_cancel=args.sms_release_cancel_url,
            code_regex=args.sms_code_regex,
            poll_interval=args.sms_poll_interval,
            timeout=args.sms_timeout,
            headers=_parse_headers(args.sms_header),
        )
        return HttpSmsProvider(cfg)
    # legacy: read project-level config (`config.SMS_PROVIDER`, etc.)
    provider = load_legacy_default()
    if provider is None:
        raise SystemExit(
            "--bind-phone --sms-source=legacy requires the project-level SMS provider to be configured. "
            "Either set config.SMS_PROVIDER + SMSBOWER_API_KEY (or PAYPAL_SMS_URL for 62us), "
            "or switch to --sms-source=http with explicit URL flags."
        )
    return provider


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    items = collect_email_items(args)
    options = RunOptions(
        proxy=args.proxy,
        login_existing=args.login_existing,
        email_type=args.email_type,
        email_code_base_url=args.email_code_base_url,
        email_code_provider=args.email_code_provider,
        timeout=args.timeout,
        code_timeout=args.code_timeout,
        checkout_country=args.checkout_country,
        checkout_currency=args.checkout_currency,
        trace_dir=None if args.no_trace else args.trace_dir,
        trace_sensitive=args.trace_sensitive,
        backend=args.backend,
        birthdate=args.birthdate,
        display_name_prefix=args.display_name_prefix,
        session_output_dir=args.session_output_dir,
    )
    return _drive(items, args, runner=lambda item: run_one(item, options))


def cmd_free(args: argparse.Namespace) -> int:
    items = collect_email_items(args)
    sms_provider = build_sms_provider(args)
    options = FreeRunOptions(
        proxy=args.proxy,
        login_existing=args.login_existing,
        email_type=args.email_type,
        email_code_base_url=args.email_code_base_url,
        email_code_provider=args.email_code_provider,
        timeout=args.timeout,
        code_timeout=args.code_timeout,
        trace_dir=None if args.no_trace else args.trace_dir,
        trace_sensitive=args.trace_sensitive,
        backend=args.backend,
        birthdate=args.birthdate,
        display_name_prefix=args.display_name_prefix,
        session_output_dir=args.session_output_dir,
        sms_provider=sms_provider,
        sms_max_attempts=args.sms_max_attempts,
        sms_otp_timeout=args.sms_otp_timeout,
        sms_max_otp_retries=args.sms_max_otp_retries,
    )
    if sms_provider is not None:
        emit({"event": "sms_provider", "name": getattr(sms_provider, "name", "unknown")})
    return _drive(items, args, runner=lambda item: run_one_free(item, options))


def _drive(items: list[EmailItem], args: argparse.Namespace, *, runner) -> int:
    success = 0
    failure = 0
    for index, item in enumerate(items, start=1):
        try:
            item = normalize_email_item(item, email_type=args.email_type)
        except Exception as exc:
            failure += 1
            result = {
                "ok": False,
                "email": item.email,
                "stage": "email_input",
                "reason": str(exc),
                "exceptionType": type(exc).__name__,
            }
            append_jsonl(args.out, [result])
            emit(result | {"event": "failure", "index": index, "total": len(items)})
            continue

        emit({
            "event": "start",
            "index": index,
            "total": len(items),
            "email": item.email,
            "mode": "login" if getattr(args, "login_existing", False) else "register",
        })
        try:
            result = runner(item)
            append_jsonl(args.out, [result])
            if result.get("ok"):
                success += 1
                emit({
                    "event": "success",
                    "email": item.email,
                    "stage": result.get("stage"),
                    "checkoutUrl": result.get("checkoutUrl"),
                    "phone": (result.get("phone") or {}).get("number") if isinstance(result.get("phone"), dict) else None,
                    "sessionOutputPath": result.get("sessionOutputPath") or "",
                })
            else:
                failure += 1
                emit({
                    "event": "failure",
                    "email": item.email,
                    "stage": result.get("stage"),
                    "reason": result.get("reason") or result.get("phoneError"),
                })
        except Exception as exc:
            failure += 1
            result = {
                "ok": False,
                "email": item.email,
                "stage": "unexpected",
                "reason": str(exc),
                "exceptionType": type(exc).__name__,
            }
            append_jsonl(args.out, [result])
            emit(result | {"event": "failure"})
            traceback.print_exc(file=sys.stderr)
    emit({"event": "done", "success": success, "failure": failure, "out": str(args.out)})
    return 0 if failure == 0 else 2


def main(argv: list[str] | None = None) -> int:
    # 让 email_provider / phone_binding / 其它模块的 logging 能透到 webui 终端
    import logging as _logging
    if not _logging.getLogger().handlers:
        _logging.basicConfig(
            level=_logging.INFO,
            stream=sys.stderr,
            format="[%(asctime)s %(name)s] %(message)s",
            datefmt="%H:%M:%S",
        )
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "free":
        return cmd_free(args)
    parser.error(f"unknown command: {args.cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
