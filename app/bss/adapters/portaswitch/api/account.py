import logging
from datetime import datetime
from typing import AsyncIterator, Final, List, Optional, Union

import httpx
from jose import jwt

from bss.adapters.portaswitch.config import PortaSwitchSettings
from bss.adapters.portaswitch.exceptions import access_token_expired_error, service_read_only_error
from bss.adapters.portaswitch.failover import READ_ONLY_FAULTS, SESSION_AUTH_FAULTS
from bss.adapters.portaswitch.types import PortaSwitchMailboxMessageFlag, PortaSwitchMailboxMessageFlagAction
from bss.adapters.portaswitch.utils import extract_fault_code
from bss.async_http_api import AsyncHTTPAPIConnector
from report_error import WebTritErrorException

DEFAULT_CHUNK_SIZE: Final[int] = 8192


class AccountAPI(AsyncHTTPAPIConnector):
    """Provides access to Account realm of the PortaSwitch API (async, WT-1720)."""

    def __init__(self, portaswitch_settings: PortaSwitchSettings, site_state=None):
        """The class constructor.

        Parameters:
            :config (app_config.AppConfig): The instance with all the service config options.
            :site_state: Optional shared active-site tracker enabling DR failover.

        """
        super().__init__(
            portaswitch_settings.ACCOUNT_API_URL,
            site_state=site_state,
            standby_server=portaswitch_settings.ACCOUNT_API_URL_STANDBY,
        )

        # TLS verification is applied at shared-client construction (httpx
        # verify is client-level, not per-request); see AsyncHTTPAPIConnector.
        self._verify_https = portaswitch_settings.VERIFY_HTTPS
        if portaswitch_settings.API_TIMEOUT is not None:
            self.DEFAULT_REQUEST_TIMEOUT = portaswitch_settings.API_TIMEOUT
        # httpx connection-pool limits for the shared async client (WT-1720).
        self._max_connections = portaswitch_settings.MAX_CONNECTIONS
        self._max_keepalive_connections = portaswitch_settings.MAX_KEEPALIVE_CONNECTIONS

    async def __send_request(
            self, module: str, method: str, params: dict, stream: bool | None = None, access_token: str | None = None
    ) -> Union[dict, tuple[str, AsyncIterator[bytes]]]:
        """Sends the PortaBilling API method by means of HTTP POST request.

        Parameters:
            :module (str): The module of the Porta-Billing API methods from which the method to be
                called.
            :method (str): The name of the Porta-Billing API method to be called.
            :params (dict): The object with parameters the API method to be called with.

        Returns:
            :response (dict): The API method execution result.

        """

        headers = None
        if access_token:
            headers = {"Authorization": f"Bearer {access_token}"}

        try:
            result = await self.send_rest_request(
                method="POST",
                path=f"/rest/{module}/{method}",
                json={"params": params},
                headers=headers,
                stream=stream,
            )
        except WebTritErrorException as error:
            fault_code = extract_fault_code(error)
            # A read-only (secondary/standalone) site rejects writes/updates with
            # a specific fault; surface a clear domain error instead of a 500.
            if fault_code in READ_ONLY_FAULTS:
                raise service_read_only_error()
            # The site that answered does not accept this session token. After a
            # DR failover that is the expected outcome for a token the app still
            # holds, because PortaSwitch sessions are site-local (WT-1814); the
            # account realm cannot log in again on the subscriber's behalf, so the
            # honest answer is "log in again", not a generic 500.
            #
            # Only Bearer-authenticated calls are mapped: Session/login,
            # /refresh_access_token, /logout and /ping carry their token as a
            # parameter instead, and keep their own, more specific errors.
            if access_token and fault_code in SESSION_AUTH_FAULTS:
                logging.warning(
                    f"PortaSwitch rejected the account session token ({fault_code}) on "
                    f"{module}/{method}; reporting it as an expired access token"
                )
                raise access_token_expired_error()
            raise error

        return result

    async def decode_response(
            self, response: httpx.Response
    ) -> Union[dict, tuple[str, AsyncIterator[bytes]]]:
        """Decode the response.

        Parameters:
            :response (httpx.Response): The response to be decoded.

        Returns:
            Response :dict|tuple: Returns dict with parsed JSON when the response is JSON.
                Returns (content_type, async byte iterator) for attachments; the iterator
                owns the open streamed response and closes it when done.
        """
        content_type = response.headers.get("Content-Type", "")
        if "application/json" in content_type:
            try:
                return response.json()
            except httpx.ResponseNotRead:
                await response.aread()
                return response.json()

        if "attachment" in response.headers.get("Content-Disposition", ""):
            return content_type, self._aiter_and_close(response)

        # A transcription asked for as plain text is a document, not an attachment,
        # and PortaBilling does not always mark it as one (WT-1963). Scoped to that one
        # method: for every other call a text/* body is something gone wrong - an error
        # page from something in between - and must stay the error below, not become a
        # 200 carrying that page as if it were the recording.
        if content_type.startswith("text/") and response.request.url.path.endswith("/CDR/get_transcription"):
            return content_type, self._aiter_and_close(response)

        # Unexpected shape: nothing will read the body, so release the
        # connection explicitly (matters for streamed responses).
        await response.aclose()
        raise ValueError("Not expected response")

    @staticmethod
    async def _aiter_and_close(response: httpx.Response) -> AsyncIterator[bytes]:
        """Stream the response body in chunks, releasing the underlying
        connection when iteration completes, is aborted (client disconnect),
        or errors out mid-stream (e.g. a read timeout)."""
        try:
            async for chunk in response.aiter_bytes(DEFAULT_CHUNK_SIZE):
                yield chunk
        finally:
            await response.aclose()

    async def login(self, login: str, password: str = None, token: str = None) -> dict:
        """Performs an account login by its login, password and/or token.

        Parameters:
            :login (str): The login of the account.
            :password (str): The password of the account. Not required when token is provided.
            :token (str): The api_token of the account.

        Returns:
            :(dict): The API method execution result.

        """
        params = {"login": login}

        if password:
            params["password"] = password

        if token:
            params["token"] = token

        return await self.__send_request(module="Session", method="login", params=params)

    async def logout(self, access_token: str) -> dict:
        """Performs an account logout.

        Parameters:
            :access_token (str): The identifier of the session to be logged.

        Returns:
            :(dict): The API method execution result.

        """
        return await self.__send_request(
            module="Session",
            method="logout",
            params={
                "access_token": access_token,
            },
        )

    async def refresh(self, refresh_token: str) -> dict:
        """Performs an account login by its login and password.

        Parameters:
            :refresh_token (str): The login of the account.

        Returns:
            :(dict): The API method execution result.

        """
        return await self.__send_request(
            module="Session",
            method="refresh_access_token",
            params={
                "refresh_token": refresh_token,
            },
        )

    async def ping(self, access_token: str) -> dict:
        """Checks whether the access_token is valid.

        Parameters:
            :access_token (str): The access_token to be checked.

        Returns:
            :(dict): The API method execution result.

        """
        return await self.__send_request(
            module="Session",
            method="ping",
            params={
                "access_token": access_token,
            },
        )

    async def get_account_info(self, access_token: str) -> dict:
        """Returns the account_info of the account, which created a session related to
        the access_token.

        Parameters:
            :access_token (str): The token that enables the API user to be authenticated
                in the PortaBilling API using the account realm.

        Returns:
            :(dict): The API method execution result that contains an account info.

        """
        return await self.__send_request(
            module="Account",
            method="get_account_info",
            params={
                "detailed_info": 1,  # to acquire the extension_id
                "without_service_features": 1,
                "limit_alias_did_number_list": 100,
            },
            access_token=access_token,
        )

    async def get_alias_list(self, access_token: str) -> dict:
        """Returns the alias list of the account, which created a session related to
        the access_token.

        Parameters:
            :i_account (int): The identifier of the account which aliases to fetch.
            :access_token (str): The token that enables the API user to be authenticated
                in the PortaBilling API using the account realm.

        Returns:
            :(dict): The API method execution result that contains an account info.

        """
        return await self.__send_request(module="Account", method="get_alias_list", params={}, access_token=access_token)

    async def get_xdr_list(
            self, access_token: str, page: int, items_per_page: int, time_from: datetime, time_to: datetime
    ) -> dict:
        """Returns the account_info of the account, which created a session related to
        the access_token.

        Parameters:
            :access_token (str): The token that enables the API user to be authenticated
                in the PortaBilling API using the account realm.
            :page (int): Shows what page of the CDR history to return.
            :items_per_page (int): Shows the number of items to return.
            :time_from (datetime): Filters the time frame of the CDR history.
            :time_to (datetime): Filters the time frame of the CDR history.

        Returns:
            :(dict): The API method execution result that contains an account info.

        """
        return await self.__send_request(
            module="Account",
            method="get_xdr_list",
            params={
                "i_service_type": 3,
                "get_total": 1,
                "show_unsuccessful": 1,
                "with_cr_download_ids": 1,
                "limit": items_per_page,
                "offset": items_per_page * (page - 1),
                "from_date": time_from.strftime("%Y-%m-%d %H:%M:%S"),
                "to_date": time_to.strftime("%Y-%m-%d %H:%M:%S"),
            },
            access_token=access_token,
        )

    async def get_phonebook_list(self, access_token: str, page: int, items_per_page: int,
                                 *, offset: int = None, limit: int = None) -> dict:
        """Return the phonebook list of the account.

        Args:
            access_token (str): Token that authenticates the API user in the PortaBilling API using the account realm.
            page (int): Shows what page of the phonebook to return.
            items_per_page (int): Shows the number of items to return.
            offset (int, optional): Raw offset override. Overrides page-based offset when provided.
            limit (int, optional): Raw limit override. Overrides items_per_page when provided.

        Returns:
            dict: API method execution result containing phonebook items.
        """
        return await self.__send_request(
            module="Account",
            method="get_phonebook_list",
            params={
                "get_total": 1,
                "limit": limit if limit is not None else items_per_page,
                "offset": offset if offset is not None else items_per_page * (page - 1),
            },
            access_token=access_token)

    async def get_phone_directory_list(self, access_token: str, page: int, items_per_page: int) -> dict:
        """Return the phone directory list of the account.

        Args:
            access_token (str): Token that authenticates the API user in the PortaBilling API using the account realm.
            page (int): Shows what page of the phone directory to return.
            items_per_page (int): Shows the number of items to return.

        Returns:
            dict: API method execution result containing phone directory items.
        """
        return await self.__send_request(
            module="UA",
            method="get_phone_directory_list",
            params={
                "get_total": 1,
                "limit": items_per_page,
                "offset": items_per_page * (page - 1),
            },
            access_token=access_token)

    async def get_phone_directory_info(self, access_token: str, i_ua_config_directory: str, page: int,
                                       items_per_page: int) -> dict:
        """Return the phone directory info of the account.

        Args:
            access_token (str): Token that authenticates the API user in the PortaBilling API using the account realm.
            i_ua_config_directory (str): Identifier of the phone directory.
            page (int): Shows what page of the phone directory items to return.
            items_per_page (int): Shows the number of items to return.

        Returns:
            dict: API method execution result containing phone directory items.
        """
        return await self.__send_request(
            module="UA",
            method="get_phone_directory_info",
            params={
                "i_ua_config_directory": i_ua_config_directory,
                "get_total": 1,
                "get_records": "Y",
                "limit": items_per_page,
                "offset": items_per_page * (page - 1),
            },
            access_token=access_token)

    async def get_call_recording(self, recording_id: int, access_token: str) -> tuple[str, AsyncIterator[bytes]]:
        """Returns the bytes of the call recording file.

        Parameters:
            :recording_id (int): The identifier of the call recording.
            :access_token (str): The token that enables the API user to be authenticated
                in the PortaBilling API using the account realm.

        Returns:
            :(dict): The API method execution result that contains an account info.

        """
        return await self.__send_request(
            module="CDR",
            method="get_call_recording",
            params={
                "i_xdr": recording_id,
            },
            stream=True,
            access_token=access_token,
        )

    async def get_call_transcription(
        self,
        call_recording_id: str,
        access_token: str,
        format: Optional[str] = None,
        check_only: bool = False,
    ) -> Union[dict, tuple[str, AsyncIterator[bytes]]]:
        """Returns the transcription of a recorded call.

        Unlike get_call_recording, this method is keyed by the call recording record
        (the xDR's h323_conf_id), not by i_xdr - PortaBilling has no i_xdr variant of
        it in any realm (WT-1963).

        Parameters:
            :call_recording_id (str): The identifier of the call recording record.
            :access_token (str): The token that enables the API user to be authenticated
                in the PortaBilling API using the account realm.
            :format (Optional[str]): "json" for the structured transcription with
                timestamps, "text" for the transcribed text only. PortaBilling defaults
                to "json".
            :check_only (bool): Only report whether a transcription exists, without
                returning it.

        Returns:
            :(dict|tuple): The parsed JSON transcription, or (content_type, async byte
                iterator) when PortaBilling answers with a file (plain text, or a ZIP
                when the call was recorded as several files).

        """
        params = {"call_recording_id": call_recording_id}
        if format:
            params["format"] = format
        if check_only:
            params["check_only"] = 1

        return await self.__send_request(
            module="CDR",
            method="get_transcription",
            params=params,
            stream=True,
            access_token=access_token,
        )

    async def get_mailbox_messages(
        self, access_token: str, from_date: Optional[str] = None, to_date: Optional[str] = None
    ) -> List[dict]:
        """
        Returns the mailbox of the account, which created a session related to the access_token.
            Parameters:
                access_token :str: The token that enables the API user to be authenticated in the PortaBilling API using the account realm.
                from_date :Optional[str]: The date in the "YYYY-MM-DD" format. Messages delivered on or after this date are returned.
                to_date :Optional[str]: The date in the "YYYY-MM-DD" format. Messages delivered before this date are returned.

            Returns:
                Response :dict: The API method execution result that contains a list of mailbox messages.
        """

        params = {}
        if from_date is not None:
            params["from_date"] = from_date
        if to_date is not None:
            params["to_date"] = to_date

        return (await self.__send_request(
            module="Account",
            method="get_mailbox_message_list",
            params=params,
            access_token=access_token,
        ))["messages"]

    async def get_mailbox_message_details(self, access_token: str, message_id: str) -> dict:
        """
        Returns the mailbox message details of the account, which created a session related to the access_token.
            Parameters:
                access_token :str: The token that enables the API user to be authenticated in the PortaBilling API using the account realm.
                message_id :str: The unique ID of the message.

            Returns:
                Response :dict: The API method execution result that contains a details of mailbox message.
        """

        return await self.__send_request(
            module="Account",
            method="get_mailbox_message_details",
            params={
                "message_uid": message_id,
            },
            access_token=access_token,
        )

    async def get_mailbox_message_attachment(self, access_token: str, message_id: str, file_format: str) -> tuple[
        str, AsyncIterator[bytes]]:
        """
        Returns the mailbox message attachment of the account, which created a session related to the access_token.
            Parameters:
                access_token :str: The token that enables the API user to be authenticated in the PortaBilling API using the account realm.
                message_id :str: The unique ID of the message.
                file_format :str: Provided file format.

            Returns:
                Response :bytes: The API method execution result that contains a raw bytes of a mailbox message attachment.
        """

        return await self.__send_request(
            module="Account",
            method="get_mailbox_message_attachment",
            params={"message_uid": message_id, "format": file_format},
            stream=True,
            access_token=access_token,
        )

    async def set_mailbox_message_flag(
            self,
            access_token: str,
            message_id: str,
            flag: PortaSwitchMailboxMessageFlag,
            action: PortaSwitchMailboxMessageFlagAction,
    ) -> dict:
        """
        Returns the mailbox message details of the account, which created a session related to the access_token.
            Parameters:
                access_token :str: The token that enables the API user to be authenticated in the PortaBilling API using the account realm.
                message_id :str: The unique ID of the message.
                flag: :PortaSwitchMailboxMessageFlag: The flag to set.
                action: :PortaSwitchMailboxMessageFlagAction: Set the flag if it has value `set_flag`, remove the flag otherwise.

            Returns:
                Response :dict: The API method execution result.
        """

        return await self.__send_request(
            module="Account",
            method="set_mailbox_messages_flag",
            params={"action": action.value, "flag": flag.value, "message_uids": [message_id]},
            access_token=access_token,
        )

    async def delete_mailbox_message(self, access_token: str, message_id: str) -> None:
        """
        Deletes the mailbox message of the account, which created a session related to the access_token.
            Parameters:
                access_token :str: The token that enables the API user to be authenticated in the PortaBilling API using the account realm.
                message_id :str: The unique ID of the message.
        """

        return await self.__send_request(
            module="Account",
            method="delete_mailbox_messages",
            params={"message_uids": [message_id]},
            access_token=access_token,
        )

    @classmethod
    def decode_and_verify_access_token_expiration(cls, access_token: str) -> dict:
        return jwt.decode(access_token, '', options={
            "verify_signature": False,
            'verify_aud': False,
            'verify_iat': False,
            'verify_exp': True,
            'verify_nbf': False,
            'verify_iss': False,
            'verify_sub': False,
            'verify_jti': False,
            'verify_at_hash': False,
        })
