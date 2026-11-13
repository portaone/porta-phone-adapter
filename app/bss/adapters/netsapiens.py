import logging
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from json import JSONDecodeError, loads as load_json
from typing import List, Dict, Tuple, Optional

import requests
from jose import jwt as jose_jwt, JWTError
from pydantic import BaseModel, Field, ValidationError

from app_config import AppConfig
from bss.adapters import BSSAdapter
from bss.dbs.firestore import FirestoreKeyValue
from bss.http_api import HTTPAPIConnectorWithLogin, OAuthSessionData, APIUser
from bss.sessions import configure_session_storage
from bss.types import (Capabilities, UserInfo, EndUser, ContactInfo,
                       CDRInfo, ConnectStatus, SessionInfo,
                       SIPRegistrationStatus, Direction,
                       Balance, BalanceType, Numbers, SIPServer, SIPInfo,
                       OTPCreateResponse, OTPVerifyRequest,
                       UserVoicemailsResponse, VoicemailMessage, VoicemailMessageDetails,
                       VoicemailMessageAttachment, VoicemailMessageType, UserVoicemailMessagePatch,
                       )
from report_error import WebTritErrorException, raise_webtrit_error
from request_trace import sanitize_data

VERSION = "0.4.0"

NS_API_TIMEOUT = 20


# Interface to Netsapiens cloud PBX https://docs.ns-api.com/reference/

class NetsapiensDeviceFilter:
    """Logic of finding the device entry for WebTrit in the list of devices.

    Mind the name: NetSapiens rings a user's devices (`<OwnDevices>` in the default answer
    rule) only when the device is named `<user>`, `<user>` plus one letter (`1001a`) or one
    of its own suffixes (`1001wp`). A device named `1001wt` registers but never gets an
    incoming call, so the filter is normally a single letter."""

    def __init__(self, pattern: str = "wt"):
        # Marker of the correct device entry in the list of devices
        self.pattern = pattern

    def find_device_entry(self, devices: List[Dict]) -> Optional[Dict]:
        """Find the device entry that matches the pattern"""
        return next((device for device in devices if device.get("device", "").endswith(self.pattern)), None)


class NetsapiensClient(BaseModel):
    client_id: str = Field(default=None,
                           description="OAuth client ID, needed only for the password sign-in")
    client_secret: str = Field(default=None,
                               description="OAuth client secret, needed only for the password sign-in")
    api_server: str = Field(default=None,
                            description="API server URL")
    domain: str = Field(default=None,
                        description="SIP domain (returned after the login)")
    device_filter: str = Field(default="WebTrit",
                               description="Suffix of the user's device that the app registers as")
    default_sip_outbound_proxy: Optional[str] = Field(default=None)
    default_sip_port: int = Field(default=5060)
    api_key: Optional[str] = Field(default=None,
                                   description="Domain API key: SIP account, delegated user tokens, "
                                               "voicemail, recordings, DIDs")

    @property
    def has_oauth_client(self) -> bool:
        return bool(self.client_id and self.client_secret)


class NetsapiensUser(APIUser):
    user_id: str
    password: str = Field(default=None)
    ns_client: NetsapiensClient = Field(default=None,
                                        description="Info about NS tenant")
    api_key: Optional[str] = Field(default=None)


