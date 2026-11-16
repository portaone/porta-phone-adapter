from enum import Enum
from typing import Optional

from bss.http_api import APIUser


class PortaSwitchAdminUser(APIUser):
    token: Optional[str] = None


class PortaSwitchSignInCredentialsType(Enum):
    SELF_CARE = "self-care"
    SIP = "sip"

    @classmethod
    def _missing_(cls, value):
        return cls.SELF_CARE


class PortaSwitchContactsSelectingMode(Enum):
    ACCOUNTS = "accounts"
    EXTENSIONS = "extensions"
    PHONEBOOK = "phonebook"
    PHONE_DIRECTORY = "phone_directory"

    @classmethod
    def _missing_(cls, value):
        return cls.ACCOUNTS


class PortaSwitchExtensionType(Enum):
    UNASSIGNED = "Unassigned"
    ACCOUNT = "Account"
    GROUP = "Group"

    @classmethod
    def _missing_(cls, value):
        return cls.UNASSIGNED


class PortaSwitchDualVersionSystem(Enum):
    UNSPECIFIED = None
    SOURCE = "source"
    TARGET = "target"

    @classmethod
    def _missing_(cls, value):
        return cls.UNSPECIFIED


class PortaSwitchMailboxMessageFlag(Enum):
    SEEN = "Seen"
    ANSWERED = "Answered"
    FLAGGED = "Flagged"


class PortaSwitchMailboxMessageFlagAction(Enum):
    SET = "set_flag"
    UNSET = "remove_flag"


class PortaSwitchMailboxMessageAttachmentFormat(Enum):
    WAV = "wav"
    MP3 = "mp3"
    AU = "au"

    @classmethod
    def has_value(cls, value):
        return value in cls._value2member_map_


# The fax formats a client may name, each mapped to the MIME types PortaBilling files a
# fax page under (`_MIME_TYPE_FOR_FAX` in Porta/Account/Mailbox.pm). PortaBilling has no
# fax `format` of its own - a fax document is fetched by its `file_name` (WT-2227).
PORTASWITCH_FAX_ATTACHMENT_FORMATS = {
    "pdf": ("application/pdf",),
    "tiff": ("image/tiff",),
    "tif": ("image/tiff",),
    "png": ("image/png", "image/xpng"),
}
PORTASWITCH_FAX_MIME_TYPES = frozenset(m for types in PORTASWITCH_FAX_ATTACHMENT_FORMATS.values() for m in types)
