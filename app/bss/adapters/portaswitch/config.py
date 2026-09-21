import json
from typing import Optional, List, Union

from pydantic import field_validator
from pydantic_settings import BaseSettings

from .types import (
    PortaSwitchContactsSelectingMode,
    PortaSwitchExtensionType,
    PortaSwitchSignInCredentialsType,
)


#: Hours of call history the adapter looks back over when the client asks for none.
#: 24 matches what PortaBilling's own admin and self-care portals send by default.
DEFAULT_CALL_HISTORY_WINDOW_HOURS = 24
#: A century, as a sanity bound on the configured window. Well before this the window
#: already reaches past the 1970 floor the adapter clamps to, so nothing is lost by
#: refusing to go further - and it keeps the timedelta built from this value nowhere
#: near the limits of the type.
CALL_HISTORY_MAX_WINDOW_HOURS = 100 * 365 * 24


def parse_string_list(value: Union[List, str, int, None]) -> List[str]:
    if not value:
        return []
    if isinstance(value, int):
        return [str(value)]
    if isinstance(value, str):
        return [x.strip() for x in value.split(';') if x.strip()]
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]


class PortaSwitchSettings(BaseSettings):
    ADMIN_API_URL: str
    ADMIN_API_LOGIN: str
    ADMIN_API_TOKEN: str
    ACCOUNT_API_URL: str
    SIP_SERVER_HOST: str = "127.0.0.1"
    SIP_SERVER_PORT: int = 5060
    VERIFY_HTTPS: Optional[bool] = True
    # Per-request timeout (seconds) for outbound PortaSwitch/PortaBilling API calls.
    # Overrides HTTPAPIConnector.DEFAULT_REQUEST_TIMEOUT for PortaSwitch only, so a
    # slow/unresponsive switch can't pin worker threads indefinitely (WT-1717).
    # Configurable via PORTASWITCH_API_TIMEOUT; set empty to keep the base default.
    API_TIMEOUT: Optional[float] = 25
    # httpx connection-pool limits for the async client (WT-1720). This pool — not
    # the old ~40 Starlette thread-pool tokens — is now the real per-pod ceiling on
    # concurrent requests toward the switch. Raise MAX_CONNECTIONS for a large switch
    # / high Cloud Run concurrency; lower it to protect a small or shared one.
    # Configurable via PORTASWITCH_MAX_CONNECTIONS /
    # PORTASWITCH_MAX_KEEPALIVE_CONNECTIONS.
    #
    # MAX_KEEPALIVE_CONNECTIONS unset means "as many as the pool holds" (WT-1973).
    # httpx's own default of 20 is meant for a client talking to many hosts; this
    # one talks only to PortaSwitch, so a keep-alive budget below the pool size buys
    # nothing and costs a TLS handshake per call. Worse, httpcore charges *active*
    # connections against that budget — its cleanup pass, which runs on every
    # response close, closes an idle connection whenever the pool's *total* count
    # exceeds max_keepalive_connections — so with a pool of 100 and a budget of 20
    # every connection was closed as soon as it answered, once more than 20 were
    # open at all. Leaving this None makes the budget follow MAX_CONNECTIONS: httpx
    # maps None to sys.maxsize and httpcore mins it with max_connections, and since
    # the pool never holds more than max_connections the surplus-idle branch becomes
    # unreachable. Set an explicit value only to cap idle sockets deliberately.
    MAX_CONNECTIONS: int = 100
    MAX_KEEPALIVE_CONNECTIONS: Optional[int] = None
    # Disaster-recovery failover for a geographically dispersed installation
    # (WT-1654). When a standby URL is unset, failover is disabled and behavior
    # is unchanged. On a main-site outage, API traffic fails over to the standby;
    # switch-back uses the operating_mode signal (BA-47641).
    ADMIN_API_URL_STANDBY: Optional[str] = None
    ACCOUNT_API_URL_STANDBY: Optional[str] = None
    # Seconds between out-of-band main-site probes while running on standby.
    SITE_RECHECK_INTERVAL: int = 60
    # Consecutive main-site 'normal' probes required to switch back (hysteresis).
    SITE_SWITCH_BACK_THRESHOLD: int = 2
    SIGNIN_CREDENTIALS: PortaSwitchSignInCredentialsType = PortaSwitchSignInCredentialsType.SELF_CARE
    CONTACTS_SELECTING: PortaSwitchContactsSelectingMode = PortaSwitchContactsSelectingMode.ACCOUNTS
    CONTACTS_SELECTING_EXTENSION_TYPES: Union[List[PortaSwitchExtensionType], str] = list(PortaSwitchExtensionType)
    CONTACTS_SELECTING_CUSTOMER_IDS: Union[List[str], str] = []
    CONTACTS_SKIP_WITHOUT_EXTENSION: bool = False
    # Seconds to reuse a customer's account list before refetching it (WT-1922).
    # 0 disables the cache, which is the default: enable it per installation.
    # Every contacts request otherwise re-reads the whole account list from the
    # switch — at the tenant of WT-1922 a median of 68 and up to 349 API calls
    # per request against a pool of MAX_CONNECTIONS — so a burst of clients
    # saturates the pool. With a TTL set, that cost is paid once per TTL per
    # customer no matter how many requests arrive. Pick a TTL comfortably above
    # the time one full read takes, or the entry expires as soon as it lands and
    # the switch is queried continuously.
    CONTACTS_CACHE_TTL: int = 0
    CONTACTS_CUSTOM: Union[List[dict], str] = []
    # Default look-back window (hours) for the call history when the client asks for
    # it without a date range (WT-1932). PortaBilling partitions CDR_ACCOUNTS by week
    # on bill_time - the very field from_date/to_date filter - and at the installation
    # of WT-1932 each non-empty partition held ~9-10M rows across ~30 partitions. The
    # adapter used to send from_date 1970-01-01 / to_date 9000-01-01, so returning 40
    # rows made the switch process the lot, twice over (once for the rows, once for
    # get_total), which took its web services down. 24 hours matches what PortaBilling's
    # own admin and self-care portals send by default. This is a *default*, never a cap:
    # a range the client asks for explicitly is passed through untouched, so older
    # records stay reachable. 0 restores the previous 1970-01-01 .. 9000-01-01 pair
    # exactly, and anything past CALL_HISTORY_MAX_WINDOW_HOURS is clamped to it.
    CALL_HISTORY_DEFAULT_WINDOW_HOURS: int = DEFAULT_CALL_HISTORY_WINDOW_HOURS
    HIDE_BALANCE_IN_USER_INFO: Optional[bool] = False
    SELF_CONFIG_PORTAL_URL: Optional[str] = None
    ALLOWED_ADDONS: Union[List[str], str] = []

    @field_validator("API_TIMEOUT", mode='before')
    @classmethod
    def decode_api_timeout(cls, v: Union[str, float, int, None]) -> Optional[float]:
        # Treat an empty/blank or non-positive value as "unset" so operators can
        # fall back to the base HTTPAPIConnector.DEFAULT_REQUEST_TIMEOUT (e.g.
        # PORTASWITCH_API_TIMEOUT="") and can't accidentally set a 0/negative
        # timeout, which requests treats as fail-immediately rather than "no limit".
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        return v if v > 0 else None

    @staticmethod
    def _positive_int_or(v: Union[str, int, None], default: Optional[int]) -> Optional[int]:
        # Treat blank/invalid/non-positive as "unset" so a stray empty env var
        # (e.g. PORTASWITCH_MAX_CONNECTIONS="") falls back to the safe default.
        if v is None or (isinstance(v, str) and not v.strip()):
            return default
        try:
            iv = int(v)
        except (TypeError, ValueError):
            return default
        return iv if iv > 0 else default

    @field_validator("CONTACTS_CACHE_TTL", mode='before')
    @classmethod
    def decode_contacts_cache_ttl(cls, v: Union[str, int, None]) -> int:
        # Unlike the pool limits, 0 is a meaningful value here — it means "no
        # caching" — so blank/invalid/negative all collapse to disabled rather
        # than to a non-zero default.
        if v is None or (isinstance(v, str) and not v.strip()):
            return 0
        try:
            iv = int(v)
        except (TypeError, ValueError):
            return 0
        return iv if iv > 0 else 0

    @field_validator("CALL_HISTORY_DEFAULT_WINDOW_HOURS", mode='before')
    @classmethod
    def decode_call_history_default_window_hours(cls, v: Union[str, int, None]) -> int:
        # Like CONTACTS_CACHE_TTL, 0 is meaningful here - it means "no default window",
        # i.e. the old open-ended query - so blank/invalid/negative collapse to the
        # 24-hour default rather than to 0. Switching the window off has to be a
        # deliberate "0", never the result of a stray empty env var. An absurdly large
        # value is clamped rather than rejected, as a sanity bound - a window that long
        # already reaches past the floor the adapter clamps to, so it can only be a
        # typo, and left unbounded it would eventually overflow the timedelta.
        if v is None or (isinstance(v, str) and not v.strip()):
            return DEFAULT_CALL_HISTORY_WINDOW_HOURS
        try:
            iv = int(v)
        except (TypeError, ValueError):
            return DEFAULT_CALL_HISTORY_WINDOW_HOURS
        if iv < 0:
            return DEFAULT_CALL_HISTORY_WINDOW_HOURS
        return min(iv, CALL_HISTORY_MAX_WINDOW_HOURS)

    @field_validator("MAX_CONNECTIONS", mode='before')
    @classmethod
    def decode_max_connections(cls, v: Union[str, int, None]) -> int:
        return cls._positive_int_or(v, 100)

    @field_validator("MAX_KEEPALIVE_CONNECTIONS", mode='before')
    @classmethod
    def decode_max_keepalive_connections(cls, v: Union[str, int, None]) -> Optional[int]:
        # None — not a number — is the "unset" value here, because that is what
        # httpx reads as "keep as many as the pool holds"; see the field comment.
        return cls._positive_int_or(v, None)

    @field_validator("CONTACTS_SELECTING_EXTENSION_TYPES", mode='before')
    @classmethod
    def decode_contacts_selecting_extension_types(cls, v: Union[List, str]) -> List[PortaSwitchExtensionType]:
        if v is None or (isinstance(v, list) and len(v) == 0):
            return list(PortaSwitchExtensionType)

        if isinstance(v, list) and all(isinstance(item, PortaSwitchExtensionType) for item in v):
            return v

        v = str(v)

        if not v or not v.strip():
            return list(PortaSwitchExtensionType)

        parts = [x.strip() for x in v.split(';') if x.strip()]
        if not parts:
            return list(PortaSwitchExtensionType)

        return [PortaSwitchExtensionType(x) for x in parts]

    @field_validator("CONTACTS_SELECTING_CUSTOMER_IDS", mode='before')
    @classmethod
    def decode_contacts_selecting_customer_ids(cls, v: Union[List, str, int, None]) -> List[str]:
        return parse_string_list(v)

    @field_validator("CONTACTS_CUSTOM", mode='before')
    @classmethod
    def decode_contacts_custom(cls, v: Union[List, str]) -> List[dict]:
        if v is None or (isinstance(v, list) and len(v) == 0):
            return []

        # If it's already a list of dicts, return it as-is
        if isinstance(v, list) and all(isinstance(item, dict) for item in v):
            return v

        # If it's a single dict, wrap it in a list
        if isinstance(v, dict):
            return [v]

        v = str(v)

        if not v or not v.strip():
            return []

        parts = [x.strip() for x in v.split(';') if x.strip()]
        if not parts:
            return []

        return [json.loads(x) for x in parts]

    @field_validator("ALLOWED_ADDONS", mode='before')
    @classmethod
    def decode_allowed_addons(cls, v: Union[List, str, int, None]) -> List[str]:
        return parse_string_list(v)

    model_config = {
        "env_prefix": "PORTASWITCH_",
        "env_file_encoding": "utf-8",
        "case_sensitive": False
    }


class OTPSettings(BaseSettings):
    IGNORE_ACCOUNTS: Union[List[str], str] = []
    STORAGE_COLLECTION: Optional[str] = None
    STORAGE_TTL_MINUTES: int = 30

    @field_validator("IGNORE_ACCOUNTS", mode='before')
    @classmethod
    def decode_ignore_accounts(cls, v: Union[List, str, int, None]) -> List[str]:
        return parse_string_list(v)

    model_config = {
        "env_prefix": "OTP_",
        "env_file_encoding": "utf-8",
        "case_sensitive": False
    }


class Settings(BaseSettings):
    JANUS_SIP_FORCE_TCP: bool = False
    ENABLE_ON_DEMAND_SESSION_MIGRATION: bool = False

    PORTASWITCH_SETTINGS: PortaSwitchSettings = PortaSwitchSettings()
    OTP_SETTINGS: OTPSettings = OTPSettings()

    model_config = {
        "env_file_encoding": "utf-8",
        "case_sensitive": False
    }
