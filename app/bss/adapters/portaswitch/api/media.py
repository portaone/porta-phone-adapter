from typing import AsyncIterator, Final, Union

import httpx

DEFAULT_CHUNK_SIZE: Final[int] = 8192


class MediaResponseMixin:
    """Decodes PortaBilling answers that may be a file rather than JSON.

    Shared by both realms: the account realm serves the subscriber's own recordings,
    voicemail and transcripts, the admin realm serves recordings and transcripts behind
    a public link (WT-1993).
    """

    async def decode_response(self, response: httpx.Response) -> Union[dict, tuple[str, AsyncIterator[bytes]]]:
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