class NetsapiensAPI(HTTPAPIConnectorWithLogin):
    def __init__(self, api_server: str, **kwargs):
        # api server is not used, since it is individual for each client (tenant)
        self.clients = {}
        if "netsapiens_clients" in kwargs:
            self.clients = kwargs.pop("netsapiens_clients")
        self.token_storage = kwargs.pop("token_storage", "firestore")

        super().__init__(api_server, **kwargs)

    def init_token_storage(self):
        """Storage for the per-user NetSapiens tokens: Firestore by default,
        memory when NETSAPIENS_TOKEN_STORAGE=memory (one replica, or a lab without Google Cloud).
        A token lost with the memory storage is minted again from the domain API key."""
        if self.SHARED_TOKENS is None:
            if self.token_storage == "memory":
                self.SHARED_TOKENS = {}
            else:
                self.SHARED_TOKENS = FirestoreKeyValue(collection_name="NetsapiensTokens")
        return self.SHARED_TOKENS

    def user_id(self, user: APIUser) -> str:
        # The base class keys tokens by str(user), which includes the password and the client:
        # a token stored at sign-in was never found again by calls that pass only user_id.
        return getattr(user, "user_id", None) or (str(user) if user else None)

    def split_uid(self, uid: str) -> Tuple[str, str]:
        """Split the user ID like abc@xyz.com into domain xyz.com and user name abc"""
        parts = uid.split('@')
        if len(parts) >= 2:
            return (parts[0], parts[1])
        return (None, None)

    def get_client(self, user_id: str) -> NetsapiensClient:
        """Obtain the NS client object for the given user"""
        uid, domain = self.split_uid(user_id)
        if domain:
            return self.clients.get(domain)

        raise KeyError(f"Could not find a client entry for {user_id}")

    def get_api_server(self, user_id: str, default: str = None) -> str:
        """Obtain server API URL for the given user"""
        client = self.get_client(user_id)
        return client.api_server

    def access_token_path(self) -> str:
        return "/ns-api/v2/tokens"

    def send_rest_request(self,
                          method: str,
                          path: str,
                          server=None,
                          data=None,
                          json=None,
                          query_params=None,
                          headers={'Content-Type': 'application/json'},
                          turn_off_login=False,
                          user: NetsapiensUser = None) -> dict:
        """Override the parent method to place there the correct API_URL"""
        if user:
            if user.ns_client is None:
                user.ns_client = self.get_client(user.user_id)
                self.api_server = user.ns_client.api_server
            else:
                # set the API server for this user
                if user.ns_client and user.ns_client.api_server:
                    self.api_server = user.ns_client.api_server
                else:
                    self.api_server = self.get_api_server(user_id=user.user_id)

            # No stored token (a restart with the memory storage, or a session created by SSO):
            # mint a delegated one from the domain API key instead of failing with "re-login".
            if (not turn_off_login and user.password is None and user.ns_client
                    and user.ns_client.api_key and self.get_auth_session(user) is None):
                self.login_delegated(user)

        return super().send_rest_request(
            method, path, server, data, json, query_params, headers, turn_off_login, user
        )

    def login(self, user: NetsapiensUser) -> OAuthSessionData:
        """Password grant with the domain's OAuth client; without a password, a delegated token."""
        if user.password is None:
            return self.login_delegated(user)

        req_data = dict(
            client_id=user.ns_client.client_id,
            client_secret=user.ns_client.client_secret,
            username=user.user_id,
            password=user.password,
            grant_type="password",
        )
        self.api_server = self.get_api_server(user_id=user.user_id,
                                              default=user.ns_client.api_server)
        res = self.send_rest_request(
            "POST",
            self.access_token_path(),
            headers={"Content-Type": "application/json"},
            json=req_data,
            turn_off_login=True,
            user=user
        )
        if res and (session := self.extract_access_token(res)):
            # access token was extracted and stored - remember the session
            # for this user
            self.store_auth_session(session, user)
            return session

        logging.debug(f"Could not find an access token in the response {sanitize_data(res)}")
        raise ValueError("Could not find an access token in the response")

    def login_delegated(self, user: NetsapiensUser) -> OAuthSessionData:
        """A NetSapiens token that acts as the user, minted with the domain API key
        (POST /ns-api/v2/jwt {user, domain}). Needs neither the user's password nor an OAuth client."""
        uid, domain = self.split_uid(user.user_id)
        client = user.ns_client or self.get_client(user.user_id)
        if not client or not client.api_key:
            raise WebTritErrorException(status_code=401, error_message="User authentication error")
        r = requests.post(f"{client.api_server.rstrip('/')}/ns-api/v2/jwt",
                          json={"user": uid, "domain": domain},
                          headers={"Authorization": f"Bearer {client.api_key}", "Accept": "application/json"},
                          timeout=NS_API_TIMEOUT)
        token = None
        if r.status_code == 200:
            try:
                token = (r.json() or {}).get("token")
            except ValueError:
                token = None
        if not token:
            logging.warning(f"Delegated token for {user.user_id} refused: HTTP {r.status_code}")
            raise WebTritErrorException(status_code=401, error_message="User authentication error")
        # NetSapiens issues it for 4 hours; renew a little earlier
        session = OAuthSessionData(access_token=token, access_token_expires_at=datetime.now() + timedelta(hours=3))
        self.store_auth_session(session, user)
        return session

    def refresh(self, user: NetsapiensUser, auth_session: OAuthSessionData):
        """Refresh access token using refresh_token; a delegated token is simply minted again"""
        client = user.ns_client or self.get_client(user.user_id)
        if not auth_session.refresh_token or not client.has_oauth_client:
            return self.login_delegated(user)

        endpoint = self.access_token_path()

        req_data = dict(
            client_id=client.client_id,
            client_secret=client.client_secret,
            grant_type="refresh_token",
            refresh_token=auth_session.refresh_token
        )

        response = self.send_rest_request(
            "POST",
            endpoint,
            headers={"Content-Type": "application/json"},
            json=req_data,
            turn_off_login=True,
            user=user
        )

        if response and (session := self.extract_access_token(response)):
            # access token was extracted and stored - remember the session
            # for this user
            self.store_auth_session(session, user)
            return session

        raise WebTritErrorException(
            status_code=401,
            error_message="Unable to refresh API token, user has to re-login"
        )

    DEVICE_PATH = "/ns-api/v2/domains/<domain>/users/<user_id>/devices"

    def get_extension(self, user_id: str, api_key: Optional[str] = None) -> Optional[Dict]:
        """Get the extension info"""

        uid, domain = self.split_uid(user_id)
        path = self.DEVICE_PATH.replace("<domain>", domain).replace("<user_id>", uid)
        device_list = self.send_rest_request("GET", path, json={},
                                             user=NetsapiensUser(user_id=user_id, api_key=api_key))
        if not device_list:
            return None

        ns_filter = NetsapiensDeviceFilter(pattern=self.get_client(user_id).device_filter)
        if isinstance(device_list, list):
            # need to figure out which one is the right one
            correct_device = ns_filter.find_device_entry(device_list)
            if not correct_device:
                # TODO: ensure it is visible in the app
                raise WebTritErrorException(
                    status_code=422,
                    error_message=f"Cannot find a proper device entry on Netsapiens side in {device_list}",
                )
            return correct_device

        return device_list

    CONTACTS_PATH = "/ns-api/v2/domains/<domain>/users/<user_id>/contacts"

    def get_all_extensions(self, user_id: str) -> List[Dict]:
        """Get all extensions defined in the PBX"""

        uid, domain = self.split_uid(user_id)
        path = self.CONTACTS_PATH.replace("<domain>", domain).replace("<user_id>", uid)

        reply = self.send_rest_request(
            "GET", path, json={},
            query_params={"includeDomain": "yes"},
            user=NetsapiensUser(user_id=user_id)
        )
        if reply:
            return reply

        return []

    CDR_PATH = "/ns-api/v2/domains/<domain>/users/<user_id>/cdrs"

    def get_cdrs(self, user_id: str,
                 start_time: datetime = None,
                 end_time: datetime = None) -> Dict:
        """Get user's CDRs"""

        uid, domain = self.split_uid(user_id)
        path = self.CDR_PATH.replace("<domain>", domain).replace("<user_id>", uid)

        q_params = {}
        # these seem to have no effect :-( - the adapter filters by time itself
        if start_time:
            q_params["datetime-start"] = start_time.isoformat()
        if end_time:
            q_params["datetime-end"] = end_time.isoformat()

        cdr_list = self.send_rest_request("GET", path, json={},
                                          query_params=q_params,
                                          user=NetsapiensUser(user_id=user_id))
        return cdr_list


