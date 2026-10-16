from bss.adapters import BSSAdapter
from bss.types import (Capabilities, UserInfo, EndUser, Contacts, ContactInfo,
                       Calls, CDRInfo, ConnectStatus, SessionInfo, SIPRegistrationStatus,
                       Balance, BalanceType, Numbers, SIPServer, SIPInfo,
                       OTPCreateResponse, OTPVerifyRequest, )

from bss.dbs import TiedKeyValue
from bss.sessions import configure_session_storage
from report_error import raise_webtrit_error
from app_config import AppConfig
from bss.http_api import HTTPAPIConnectorWithLogin, OAuthSessionData, APIUser
from request_trace import sanitize_data
from typing import Union, List, Dict, Optional
import hmac
import logging

import re

from pydantic import TypeAdapter, ValidationError, EmailStr

VERSION = "0.1.0"

class FreePBXAPI(HTTPAPIConnectorWithLogin):
    def __init__(self, api_server: str, api_user: str, api_password: str, **kwargs):
        super().__init__(api_server, api_user, api_password)
        if "graphql_path" in kwargs:
            self.graphql_path = kwargs["graphql_path"]
        else:
            self.graphql_path = "/admin/api/api/gql"


    def access_token_path(self) -> str:
        return "/admin/api/api/token"
    
    def login(self, user: APIUser = None) -> OAuthSessionData:
        """Obtain an access token with the client credentials of the M2M app"""
        return self._request_token({
            "client_id": self.api_user,
            "client_secret": self.api_password,
            "scope": "",
            "grant_type": "client_credentials",
        })

    def refresh(self, user: APIUser, auth_session: OAuthSessionData) -> OAuthSessionData:
        """Exchange the refresh token for a new access token"""
        return self._request_token({
            "client_id": self.api_user,
            "client_secret": self.api_password,
            "scope": "",
            "refresh_token": auth_session.refresh_token,
            "grant_type": "refresh_token",
        })

    def _request_token(self, data: dict) -> OAuthSessionData:
        res = self.send_rest_request(
            "POST",
            self.access_token_path(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=data,
            turn_off_login=True,
        )
        if res and (session := self.extract_access_token(res)).access_token:
            # session_in_progress does not store what login/refresh return,
            # so without this an expiring token would be refreshed on every call
            return self.store_auth_session(session)

        logging.debug(f"Could not find an access token in the response {sanitize_data(res)}")
        raise ValueError("Could not find an access token in the response")

    # GraphQL query to get a specific extension
    query_ext = """{ 
            fetchExtension(extensionId: <extid>) {
                status message id extensionId
                user {
                    password extPassword name sipname
                }
            }
        }"""
    # GraphQL query to get a specific extension's voicemail data
    query_voicemail = """{
            fetchVoiceMail (extensionId: <extid>) {
                status message name password email
            }
        }"""

    def get_extension(self, user_id: str) -> Dict:
        """Get the extension info"""

        if not user_id.isdigit():
            # ext ID has to be numeric
            return None
        
        query = self.query_ext.replace("<extid>", user_id)
        user = self.send_rest_request("POST", self.graphql_path, json={"query": query})
        # GraphQL answers an unknown extension (or any error) with a null
        # "data" or a null "fetchExtension" rather than an HTTP error
        user_data = ((user or {}).get("data") or {}).get("fetchExtension") or {}
        if not user_data.get("extensionId"):
            return None

        # the extension data does not contain the email, so we have to
        # retrieve it from the voicemail data - which is null when the
        # extension has no voicemail
        query = self.query_voicemail.replace("<extid>", user_id)
        vm = self.send_rest_request("POST", self.graphql_path, json={"query": query})
        user_data["vm"] = ((vm or {}).get("data") or {}).get("fetchVoiceMail") or {}

        return user_data
    
    # GraphQL query to get all extensions
    query_all_extensions = """{
            fetchAllExtensions {
                status
                message
                totalCount
                extension {
                    id
                    extensionId
                    user {
                        name password outboundCid ringtimer sipname password extPassword
                    }
                    coreDevice {
                        deviceId dial devicetype description emergencyCid
                    }
                }
            }
        }"""

    def get_all_extensions(self) -> List[Dict]:
        """Get all extensions defined in the PBX"""
        reply = self.send_rest_request(
            "POST", self.graphql_path, json={"query": self.query_all_extensions}
        )
        all_extensions = ((reply or {}).get("data") or {}).get("fetchAllExtensions") or {}
        return all_extensions.get("extension") or []
    


class FreePBXAdapter(BSSAdapter):
    """Connect WebTrit and FreePBX. Authenticate a user using his/her
    data in FreePBX, retrieve user's SIP credentials to be used by
    WebTrit and return a list of other configured extenstions (to
    be provided as 'Cloud PBX' contacts).
    Currently does not support OTP login, CDRs and call recording retrieval."""

    CAPABILITIES = [
        # log in user with extension number / SIP password
        Capabilities.passwordSignin,
        # obtain the list of other extensions in the PBX
        Capabilities.extensions,
        # features of WebTrit Core itself, which FreePBX does not take part in
        Capabilities.internal_messaging,
        Capabilities.direct_presence,
        Capabilities.conversation_mute,
        # Asterisk serves presence and dialog-info subscriptions
        Capabilities.sip_presence,
        Capabilities.sip_dialogs,
    ]

    def __init__(self, config: AppConfig):
        super().__init__(config)
        api_server = config.get_conf_val(
            "FreePBX", "API_Server", default="http://127.0.0.1"
        )
        api_user = config.get_conf_val("FreePBX", "API_User")
        api_password = config.get_conf_val("FreePBX", "API_Secret")
        self.sip_server = config.get_conf_val(
            "FreePBX", "SIP_Server", default="167.172.185.158"
        )

        self.api_client = FreePBXAPI(
            api_server=api_server, api_user=api_user, api_password=api_password
        )
        self.sessions = configure_session_storage(config)
        # for debugging only
        self.otp_db = TiedKeyValue()

    @classmethod
    def name(cls) -> str:
        return "FreePBX adapter"

    @classmethod
    def version(cls) -> str:
        global VERSION
        return VERSION

    def generate_otp(self, user: UserInfo) -> OTPCreateResponse:
        """Not supported"""
        pass

    def validate_otp(self, otp: OTPVerifyRequest) -> SessionInfo:
        """Not supported"""
        pass

    def extract_user_id(self, user_data: object) -> str:
        """Extract user_id (unique and unmutable identifier of the user)
        from the data in the DB. Please override it in your sub-class"""
        # for production systems it is more appropriate to use 'id'
        # which is a permanent identifier of the user; but for testing
        # extension number is more convenient
        return user_data.get("extensionId", None)

    def authenticate(self, user: UserInfo, password: str = None) -> SessionInfo:
        """Authenticate the user with the extension number and its SIP password."""

        user_data = self.api_client.get_extension(user.login)
        if not user_data:
            raise_webtrit_error(401, error_message="User authentication error",
                                extra_error_code="incorrect_credentials")

        # the SIP secret, not the voicemail PIN: an extension without
        # voicemail has no PIN at all
        sip_password = (user_data.get("user") or {}).get("extPassword")
        if not password or not sip_password or not hmac.compare_digest(sip_password, password):
            raise_webtrit_error(401, error_message="Invalid password",
                                extra_error_code="incorrect_credentials")

        # everything is in order, create a session
        user.user_id = self.extract_user_id(user_data)
        session = self.sessions.create_session(user)
        self.sessions.store_session(session)
        return session

    def retrieve_user(self, session: SessionInfo, user: UserInfo) -> EndUser:
        """Obtain user's information - most importantly, his/her SIP credentials."""

        user = self.api_client.get_extension(user.user_id)
        if user:
            return self.freepbx_to_webtrit_obj(user, produce_user_info=True)

        # no such session
        raise_webtrit_error(404, error_message="User not found",
                            extra_error_code="user_not_found")

    def retrieve_contacts(self, session: SessionInfo, user: UserInfo) -> List[ContactInfo]:
        """List of other extensions in the PBX"""

        ext_list = self.api_client.get_all_extensions()

        contacts = [
            self.freepbx_to_webtrit_obj(x, produce_user_info=False)
            for x in ext_list
            if x.get("extensionId", "") != user.user_id
        ]

        return contacts

    def retrieve_contacts_v2(
            self, session: SessionInfo,
            user: UserInfo,
            search: Optional[str] = None,
            phone_numbers: List[str] = [],
            page: Optional[int] = 1,
            items_per_page: Optional[int] = 100,
    ) -> tuple[List[ContactInfo], int]:
        """The same extensions as retrieve_contacts, filtered and paginated here:
        the GraphQL API has neither a search nor paging for fetchAllExtensions"""

        contacts = self.retrieve_contacts(session, user)

        wanted_numbers = {n.replace("+", "").strip() for n in phone_numbers if n}
        if wanted_numbers:
            # a phone number lookup ignores the generic search
            contacts = [c for c in contacts
                        if {c.numbers.ext, c.numbers.main} & wanted_numbers]
        elif search and (needle := search.strip().lower()):
            contacts = [c for c in contacts
                        if any(needle in (value or "").lower()
                               for value in (c.first_name, c.last_name, c.alias_name,
                                             c.email, c.numbers.ext, c.numbers.main))]

        page = page or 1
        items_per_page = items_per_page or 100
        start_index = (page - 1) * items_per_page
        return contacts[start_index:start_index + items_per_page], len(contacts)

    def retrieve_contact_by_user_id(self, session: SessionInfo, user: UserInfo, user_id: str) -> ContactInfo:
        """A single extension of the PBX"""

        for ext in self.api_client.get_all_extensions():
            if ext.get("extensionId") == user_id:
                return self.freepbx_to_webtrit_obj(ext, produce_user_info=False)

        raise_webtrit_error(404, error_message=f"There is no extension with such id: {user_id}",
                            extra_error_code="contact_not_found")

    def retrieve_calls(self, session: SessionInfo, user: UserInfo, **kwargs) -> List[CDRInfo]:
        pass

    # call recording is not supported in this example
    def retrieve_call_recording(
        self, session: SessionInfo, call_recording: str
    ) -> bytes:
        """Get the media file for a previously recorded call."""
        # not yet implemented
        pass

    def freepbx_to_webtrit_obj(self, ext: dict, produce_user_info=True) -> dict:
        """Convert the JSON data returned by FreePBX API into an dictionary
        that can be used to crate a WebTrit object (either EndUser or ContactInfo)):

            Args:
                * ext (dict): A dictionary (representing JSON structure,
                    returned by the FreePBX API), which contains the
                    extension's information.
                * produce_user_info (bool, optional): A flag to indicate
                    whether to generate data for EndUser (info about a
                    specific extension, includes SIP credentials, etc.) 
                    or ContactInfo (basic info about other extensions in PBX)

            Returns:
                dict: data to be passed to object's constructor
        """
        # FreePBX sends null for an unset field, so .get() defaults do not apply
        extension_id = ext.get("extensionId") or ""
        ext_info = ext.get("user") or {}
        parts = (ext_info.get("name") or "").split()
        firstname = parts[0] if parts else ""
        lastname = " ".join(parts[1:])

        outbound_id = ext_info.get("outboundCid") or f"<{extension_id}>"
        match = re.search(r"<(\d+)>", outbound_id)
        if match:
            main_number = match.group(1)
        else:
            main_number = extension_id
        data = {
            "company_name": "FreePBX",
            "first_name": firstname,
            "last_name": lastname,
            "email": self.voicemail_email(ext.get("vm") or {}),
            "numbers": Numbers(
                ext=extension_id,
                main=main_number,
                additional=[],
                # Core cannot handle "sms": null
                sms=[],
            ),
            "balance": Balance( balance_type=BalanceType.inapplicable, )
        }
        if firstname or lastname:
            display_name = lastname if lastname else ""
            if display_name:
                display_name = display_name + ", "
            display_name = display_name + firstname 
        else:
            display_name = "Ext " + (extension_id or "???")

        if produce_user_info:
            data["sip"] = SIPInfo(
                username=extension_id,
                display_name=display_name,
                password=ext_info.get("extPassword") or "",
                sip_server=SIPServer(host=self.sip_server, port=5060),
            )
            return EndUser(**data)
        else:
            # lets Core match the contact to the WebTrit user it belongs to
            data["user_id"] = extension_id
            # TODO: find out whether we can get the real
            # registration status via API - for now all extensions
            # are assumed to be registered
            data["sip_status"] = SIPRegistrationStatus.registered

            return ContactInfo(**data)

    @staticmethod
    def voicemail_email(vm: dict) -> Optional[str]:
        """The first address of the voicemail e-mail field, which may hold several
        (separated by "," or "|") or none; a value that is not a valid address is
        left out rather than failing the whole user info"""
        email = re.split(r"[,|]", vm.get("email") or "")[0].strip()
        try:
            return TypeAdapter(EmailStr).validate_python(email) if email else None
        except ValidationError:
            logging.debug(f"Ignoring voicemail e-mail that is not a valid address: {email!r}")
            return None

    def signup(self, user_data, tenant_id: str = None):
        """Create a new user as a part of the sign-up process - not supported yet"""
        pass
