# An adapter between WebTrit and external VoIP system or PBX
## Overview
This is an application that serves as a mapper of API requests
from WebTrit cloud back-end to a 3rd-party system (e.g. hosted PBX)
to retrieve the data about users, so they can use WebTrit's
mobile or web dialer.

The idea is to expand it with additional modules for conecting to
specific types of systems.

More details about how (and why) WebTrit connects to external VoIP
or BSS systems in this [blog article](https://webtrit.com/insights/webrtc-softphone-third-party-voip-switches-cloud-pbx-systems/)

## What's included
* general FastAPI application that processes API requests from WebTrit
* bss/connectors/example.py module which mimics the functionality of
connecting to a real system. User info is stored in the source code and 
info about other extensions or previously made calls is generated randomly.
* set of tests (in tests/ folder) which you can use to test your own 
adapter once it is ready
* Dockerfile for packaging

## Usage
### With an "example" module
* cd app
* docker build -t xyz .
* make your container running on a public IP address (I assume 1.2.3.4)
* Verify that things are working on by
pytest --server http://1.2.3.4 tests
* Apply http://1.2.3.4 in the configuration of your WebTrit instance, so it
sends requests to your API

### Creating your own adapter
* Create your own module xyz in bss/adapters/ folder (use example.py as a template) and define a class (inherited from BSSAdapter) called XYZAdapter
* set BSS_ADAPTER_MODULE environment variable to bss.connectors.xyz
* set BSS_ADAPTER_CLASS environment variable to the name XYZAdapter
* set additional variables as needed (e.g. path to the REST API of your VoIP system)
* start the app ```
cd app
uvicorn main:app --port 8000
```
* test it: ```
pip install pytest-lazy-fixture
pytest --server http://<your-server-ip-and-port> --user user1 --password xyz tests
```

## Capability switches

`GET /system-info` reports a `supported` list, and WebTrit Core passes it to the client
apps so they can show or hide a control. Two things decide what is in it: the
`CAPABILITIES` list on the adapter class (what the adapter has code for) and a
per-deployment env variable for each entry (whether this installation offers it).

The env variable name is `CAPABILITIES_<OPTION>` — prefixed with `<APP_NAME>_` when an
`APP_NAME` is set — where the options and their defaults are
`CONFIG_CAPABILITIES_OPTIONS` in `app/bss/adapters/__init__.py`. Values are read as
booleans (`1`/`true`/`yes`/`y`). A capability the adapter class does not list cannot be
switched on.

Voicemail (WT-1878):

| Variable | Default | Purpose |
|---|---|---|
| `CAPABILITIES_VOICEMAIL` | `false` | The voicemail screen at all. Every voicemail functionality below is dropped when this is off |
| `CAPABILITIES_VOICEMAIL_FORWARD` | `true` | Passing a message on to another user. Implemented and stored by Core — the mailbox has no forward API |

`voicemailSave` and `voicemailTrash` have no switch of their own: whenever the
voicemail screen is on, both are advertised. Save is backed by the IMAP `\Flagged`
flag on the PortaSwitch mailbox; trash means a `DELETE` moves the message to a trash
it can be restored from, and is Core's own.

`voicemailTrash` and `voicemailForward` describe Core behaviour, not PortaSwitch
behaviour; they are advertised here only so a client can tell a Core that speaks them
from one that does not.

## Call history date range (PortaSwitch)

`GET /user/history` takes optional `time_from` / `time_to` query parameters and forwards
them to PortaBilling's `Account/get_xdr_list` as `from_date` / `to_date`. That method
requires both bounds, so a range is always sent — the only question is what it is when the
client did not ask for one.

| Variable | Default | Purpose |
|---|---|---|
| `PORTASWITCH_CALL_HISTORY_DEFAULT_WINDOW_HOURS` | `24` | How far back to look when the client sends no `time_from`. `0` restores the previous `1970-01-01` → `9000-01-01` pair exactly. A value above a century is clamped as a sanity bound — such a window already reaches past the `1970-01-01` floor |

Only the bounds the client omitted are filled in:

- neither given — the last `…_WINDOW_HOURS` up to now;
- `time_from` only — from there up to now, however old `time_from` is;
- `time_to` only — the window measured back from `time_to`;
- both given — passed through untouched.

**The window is a default, not a cap.** A client that asks for a range gets that range,
however wide, so older records stay reachable — it just has to ask, which is how
PortaBilling's own admin and self-care portals behave (they default to the last 24 hours
and let the user widen it). The one bound worth noting is `time_to` on its own: it no
longer means "everything up to then" but "the window ending then", so a client that wants
the older history has to send `time_from` as well.

Both bounds are sent as naive UTC, which is what the PortaBilling API expects — its
reference states that datetime attributes are received in UTC unless a method says
otherwise, and `get_xdr_list` does not. An account's `time_zone_name` affects how its
self-care interface displays a time, not how the API reads one.

Why this matters (WT-1932): PortaBilling partitions `CDR_ACCOUNTS` by week on `bill_time`,
the field `from_date` / `to_date` filter. At the reporting installation each non-empty
partition held ~9-10M rows across ~30 partitions, so the old `1970-01-01` → `9000-01-01`
default made the switch process every partition to return 40 rows — twice, because
`get_total => 1` counts the same range — and that repeatedly took its web services down.

Note that `from_date` / `to_date` filter on `bill_time` while the response reports
`connect_time`, so calls right at a window edge can fall on the other side of it than the
displayed timestamp suggests.