class NetsapiensAdapter(BSSAdapter):
    """Connect WebTrit and Netsapiens. Authenticate a user using the API,
    retrieve user's SIP credentials to be used by
    WebTrit and return a list of other configured extensions (to
    be provided as 'Cloud PBX' contacts).
    Currently does not support OTP login.

    Config variables:

    NETSAPIENS_CLIENTS = JSON string with a list of dicts, each containing:
        - api_server
        - domain
            Domain visible to the end-user; users are expected to login as
            username@domain
        - api_key
            Domain API key (Office Manager scope limited to the domain is enough)
        - client_id, client_secret
            OAuth client, only for the password sign-in
        - device_filter
            suffix of the user's device the app registers as. Default: "WebTrit"
    NETSAPIENS_CLIENTS_FIRESTORE = "<name of the collection in Firestore>"
        Alternative method for storing the list of client, more suitable for
        production systems. The clients are stored in the Firestore
        collection.

    Either NETSAPIENS_CLIENTS or NETSAPIENS_CLIENTS_FIRESTORE env var should be defined.
    The other options (SSO, web tab tokens, token storage) are described in README.md.
    """

    def get_client_list(self, config: AppConfig) -> List[NetsapiensClient]:
        """Get the list of Netsapiens clients"""

        if collection := config.get_conf_val("Netsapiens", "Clients", "Firestore",
                                             default=""):
            cfg = FirestoreKeyValue(collection_name=collection)
            clients = {
                v.get("domain"): NetsapiensClient(**v)
                for k, v in cfg.items()
            }
            return clients
        elif client_list := config.get_conf_val(
                "Netsapiens", "Clients", default=""
        ):
            logging.debug("Loading the client list from NETSAPIENS_CLIENTS")
            clients = {}
            try:
                clients = {
                    c.get("domain"): NetsapiensClient(**c)
                    for c in load_json(client_list)
                }
            except JSONDecodeError as e:
                logging.error(f"Could not parse the client list: {e}")
            return clients

        raise ValueError("Netsapiens client list is not defined in the config " +
                         "specify either NETSAPIENS_CLIENTS or NETSAPIENS_CLIENTS_FIRESTORE")

    def __init__(self, config: AppConfig):
        super().__init__(config)

        self.clients = self.get_client_list(config)
        logging.debug(f"NS Clients: {list(self.clients)}")

        self.api_client = NetsapiensAPI(
            api_server="http://localhost",  # does not really matter
            netsapiens_clients=self.clients,
            token_storage=str(config.get_conf_val("Netsapiens", "Token", "Storage",
                                                  default="firestore")).lower(),
        )
        # need to init the sessions since we are derived from BSSAdapter
        self.sessions = configure_session_storage(config)

        # sign-in with a token signed by the customer's SaaS (deeplink, QR, web widget, embedded page)
        self.sso_secret = config.get_conf_val("Netsapiens", "SSO", "Secret", default="")
        self.sso_issuer = config.get_conf_val("Netsapiens", "SSO", "Issuer", default="")
        self.sso_audience = config.get_conf_val("Netsapiens", "SSO", "Audience", default="portadialer")
        self.sso_signup = str(config.get_conf_val("Netsapiens", "SSO", "Signup", default="false")).lower() == "true"
        # token for the customer's pages shown as app tabs
        self.tab_secret = config.get_conf_val("Netsapiens", "Tab", "Token", "Secret", default="") or self.sso_secret
        self.tab_issuer = config.get_conf_val("Netsapiens", "Tab", "Token", "Issuer", default="portadialer")
        self.tab_audience = config.get_conf_val("Netsapiens", "Tab", "Token", "Audience", default="") or self.sso_issuer
        self.tab_ttl = int(config.get_conf_val("Netsapiens", "Tab", "Token", "TTL", default=600))
        self._used_jti = {}

    @classmethod
    def name(cls) -> str:
        return "Netsapiens adapter"

    @classmethod
    def version(cls) -> str:
        global VERSION
        return VERSION

    def get_capabilities(self) -> list:
        """Capabilities of your hosted PBX / BSS / your API adapter"""
        capabilities = [
            Capabilities.callHistory,
            Capabilities.extensions,
            Capabilities.voicemail,
            Capabilities.voicemail_save,
            # trash and forward are Core-side features on top of voicemail
            Capabilities.voicemail_trash,
            Capabilities.voicemail_forward,
            Capabilities.recordings,
            # presence between apps lives in Core; without this entry Core switches it off
            Capabilities.direct_presence,
        ]
        if any(c.has_oauth_client for c in self.clients.values()):
            # the password grant needs the domain's OAuth client
            capabilities.append(Capabilities.passwordSignin)
        if self.sso_secret:
            capabilities.append(Capabilities.autoProvision)
            if self.sso_signup:
                # the mobile app's embedded sign-in page goes through signup; the web dialer
                # shows a sign-up tab for it, so it is opt-in
                capabilities.append(Capabilities.signup)
        if self.tab_secret:
            capabilities.append(Capabilities.customMethods)
        return capabilities

    def generate_otp(self, user: UserInfo) -> OTPCreateResponse:
        """Not supported"""
        pass

    def validate_otp(self, otp: OTPVerifyRequest) -> SessionInfo:
        """Not supported"""
        pass

    def split_uid(self, uid: str) -> Tuple[str, str]:
        """Split the user ID like abc@xyz.com into domain xyz.com and user name abc"""
        parts = uid.split('@')
        if len(parts) >= 2:
            return (parts[0], parts[1])
        return (None, None)

    def extract_user_id(self, user_data: object) -> str:
        """Extract user_id (unique and unmutable identifier of the user)
        from the data in the DB. Please override it in your sub-class"""
        # for production systems it is more appropriate to use 'id'
        # which is a permanent identifier of the user; but for testing
        # extension number is more convenient
        return user_data.get("uid", None)

    @staticmethod
    def _id(value) -> str:
        """Session user ids and recording ids arrive as pydantic RootModels"""
        return str(getattr(value, "root", value))

    # ------------------------------------------------------------------ NetSapiens REST with the domain API key
    def _ns(self, method: str, user_id: str, path: str, **kw):
        _, domain = self.split_uid(user_id)
        client = self.clients.get(domain)
        if not client or not client.api_key:
            raise_webtrit_error(500, error_message=f"No API key configured for the NetSapiens domain {domain}")
        url = f"{client.api_server.rstrip('/')}/ns-api/v2/domains/{domain}{path}"
        return requests.request(method, url, headers={"Authorization": f"Bearer {client.api_key}",
                                                      "Accept": "application/json"},
                                timeout=NS_API_TIMEOUT, **kw)

    # ------------------------------------------------------------------ sign-in
    def authenticate(self, user: UserInfo, password: str = None) -> SessionInfo:
        """Authenticate user with username and password and obtain an API token for
        further requests."""

        username, domain = self.split_uid(user.login)

        if not username or not domain:
            raise WebTritErrorException(
                status_code=422,
                error_message=f"Invalid login {user.login}, it should contain both username and client id, e.g. 100@abc",
            )
        client = self.clients.get(domain)
        if client is None:
            raise WebTritErrorException(
                status_code=422,
                error_message=f"Unknown Netsapiens domain {domain}",
            )
        if not client.has_oauth_client or not password:
            # without an OAuth client NetSapiens cannot check the password at all
            raise WebTritErrorException(status_code=401, error_message="User authentication error")
        token_data = self.api_client.login(NetsapiensUser(
            user_id=user.login,
            password=password,
            ns_client=client
        ))
        if token_data:
            # everything is in order, create a session
            user.user_id = user.login
            session = self.sessions.create_session(user)
            self.sessions.store_session(session)
            return session

        # something is wrong. your code should return a more descriptive
        # error message to simplify the process of fixing the problem
        raise WebTritErrorException(
            status_code=401,
            error_message="User authentication error",
        )

    def _sso_user(self, token: str) -> str:
        """The NetSapiens user a token signed by the customer's SaaS vouches for.
        Checks the signature (HS256, NETSAPIENS_SSO_SECRET), issuer, audience, expiry and that
        the token (its jti) was not used before."""
        if not self.sso_secret:
            raise_webtrit_error(501, error_message="Sign-in with a token is not configured")
        try:
            claims = jose_jwt.decode(token, self.sso_secret, algorithms=["HS256"],
                                     audience=self.sso_audience, issuer=self.sso_issuer or None)
        except JWTError as e:
            raise_webtrit_error(401, error_message=f"Sign-in token rejected: {e}", extra_error_code="invalid_token")
        now = datetime.now(timezone.utc).timestamp()
        self._used_jti = {k: v for k, v in self._used_jti.items() if v > now}
        jti = claims.get("jti")
        if not jti or jti in self._used_jti:
            raise_webtrit_error(401, error_message="Sign-in token already used", extra_error_code="invalid_token")
        self._used_jti[jti] = claims.get("exp", now + 300)
        user_id = claims.get("sub", "")
        _, domain = self.split_uid(user_id)
        if not domain or domain not in self.clients:
            raise_webtrit_error(401, error_message=f"Unknown NetSapiens user {user_id}",
                                extra_error_code="invalid_token")
        return user_id

    def _session_for(self, user_id: str) -> SessionInfo:
        """A session for a user the customer's SaaS vouched for: no password, a delegated NS token"""
        client = self.clients[self.split_uid(user_id)[1]]
        self.api_client.login_delegated(NetsapiensUser(user_id=user_id, ns_client=client))
        session = self.sessions.create_session(UserInfo(user_id=user_id, login=user_id))
        self.sessions.store_session(session)
        return session

    def autoprovision_session(self, config_token: str, tenant_id: str = None) -> SessionInfo:
        """Deeplink, QR code and the web dialer widget: config_token is the SaaS-signed token"""
        return self._session_for(self._sso_user(config_token))

    def signup(self, user_data, tenant_id: str = None):
        """The mobile app's embedded sign-in page posts the SaaS-signed token as sso_token"""
        extra = getattr(user_data, "model_extra", None) or (user_data if isinstance(user_data, dict) else {})
        token = extra.get("sso_token") or extra.get("config_token")
        if not token:
            raise_webtrit_error(422, error_message="sso_token is missing", extra_error_code="validation_error")
        return self._session_for(self._sso_user(token))

    # ------------------------------------------------------------------ token for the customer's web tabs
    def custom_method_private(self, session, user_id, method_name, data, headers={}, tenant_id=None, lang=None,
                              **kw):
        if method_name != "external-page-access-token" or not self.tab_secret:
            raise_webtrit_error(404, error_message=f"Unknown method {method_name}")
        uid = self._id(user_id) if user_id else self._id(session.user_id)
        exp = datetime.now(timezone.utc) + timedelta(seconds=self.tab_ttl)
        claims = {"iss": self.tab_issuer, "sub": uid, "exp": int(exp.timestamp())}
        if self.tab_audience:
            claims["aud"] = self.tab_audience
        token = jose_jwt.encode(claims, self.tab_secret, algorithm="HS256")
        # the app asks for a new token when this one expires, so there is no separate refresh flow
        return {"access_token": token, "refresh_token": token, "expires_at": exp.isoformat()}

    # ------------------------------------------------------------------ user and contacts
    def retrieve_user(self, session: SessionInfo, user: UserInfo) -> EndUser:
        """Obtain user's information - most importantly, his/her SIP credentials."""

        _username, domain = self.split_uid(user.user_id)
        client = self.clients.get(domain)
        device = self.api_client.get_extension(user.user_id, client.api_key)
        if not device:
            raise WebTritErrorException(status_code=404, error_message=f"User with ID {user.user_id} not found")

        # need to append the info like email address from contacts
        ext_list = self.api_client.get_all_extensions(user.user_id)
        if ext_list:
            # we need to find the correct "device" entry in the list
            for ext in ext_list:
                if ext.get("uid") == user.user_id:
                    device.update(ext)
                    break
        end_user = self.netsapiens_to_webtrit_obj(device, produce_user_info=True, client=client)
        self._apply_dids(end_user, user.user_id)
        return end_user

    def _apply_dids(self, end_user: EndUser, user_id: str):
        """DIDs are not on the user: take the ones routed to this user, so Core can find the
        user by phone number (a third-party push addresses users by number)"""
        if not end_user.numbers:
            return
        uid, _ = self.split_uid(user_id)
        try:
            r = self._ns("GET", user_id, "/phonenumbers")
            numbers = r.json() if r.status_code == 200 else []
        except Exception as e:
            logging.warning(f"Could not read the DIDs of {user_id}: {e}")
            return
        dids = [f"+{p['phonenumber']}" for p in numbers or []
                if p.get("phonenumber") and str(p.get("dial-rule-translation-destination-user")) == str(uid)]
        if dids:
            end_user.numbers.main = dids[0]
            end_user.numbers.additional = dids[1:]

    def retrieve_contacts(self, session: SessionInfo, user: UserInfo) -> List[ContactInfo]:
        """List of other extensions in the PBX"""

        ext_list = self.api_client.get_all_extensions(user.user_id)

        contacts = [
            self.netsapiens_to_webtrit_obj(x, produce_user_info=False)
            for x in ext_list
            if x.get("uid") != user.user_id
        ]
        self._apply_registration_state(contacts, user.user_id)
        return contacts

    def _apply_registration_state(self, contacts: List[ContactInfo], user_id: str):
        """The contacts endpoint carries no registration state: read it from the domain's devices"""
        try:
            r = self._ns("GET", user_id, "/devices")
            devices = r.json() if r.status_code == 200 else None
        except Exception as e:
            logging.warning(f"Could not read device registration states: {e}")
            return
        if not isinstance(devices, list):
            return
        online = {str(d.get("user")) for d in devices if d.get("device-sip-registration-state") == "registered"}
        for c in contacts:
            ext = c.numbers.ext if c.numbers else None
            c.sip_status = SIPRegistrationStatus.registered if ext in online else SIPRegistrationStatus.notregistered

    def retrieve_contacts_v2(
            self,
            session: SessionInfo,
            user: UserInfo,
            search: Optional[str] = None,
            phone_numbers: List[str] = [],
            page: Optional[int] = 1,
            items_per_page: Optional[int] = 100,
            sms_capable: bool = False,
    ) -> tuple[List[ContactInfo], int]:
        """
        Retrieve contacts with pagination, search and the lookup by phone numbers.

        Returns:
            (paginated_contacts, total_count)
        """

        page = page or 1
        items_per_page = items_per_page or 100
        contacts = self.retrieve_contacts(session, user)
        if sms_capable:
            contacts = [c for c in contacts if c.numbers and c.numbers.sms]
        if phone_numbers:
            wanted = {str(p).lstrip("+") for p in phone_numbers}
            contacts = [c for c in contacts if c.numbers and wanted & {
                str(n).lstrip("+") for n in [c.numbers.main, c.numbers.ext, *(c.numbers.additional or [])] if n}]
        if search:
            s = search.lower()
            contacts = [c for c in contacts
                        if s in f"{c.first_name or ''} {c.last_name or ''}".lower()
                        or (c.numbers and any(s in str(n) for n in [c.numbers.main, c.numbers.ext] if n))]

        start_index = (page - 1) * items_per_page
        end_index = start_index + items_per_page

        return contacts[start_index:end_index], len(contacts)

    # ------------------------------------------------------------------ call history and recordings
    def retrieve_calls(self,
                       session: SessionInfo,
                       user: UserInfo,
                       page: Optional[int] = 1,
                       items_per_page: Optional[int] = 100,
                       time_from: Optional[datetime] = None,
                       time_to: Optional[datetime] = None,
                       time_floor: Optional[datetime] = None,
                       ) -> Tuple[List[CDRInfo], int]:
        if time_floor is not None and (time_from is None or time_from < time_floor):
            time_from = time_floor
        page = page or 1
        items_per_page = items_per_page or 100
        me, _ = self.split_uid(user.user_id)

        cdrs = self.api_client.get_cdrs(user.user_id, start_time=time_from, end_time=time_to) or []
        items = [self.netsapiens_to_webtrit_cdr(c, me=me) for c in cdrs]

        def aware(t):
            return t if t is None or t.tzinfo else t.replace(tzinfo=timezone.utc)

        # NetSapiens ignores the date parameters
        if time_from:
            items = [i for i in items if i.connect_time and aware(i.connect_time) >= aware(time_from)]
        if time_to:
            items = [i for i in items if i.connect_time and aware(i.connect_time) <= aware(time_to)]
        items.sort(key=lambda i: aware(i.connect_time) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

        start = (page - 1) * items_per_page
        page_items = items[start:start + items_per_page]

        # the CDR does not say whether the call was recorded: ask for this page's answered calls
        answered = [i for i in page_items if i.status == ConnectStatus.accepted and i.call_id]
        if answered:
            with ThreadPoolExecutor(max_workers=8) as pool:
                found = list(pool.map(lambda i: self._recording(user.user_id, i.call_id), answered))
            for item, rec in zip(answered, found):
                if rec:
                    item.recording_id = item.call_id

        return page_items, len(items)

    def _recording(self, user_id: str, call_id: str) -> Optional[dict]:
        uid, _ = self.split_uid(user_id)
        try:
            r = self._ns("GET", user_id, f"/users/{uid}/recordings/{call_id}")
        except requests.RequestException as e:
            logging.warning(f"Could not read the recording of {call_id}: {e}")
            return None
        if r.status_code != 200:
            return None
        d = r.json()
        d = d[0] if isinstance(d, list) and d else d
        return d if isinstance(d, dict) and d.get("call-id") else None

    def retrieve_call_recording(self, session: SessionInfo, recording_id) -> tuple:
        """The recording of a call, as WAV. NetSapiens keeps it as GSM 6.10, which browsers cannot
        play, so it is re-encoded to PCM when ffmpeg is installed."""
        recording_id = self._id(recording_id)
        rec = self._recording(self._id(session.user_id), recording_id)
        if not rec:
            raise_webtrit_error(404, error_message="Recording not found", extra_error_code="recording_not_found")
        r = requests.get(rec["file-access-url"], timeout=60)
        if r.status_code != 200 or not r.content:
            # NetSapiens keeps a recording "unconverted" for a minute or two after the call
            raise_webtrit_error(404, error_message=f"Recording is not ready yet ({rec.get('call-recording-status')})",
                                extra_error_code="recording_not_found")
        data = r.content
        if shutil.which("ffmpeg"):
            try:
                out = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-acodec", "pcm_s16le",
                                      "-ar", "8000", "-ac", "1", "-f", "wav", "pipe:1"],
                                     input=data, capture_output=True, timeout=30, check=True).stdout
                if out:
                    data = out
            except (subprocess.SubprocessError, OSError) as e:
                logging.warning(f"Recording re-encode failed, sending the original: {e}")
        return "audio/wav", iter([data])

    # ------------------------------------------------------------------ voicemail
    # NetSapiens keeps voicemail in folders: "new" (not heard) and "save" (heard and kept);
    # deleting moves a message to its "trash" folder.
    VM_FOLDERS = ("new", "save")

    @staticmethod
    def _size_bytes(text) -> int:
        try:
            num, unit = str(text).split()
            return int(float(num) * {"B": 1, "KB": 1024, "MB": 1024 ** 2}.get(unit.upper(), 1))
        except (ValueError, TypeError):
            return 0

    def _vm_all(self, user_id: str) -> List[dict]:
        uid, _ = self.split_uid(user_id)
        out = []
        for folder in self.VM_FOLDERS:
            r = self._ns("GET", user_id, f"/users/{uid}/voicemails/{folder}")
            if r.status_code == 200:
                out += [dict(m, _folder=folder) for m in (r.json() or [])]
        return out

    def _vm_find(self, user_id: str, message_id: str) -> dict:
        for m in self._vm_all(user_id):
            if m.get("filename") == message_id:
                return m
        raise_webtrit_error(404, error_message="Voicemail message not found", extra_error_code="message_not_found")

    def _vm_model(self, m: dict, user_id: str) -> dict:
        created = m.get("created-datetime")
        return dict(
            id=m["filename"],
            type=VoicemailMessageType.VOICE,
            duration=float(m.get("file-duration-seconds") or 0),
            size=self._size_bytes(m.get("file-size-kilobytes")),
            date=datetime.fromisoformat(created) if created else datetime.now(timezone.utc),
            seen=m["_folder"] != "new",
            saved=m["_folder"] == "save",
            # the live API answers voicemail-from-*, the API reference still shows FromUser / FromName
            sender=str(m.get("voicemail-from-user") or m.get("FromUser") or m.get("voicemail-from-name") or ""),
            receiver=self.split_uid(user_id)[0],
        )

    def retrieve_voicemails(self, session, user, from_date=None, to_date=None) -> UserVoicemailsResponse:
        msgs = [self._vm_model(m, user.user_id) for m in self._vm_all(user.user_id)]

        def as_dt(x):
            if x is None:
                return None
            d = x if isinstance(x, datetime) else datetime.fromisoformat(str(x).replace("Z", "+00:00"))
            # clients send plain dates (2026-11-14), which parse as naive
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)

        f, t = as_dt(from_date), as_dt(to_date)
        if f:
            msgs = [m for m in msgs if m["date"] >= f]
        if t:
            msgs = [m for m in msgs if m["date"] <= t]
        msgs.sort(key=lambda m: m["date"], reverse=True)
        return UserVoicemailsResponse(messages=[VoicemailMessage(**m) for m in msgs],
                                      has_new_messages=any(not m["seen"] for m in msgs))

    def retrieve_voicemail_message_details(self, session, user, message_id) -> VoicemailMessageDetails:
        m = self._vm_find(user.user_id, message_id)
        data = self._vm_model(m, user.user_id)
        data["sender"] = data["sender"] or "unknown"
        return VoicemailMessageDetails(**data, attachments=[VoicemailMessageAttachment(
            type="audio", subtype="wav", size=data["size"], filename=m["filename"])])

    def retrieve_voicemail_message_attachment(self, session, message_id, file_format) -> tuple:
        if file_format and file_format.lower() != "wav":
            raise_webtrit_error(422, error_message="Only wav is available from NetSapiens",
                                extra_error_code="unsupported_file_format")
        m = self._vm_find(self._id(session.user_id), message_id)
        r = requests.get(m["file-access-url"], stream=True, timeout=30)
        if r.status_code != 200:
            raise_webtrit_error(404, error_message="Voicemail audio not available", extra_error_code="message_not_found")
        return r.headers.get("Content-Type", "audio/wav"), r.iter_content(8192)

    def patch_voicemail_message(self, session, message_id, body) -> UserVoicemailMessagePatch:
        user_id = self._id(session.user_id)
        m = self._vm_find(user_id, message_id)
        uid, _ = self.split_uid(user_id)
        # NetSapiens has one transition: new -> save (heard and kept); it cannot be undone
        if (body.seen or body.saved) and m["_folder"] == "new":
            self._ns("PATCH", user_id, f"/users/{uid}/voicemails/new/{message_id}/save")
            m["_folder"] = "save"
        return UserVoicemailMessagePatch(seen=m["_folder"] != "new", saved=m["_folder"] == "save")

    def delete_voicemail_message(self, session, message_id) -> None:
        user_id = self._id(session.user_id)
        m = self._vm_find(user_id, message_id)
        uid, _ = self.split_uid(user_id)
        r = self._ns("DELETE", user_id, f"/users/{uid}/voicemails/{m['_folder']}/{message_id}")
        if r.status_code >= 400:
            raise_webtrit_error(r.status_code, error_message=f"NetSapiens refused the delete: HTTP {r.status_code}")

    # ------------------------------------------------------------------ mapping
    def netsapiens_to_webtrit_obj(self, ext: dict, produce_user_info=True,
                                  client: Optional[NetsapiensClient] = None):
        """Convert an extension (device + user) returned by the NetSapiens API into
        an EndUser (with SIP credentials) or a ContactInfo."""

        def parse_sip_uri(sip_uri: str):
            """
            Parses a SIP URI into protocol, username, hostname, and optional port
                (default port = 5060).

            Args:
                sip_uri (str): The SIP URI to parse (e.g., "sip:100@engagep2p:5061").

            Returns:
                dict: A dictionary containing protocol, username, hostname, and port.
            """
            pattern = r'(?P<protocol>\w+):(?P<username>[\w]+)@(?P<hostname>[\w.]+)(?::(?P<port>\d+))?'
            match = re.match(pattern, sip_uri)

            if not match:
                raise ValueError(f"Invalid SIP URI format: {sip_uri}")

            components = match.groupdict()
            return {
                "protocol": components["protocol"],
                "username": components["username"],
                "hostname": components["hostname"],
                "port": int(components["port"]) if components["port"] else client.default_sip_port,
            }

        firstname = ext.get("name-first-name", ext.get("name-full-name", ""))
        lastname = ext.get("name-last-name", "")

        data = {
            "company_name": ext.get("domain", ""),
            "first_name": firstname,
            "last_name": lastname,
            # Core maps over sms / additional: null there fails the sign-in
            "numbers": Numbers(
                ext=str(ext.get("user", "")),
                main=str(ext.get("user", "")),
                additional=[],
                sms=[],
            ),
            "balance": Balance(balance_type=BalanceType.inapplicable, )
        }
        display_name = ext.get("name-full-name", f"{lastname}, {firstname}")

        if email := ext.get("email", None):
            data["email"] = email

        if produce_user_info:
            sip_info = parse_sip_uri(ext.get("device-sip-registration-uri", ""))
            core_server = ext.get("core-server")
            outbound_proxy = SIPServer(
                host=core_server if core_server else client.default_sip_outbound_proxy,
                port=client.default_sip_port,
            )
            data["sip"] = SIPInfo(
                username=ext.get("device"),
                display_name=display_name,
                password=ext.get("device-sip-registration-password", ""),
                sip_server=SIPServer(
                    host=sip_info.get("hostname"),
                    port=sip_info.get("port")),
                outbound_proxy_server=outbound_proxy,
                registrar_server=outbound_proxy,
            )
            return EndUser(**data)
        else:
            # without an id every contact looks the same to a client (favorites, name lookup)
            data["user_id"] = ext.get("uid")
            data["sip_status"] = SIPRegistrationStatus.registered \
                if ext.get("device-sip-registration-state", "") == "registered" else SIPRegistrationStatus.notregistered
            try:
                return ContactInfo(**data)
            except ValidationError:
                data.pop("email", None)
                return ContactInfo(**data)

    def map_disconnect_status(self, duration: int, status: str) -> ConnectStatus:
        """Map Netsapiens disconnect status to WebTrit CDR status"""
        if "Bye" in status:
            # disconnect by user
            return ConnectStatus.accepted if duration > 0 else ConnectStatus.missed

        if "Cancel" in status:
            return ConnectStatus.declined

        return ConnectStatus.error

    def netsapiens_to_webtrit_cdr(self, cdr: dict, me: Optional[str] = None) -> CDRInfo:
        """Convert a NetSapiens CDR into a WebTrit one, as seen by the user `me` (extension number).

        - caller / callee are user numbers (call-orig-user, call-orig-to-user), not device names;
        - the direction is relative to `me`: call-direction is not "incoming for this user";
        - the duration is the talk time; an unanswered incoming call is missed."""
        try:
            x = cdr.get("call-start-datetime")
            start = datetime.fromisoformat(x) if x else None
        except (ValueError, TypeError) as e:
            logging.error(f"Invalid datetime format for call-start-datetime: {cdr.get('call-start-datetime')} {e}")
            start = None
        talk = int(cdr.get("call-talking-duration-seconds") or 0)
        total = int(cdr.get("call-total-duration-seconds") or 0)
        end = start + timedelta(seconds=total) if start else None

        caller = str(cdr.get("call-orig-user") or cdr.get("call-orig-from-user") or cdr.get("call-orig-caller-id") or "")
        callee = str(cdr.get("call-orig-to-user") or cdr.get("call-orig-request-user") or "")
        if me is not None:
            incoming = caller != str(me)
        else:
            incoming = cdr.get("call-direction") == 1
        answered = bool(cdr.get("call-answer-datetime")) and talk > 0
        reason = cdr.get("call-disconnect-reason-text") or ""
        if answered:
            status = ConnectStatus.accepted
        elif incoming:
            status = ConnectStatus.missed
        elif "Cancel" in reason:
            status = ConnectStatus.declined
        else:
            status = ConnectStatus.failed

        call_id = cdr.get("call-orig-call-id")
        return CDRInfo(call_id=str(call_id) if call_id is not None else None,
                       direction=Direction.incoming if incoming else Direction.outgoing,
                       caller=caller,
                       callee=callee,
                       connect_time=start,
                       disconnect_time=end,
                       duration=talk,
                       status=status,
                       recording_id=None)
