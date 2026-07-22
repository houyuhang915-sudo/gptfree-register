"""Protocol-only ChatGPT registration, trial checkout link, and add-phone."""

from .errors import (
    MissingFieldError,
    NoPhoneAvailableError,
    PhoneNumberInUseError,
    PhoneOtpInvalidError,
    ProtocolError,
    ProtocolResponseError,
    UnsupportedProtocolStep,
)
from .codex_oauth import (
    CodexOAuthProtocolError,
    CodexOAuthProtocolFlow,
    CodexOAuthProtocolResult,
    run_codex_oauth_protocol,
)
from .flows import LoginResult, ProtocolRegistrarFlow, RegistrationResult
from .models import (
    AccountInput,
    AuthStart,
    BrowserProfile,
    CheckoutInput,
    CheckoutLink,
    PhoneBindResult,
    ProtocolConfig,
    SessionInfo,
)
from .service import (
    EmailItem,
    FreeRunOptions,
    RunOptions,
    run_one,
    run_one_free,
)
from .sms import (
    HttpSmsConfig,
    HttpSmsProvider,
    LegacySmsProviderAdapter,
    SmsActivation,
    SmsProvider,
    from_legacy_provider,
    load_legacy_default,
)


__all__ = [
    "__version__",
    # errors
    "ProtocolError",
    "ProtocolResponseError",
    "MissingFieldError",
    "UnsupportedProtocolStep",
    "PhoneNumberInUseError",
    "PhoneOtpInvalidError",
    "NoPhoneAvailableError",
    "CodexOAuthProtocolError",
    # models
    "AccountInput",
    "AuthStart",
    "BrowserProfile",
    "CheckoutInput",
    "CheckoutLink",
    "PhoneBindResult",
    "ProtocolConfig",
    "SessionInfo",
    # flows
    "LoginResult",
    "ProtocolRegistrarFlow",
    "RegistrationResult",
    "CodexOAuthProtocolFlow",
    "CodexOAuthProtocolResult",
    "run_codex_oauth_protocol",
    # service
    "EmailItem",
    "FreeRunOptions",
    "RunOptions",
    "run_one",
    "run_one_free",
    # sms
    "SmsProvider",
    "SmsActivation",
    "LegacySmsProviderAdapter",
    "HttpSmsConfig",
    "HttpSmsProvider",
    "from_legacy_provider",
    "load_legacy_default",
]

__version__ = "0.3.0"
