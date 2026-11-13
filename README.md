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

Presence (WT-1834):

| Variable | Default | Purpose |
|---|---|---|
| `CAPABILITIES_DIRECT_PRESENCE` | `true` | Presence exchanged between WebTrit apps over Core's own PubSub. Never touches SIP or PortaSwitch |
| `CAPABILITIES_SIP_PRESENCE` | `true` | Presence over SIP PUBLISH/SUBSCRIBE, from the user's subscription book |
| `CAPABILITIES_SIP_DIALOGS` | `true` | Dialog-info subscriptions: Call Pull and contact BLF |
| `CAPABILITIES_SIP_MWI` | `true` | `message-summary` subscription to the user's own voicemail box, which the new voicemail push and list refresh run on. Needed when PortaSwitch's `MediaServer.mwi_backward_compatibility` is off (the default since mr113); dropped with `CAPABILITIES_VOICEMAIL` off |

The four are independent. `directPresence` describes Core behaviour rather
than PortaSwitch behaviour, but unlike `conference` it is not advertising: Core requires
the entry, so switching it off stops a controller publishing its own status and reading
anyone else's. Leave it on unless a deployment is large enough that app-to-app presence
is not worth its cost.

Voicemail (WT-1878):

| Variable | Default | Purpose |
|---|---|---|
| `CAPABILITIES_VOICEMAIL` | `false` | The voicemail screen at all. Every voicemail functionality below is dropped when this is off |
| `CAPABILITIES_VOICEMAIL_SAVE` | `true` | Marking a message as saved, independent of whether it was heard. Backed by the IMAP `\Flagged` flag on the PortaSwitch mailbox |
| `CAPABILITIES_VOICEMAIL_TRASH` | `true` | Delete moves a message to a trash it can be restored from. Implemented and stored by Core. Off, clients delete immediately and permanently |
| `CAPABILITIES_VOICEMAIL_FORWARD` | `true` | Passing a message on to another user. Implemented and stored by Core — the mailbox has no forward API |

`voicemailTrash` and `voicemailForward` describe Core behaviour, not PortaSwitch
behaviour; they are advertised here only so a client can tell a Core that speaks them
from one that does not.

Call recordings (WT-1963):

| Variable | Default | Purpose |
|---|---|---|
| `CAPABILITIES_RECORDINGS` | `false` | Downloading the recording of a call |
| `CAPABILITIES_TRANSCRIPTION` | `false` | The speech-to-text transcript of that recording |

`transcription` depends on `recordings`: the transcript is keyed by an id that only a
call history row carrying a recording gives out, so switching recordings off drops it
too. It is off by default because transcription is a PortaSwitch service feature the
deployment subscribes to and is charged for — turning it on where it is not configured
makes clients offer a control that answers 404.

With `recordings` off, `GET /user/history` returns `recording_id: null` for every call
(WT-2048): the recording route answers 501 then, so an id would only make clients offer
a player that cannot play.

Other switches:

| Variable | Default | Purpose |
|---|---|---|
| `CAPABILITIES_SIGNUP` | `false` | Self sign-up of new users (`POST /user`) |
| `CAPABILITIES_PASSWORD` | `true` | Sign-in with login and password (`POST /session`) |
| `CAPABILITIES_OTP` | `false` | Sign-in with a one-time password (`POST /session/otp-create`, `/session/otp-verify`) |
| `CAPABILITIES_AUTO_PROVISION` | `false` | Sign-in with a config token (`POST /session/auto-provision`). Not coded by the PortaSwitch adapter |
| `CAPABILITIES_CDRS` | `false` | Call history (`GET /user/history`). Off, the route answers an empty list |
| `CAPABILITIES_EXTENSIONS` | `true` | The contact list (`GET /user/contacts`). Off, the route answers an empty list |
| `CAPABILITIES_CUSTOM_METHODS` | `false` | `POST /custom/public/…` and `/custom/private/…` — for PortaSwitch, `custom-pages` (the self-config portal) and `external-page-access-token` |
| `CAPABILITIES_INTERNAL_MESSAGING` | `true` | Chat between WebTrit users. A Core feature; the adapter has no route behind it |
| `CAPABILITIES_SMS_MESSAGING` | `false` | SMS conversations. A Core feature; the adapter has no route behind it |
| `CAPABILITIES_USER_EVENTS` | `false` | `POST /user/events`. Not coded by the PortaSwitch adapter |
| `CAPABILITIES_NOTIFICATIONS` | `false` | The in-app notification list. A Core feature; the adapter has no route behind it |
| `CAPABILITIES_NOTIFICATIONS_PUSH` | `false` | Core sends a push notification for a new notification or sign-in only while this is on |
| `CAPABILITIES_CONFERENCE` | `false` | Merging calls into a conference. Happens entirely in Core and Janus — advertising only (WT-783) |
| `CAPABILITIES_CONVERSATION_MUTE` | `true` | Muting one chat or SMS conversation. Stored and enforced by Core — advertising only (WT-1880) |
| `CAPABILITIES_CALL_CENTER` | `false` | The "My Queues" screen (`/user/queues`, WT-1881) |
| `CAPABILITIES_SESSION_AUTO_TERMINATE` | `true` | Lets users choose after how many idle days Core ends their sessions. Stored and enforced by Core, which refuses the setting while this is off (WT-1789) |

## PortaSwitch adapter settings

The PortaSwitch adapter (`BSS_ADAPTER_MODULE=bss.adapters.portaswitch`,
`BSS_ADAPTER_CLASS=PortaSwitchAdapter`) reads its settings from env variables through
pydantic (`app/bss/adapters/portaswitch/config.py`). Names are used exactly as
listed and are case-insensitive. Booleans are read by pydantic
(`true`/`false`, `1`/`0`, `yes`/`no`, `on`/`off`), and lists are separated by `;`. The four
variables without a default are mandatory — the adapter does not start without them.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `PORTASWITCH_ADMIN_API_URL` | yes | — | Base URL of the PortaBilling admin realm API, e.g. `https://pbx.example.com` (`/rest/…` is appended) |
| `PORTASWITCH_ADMIN_API_LOGIN` | yes | — | Login of the PortaBilling admin user the adapter acts as |
| `PORTASWITCH_ADMIN_API_TOKEN` | yes | — | API token of that admin user. Also the key that encrypts the admin session stored with a pending OTP |
| `PORTASWITCH_ACCOUNT_API_URL` | yes | — | Base URL of the PortaBilling account realm API |
| `PORTASWITCH_SIP_SERVER_HOST` | no | `127.0.0.1` | SIP server host handed to the apps in the user's SIP settings — set it for any real deployment |
| `PORTASWITCH_SIP_SERVER_PORT` | no | `5060` | SIP server port handed to the apps |
| `PORTASWITCH_VERIFY_HTTPS` | no | `true` | Whether to verify the PortaBilling API's HTTPS certificate |
| `PORTASWITCH_API_TIMEOUT` | no | `25` | Timeout in seconds for each PortaBilling API request, applied to connect, read, write and pool alike. Blank, non-numeric or non-positive falls back to 5 s connect / 25 s read (WT-1717) |
| `PORTASWITCH_MAX_CONNECTIONS` | no | `100` | See [Outbound connection pool](#outbound-connection-pool-portaswitch) |
| `PORTASWITCH_MAX_KEEPALIVE_CONNECTIONS` | no | unset | See [Outbound connection pool](#outbound-connection-pool-portaswitch) |
| `PORTASWITCH_ADMIN_API_URL_STANDBY` | no | unset | Admin API URL of the disaster-recovery standby site. Failover is on only when both standby URLs are set; with one of them set it stays off and a warning is logged (WT-1654) |
| `PORTASWITCH_ACCOUNT_API_URL_STANDBY` | no | unset | Account API URL of the standby site |
| `PORTASWITCH_VERIFY_HTTPS_STANDBY` | no | unset — follows `PORTASWITCH_VERIFY_HTTPS` | See [Standby site certificate verification](#standby-site-certificate-verification-portaswitch) |
| `PORTASWITCH_SITE_RECHECK_INTERVAL` | no | `60` | Minimum seconds between probes of the main site's `operating_mode` while running on the standby; a probe starts only when a request arrives. Used only while the sites do not report `operating_mode` |
| `PORTASWITCH_SITE_SWITCH_BACK_THRESHOLD` | no | `2` | Consecutive `normal` probes (or monitor ticks, when the sites report `operating_mode`) needed to switch back to the main site (at least 1) |
| `PORTASWITCH_SITE_POLL_INTERVAL` | no | `10` | Seconds between site monitor ticks when the sites report `operating_mode` (BA-47641). Ticks are aligned to the wall clock, so every replica polls both sites at the same instant; requests then go to the active site only |
| `PORTASWITCH_SITE_MAIN_DOWN_THRESHOLD` | no | `3` | Consecutive monitor ticks on which the main site does not answer (or reports `secondary`) while the standby answers, before traffic moves to the standby. A standby that reports `standalone` moves traffic at once |
| `PORTASWITCH_SIGNIN_CREDENTIALS` | no | `self-care` | Which account fields password sign-in checks: `self-care` — `login` and `password`; `sip` — `id` and `h323_password`. An unknown value means `self-care` |
| `PORTASWITCH_CONTACTS_SELECTING` | no | `accounts` | Source of the contact list: `accounts` (the customer's accounts), `extensions` (its extensions), `phonebook` (the user's phonebook) or `phone_directory` (the phone directories). An unknown value means `accounts` |
| `PORTASWITCH_CONTACTS_SELECTING_EXTENSION_TYPES` | no | all — `Unassigned;Account;Group` | `extensions` mode only: which extension types are listed. An unknown name is read as `Unassigned` |
| `PORTASWITCH_CONTACTS_SELECTING_CUSTOMER_IDS` | no | empty | `phonebook` and `phone_directory` modes: `i_customer`s whose account lists are scanned to match entries to accounts. Empty, each number is looked up on its own |
| `PORTASWITCH_CONTACTS_SKIP_WITHOUT_EXTENSION` | no | `false` | `accounts` mode only: leave out accounts that have no extension |
| `PORTASWITCH_CONTACTS_CACHE_TTL` | no | `0` — no cache | Seconds to reuse a customer's account list for contacts before reading it again. Pick a value well above one full read (WT-1922) |
| `PORTASWITCH_CONTACTS_CUSTOM` | no | empty | Extra contacts appended in every mode: JSON objects separated by `;`, each `{"name": "…", "number": "…"}` |
| `PORTASWITCH_CALL_HISTORY_DEFAULT_WINDOW_HOURS` | no | `24` | See [Call history date range](#call-history-date-range-portaswitch) |
| `PORTASWITCH_HIDE_BALANCE_IN_USER_INFO` | no | `false` | Leave the balance out of the user info |
| `PORTASWITCH_SELF_CONFIG_PORTAL_URL` | no | unset | When set, `POST /custom/private/custom-pages` offers a "Self-config Portal" page at this URL with `?token=<access token>` appended. Needs `CAPABILITIES_CUSTOM_METHODS` |
| `PORTASWITCH_ALLOWED_ADDONS` | no | empty — no restriction | See [Add-on restricted sign-in](#add-on-restricted-sign-in-portaswitch) |
| `PORTASWITCH_DEFAULT_ACCOUNT_MAX_DEVICES` | no | unset — no limit | See [Device and session limits](#device-and-session-limits-portaswitch) |
| `PORTASWITCH_DEFAULT_ACCOUNT_MAX_DEVICE_SWITCHES` | no | unset — no limit | See [Device and session limits](#device-and-session-limits-portaswitch) |
| `PORTASWITCH_DEVICE_LIMITS_CACHE_TTL` | no | `60` | See [Device and session limits](#device-and-session-limits-portaswitch) |
| `PORTASWITCH_CRM_HOST_CUSTOM_FIELD` | no | empty — not read | See [CRM of the company](#crm-of-the-company-portaswitch) |
| `OTP_IGNORE_ACCOUNTS` | no | empty | Sign-in identifiers whose OTP is accepted even when PortaBilling rejects the code — test and review accounts only |
| `OTP_STORAGE_COLLECTION` | no | unset — process memory | Firestore collection that keeps pending OTPs. Unset, they live in the process's memory, which breaks OTP sign-in when more than one instance serves requests |
| `OTP_STORAGE_TTL_MINUTES` | no | `30` | Firestore only: the `expires_at` written on each pending OTP. Cleanup needs a Firestore TTL policy on that field |
| `JANUS_SIP_FORCE_TCP` | no | `false` | Hand the apps TCP instead of UDP as the SIP transport |
| `ENABLE_ON_DEMAND_SESSION_MIGRATION` | no | `false` | Migrating sessions of an older adapter: a non-JWT access token answers "session upgrade needed" instead of "invalid", and a refresh token made only of digits is looked up in the hash table and, if absent, taken as an `i_account` itself; either way it is exchanged for a new session of that account without a password. Builds a table of a million hashes at start-up |

## FreePBX adapter settings

The FreePBX adapter (`BSS_ADAPTER_MODULE=bss.adapters.freepbx`,
`BSS_ADAPTER_CLASS=FreePBXAdapter`) talks to the FreePBX GraphQL API as a
Machine-to-Machine application (Connectivity → API → Add Application).

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `FREEPBX_API_SERVER` | no | `http://127.0.0.1` | Base URL of FreePBX; `/admin/api/api/token` and `/admin/api/api/gql` are appended |
| `FREEPBX_API_USER` | yes | — | Client ID of the M2M application |
| `FREEPBX_API_SECRET` | yes | — | Client Secret of the M2M application |
| `FREEPBX_SIP_SERVER` | no | `167.172.185.158` | SIP server host handed to the apps (port 5060) — set it for any real deployment |

Users sign in with the extension number and its SIP secret (the extension's
`secret`), not the voicemail PIN, so an extension without voicemail can sign in too
(WT-1931). The user's e-mail is the voicemail e-mail, absent when there is none. The
contact list is every other extension; FreePBX has no search or paging for it, so
`/api/v2/user/contacts` filters and pages the full list in the adapter.

### FreePBX test stand

We run no FreePBX ourselves, so to develop or check the adapter, set up a lab:

1. **FreePBX 17.** Take a clean Debian 12 VM (2 vCPU, 2 GB RAM is enough) and, as root,
   run Sangoma's official installer — it takes 30 minutes or more and logs to
   `/var/log/pbx/freepbx17-install.log`:
   ```bash
   cd /tmp
   wget https://github.com/FreePBX/sng_freepbx_debian_install/raw/master/sng_freepbx_debian_install.sh
   bash sng_freepbx_debian_install.sh
   ```
   Then open `http://<vm-ip>/admin` and create the admin user. Containers exist only from
   third parties (none from Sangoma), so a VM is the supported way.
2. **API module.** Admin → Module Admin: make sure **API** is installed and up to date —
   versions up to 17.0.8 have a command injection in GraphQL (CVE-2026-40520).
3. **Extensions.** Connectivity → Extensions → Add PJSIP extension, e.g. `1001` with
   voicemail enabled and an e-mail on it, and `1002` with voicemail disabled — the two
   cases sign-in and user info must both handle. Note each extension's **Secret**: it is
   the sign-in password.
4. **API application.** Connectivity → API → Applications → Add Application →
   **Machine-to-Machine**, with scopes allowing GraphQL reads of core and voicemail
   (the Scope Visualizer tab composes them, e.g. `gql:core`). Copy the Client ID and
   Client Secret.
5. **Firewall.** If the FreePBX firewall is on, add the adapter host to Connectivity →
   Firewall → Networks as Trusted.
6. **Adapter.** Build and run it against the lab:
   ```bash
   docker build -t adapter .
   docker run --rm -p 8080:8080 \
     -e BSS_ADAPTER_MODULE=bss.adapters.freepbx -e BSS_ADAPTER_CLASS=FreePBXAdapter \
     -e FREEPBX_API_SERVER=http://<vm-ip> -e FREEPBX_SIP_SERVER=<vm-ip> \
     -e FREEPBX_API_USER=<client id> -e FREEPBX_API_SECRET=<client secret> \
     adapter
   ```
7. **Check.** Sign in, then read the user and the contacts with the returned token:
   ```bash
   curl -s -X POST localhost:8080/api/v1/session -H 'Content-Type: application/json' \
     -d '{"login": "1001", "password": "<secret of 1001>"}'
   curl -s localhost:8080/api/v1/user -H 'Authorization: Bearer <access_token>'
   curl -s localhost:8080/api/v2/user/contacts -H 'Authorization: Bearer <access_token>'
   ```

## NetSapiens adapter settings

The NetSapiens adapter (`BSS_ADAPTER_MODULE=bss.adapters.netsapiens`,
`BSS_ADAPTER_CLASS=NetsapiensAdapter`) talks to the NetSapiens API v2 of each domain
with that domain's API key. Users are `<extension>@<domain>`.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `NETSAPIENS_CLIENTS` | yes¹ | — | JSON list, one entry per domain: `domain`, `api_server` (e.g. `https://ns-api.com`), `api_key`, `device_filter`, `default_sip_outbound_proxy`, `default_sip_port`; `client_id` / `client_secret` only for the password sign-in |
| `NETSAPIENS_CLIENTS_FIRESTORE` | yes¹ | — | Firestore collection holding the same entries, instead of `NETSAPIENS_CLIENTS` |
| `NETSAPIENS_TOKEN_STORAGE` | no | `firestore` | Where the per-user NetSapiens tokens are kept: `firestore`, or `memory` (one replica, or a lab without Google Cloud; a lost token is minted again from the API key) |
| `NETSAPIENS_SSO_SECRET` | no | — | Shared HS256 key of the customer's SaaS. When set, users sign in with a token the SaaS signs (`autoProvision`) |
| `NETSAPIENS_SSO_ISSUER` | no | — | Expected `iss` of the SaaS tokens; not checked when empty |
| `NETSAPIENS_SSO_AUDIENCE` | no | `portadialer` | Expected `aud` of the SaaS tokens |
| `NETSAPIENS_SSO_SIGNUP` | no | `false` | `true` also advertises `signup`: the mobile app's embedded sign-in page posts the token as `sso_token`. The web dialer shows a sign-up tab for this capability, so keep it off for web-only setups |
| `NETSAPIENS_TAB_TOKEN_SECRET` | no | the SSO secret | HS256 key of the tokens for the customer's pages shown as app tabs (`external-page-access-token`, `customMethods`) |
| `NETSAPIENS_TAB_TOKEN_ISSUER` | no | `portadialer` | `iss` of the tab tokens |
| `NETSAPIENS_TAB_TOKEN_AUDIENCE` | no | the SSO issuer | `aud` of the tab tokens |
| `NETSAPIENS_TAB_TOKEN_TTL` | no | `600` | Lifetime of a tab token, seconds; the app asks for a new one when it expires |

¹ One of the two.

**Sign-in.** With `client_id` / `client_secret` (an OAuth client of the domain) users can
sign in with their NetSapiens password. Without them the password sign-in is refused,
since NetSapiens cannot check the password. The usual set-up is a token from the
customer's SaaS: a JWT (HS256) with `sub` = `<extension>@<domain>`, a short `exp` and a
unique `jti`; each `jti` is accepted once. It comes as `config_token` of
`/session/auto-provision` (deeplink, QR code, web dialer widget) or as `sso_token` of
the sign-up request (embedded sign-in page of the mobile app). After that the adapter
acts for the user with a delegated token (`POST /ns-api/v2/jwt`, with the domain API key),
so the user's NetSapiens password is never needed.

**API key.** One per domain. Office Manager scope limited to the domain is enough, with
the models `device, subscriber, contact, cdr, cdr2, voicemail, audio, recording,
phonenumber, oauth`. Write access is used only for voicemail (mark heard, delete). Limit
the key to the adapter's addresses.

**Device.** The app registers as the user's device whose name ends with `device_filter`.
NetSapiens rings a user's devices only when the device is named `<extension>`,
`<extension>` plus one letter (`1001a`) or one of its own suffixes (`1001wp`): a device
named `1001wt` registers but never gets an incoming call. Use a one-letter
`device_filter` such as `a`.

**Numbers.** The user's main number is the first DID routed to the user in
`/phonenumbers` (E.164), else the extension; Core finds users by it, for example for
`/user/notifications`. A DID routed "to user" without the domain as its host goes
straight to voicemail on NetSapiens.

**Voicemail** stays on NetSapiens: the `new` folder is unheard, `save` is heard; marking a
message heard moves it to `save` and cannot be undone; deleting moves it to NetSapiens'
trash. **Call recordings** are looked up by call ID for the answered calls of a history
page. NetSapiens stores them as GSM 6.10 WAV, which browsers cannot play; when `ffmpeg` is
installed in the image the adapter re-encodes them to PCM WAV. A recording is not
available for a minute or two after the call. Forwarding a voicemail to a colleague makes
Core keep an mp3 copy; NetSapiens serves only WAV, so forwarding also needs `ffmpeg`
(without it the adapter refuses mp3 and the forward fails).

**Capabilities.** Besides the PBX-backed ones (history, contacts, voicemail, recordings)
the adapter advertises the features Core provides on its own: chat
(`internalMessaging`), notifications and their push (`notifications`,
`notificationsPush`, without which Core sends no push for a notification posted by a
third party), presence between the apps (`directPresence`), conference, conversation
mute, session auto-termination and voicemail trash/forward. Core also subscribes to the
PBX over SIP for line state (`sipDialogs`, BLF) and for the voicemail indicator
(`sipMwi`); NetSapiens answers both. `sipPresence` is not offered: NetSapiens refuses a
presence PUBLISH (403). Each one can be switched off with the usual
`CAPABILITIES_<NAME>=false` (e.g. `CAPABILITIES_SIP_DIALOGS`). The sign-in capabilities
follow the settings above. Not supported: OTP sign-in, SMS, call center, transcription.

## Outbound connection pool (PortaSwitch)

Every PortaSwitch call goes through one process-wide `httpx.AsyncClient` per TLS-verify
setting, shared by the admin and the account realm. Its pool is the real per-pod ceiling
on concurrent requests toward the switch.

| Variable | Default | Purpose |
|---|---|---|
| `PORTASWITCH_MAX_CONNECTIONS` | `100` | How many connections to the switch a pod may hold at once. Raise it for a large or dedicated switch and high per-instance concurrency; lower it to protect a small or shared one. Requests in flight are capped just below this, so a checkout never has to queue |
| `PORTASWITCH_MAX_KEEPALIVE_CONNECTIONS` | unset — as many as the pool holds | How many of those connections may sit idle waiting to be reused. Set it only to cap idle sockets deliberately; a blank or non-positive value means unset |

**Leave the keep-alive limit unset unless you have a reason not to** (WT-1973). The
adapter talks to a single switch, so a connection it closes after a response is a TCP+TLS
handshake it pays again on the next call. httpx's own default of 20 is meant for a client
spread over many hosts, and it behaves worse than it reads: the underlying pool charges
*busy* connections against the keep-alive budget, closing an idle connection whenever the
pool's **total** count exceeds the limit. With a pool of 100 and a limit of 20, every
connection was therefore closed the moment it answered, as soon as more than 20 were open
at all — and one `/user/contacts` request against a large hierarchy opens ten at a time.
At the reporting installation that reached ~120 new connections per second to a single
IP:port, which exhausted the egress NAT's source ports and started failing calls to
PortaBilling.

Leaving it unset does not raise peak concurrency — that is `PORTASWITCH_MAX_CONNECTIONS`
— it only lets a connection be reused instead of reopened. Steady-state port usage drops,
because each closed connection otherwise holds a source port in `TIME_WAIT` for a minute
afterwards. The trade-off is that the switch sees up to `PORTASWITCH_MAX_CONNECTIONS`
idle sockets per pod; a connection idle for more than five seconds is never reused anyway.

## Standby site certificate verification (PortaSwitch)

With disaster-recovery failover configured (`PORTASWITCH_ADMIN_API_URL_STANDBY` and
`PORTASWITCH_ACCOUNT_API_URL_STANDBY`), the standby site's HTTPS certificate is checked
on its own switch.

| Variable | Default | Purpose |
|---|---|---|
| `PORTASWITCH_VERIFY_HTTPS_STANDBY` | unset — follows `PORTASWITCH_VERIFY_HTTPS` | Whether to verify the standby site's HTTPS certificate |

A standby is usually reached by IP while its certificate names the main site's domain,
so it fails a check the main site passes. Setting this to `false` lets failover work
without turning verification off for the main site too — but the admin credentials and
subscribers' tokens then go to a site whose identity is not checked. The proper fix is a
domain name for the standby that resolves to it and is covered by its certificate (a
wildcard certificate for the main domain often already covers one); use that in the
standby URLs and leave verification on.

## Call recording transcription (PortaSwitch)

`GET /user/recordings/{recording_id}/transcription` returns what PortaBilling's
`CDR/get_transcription` produced, with its own content type and no wrapper: a JSON
document, plain text (`?format=text`), or an archive when the call was recorded as
several files. `?check_only=true` reports presence without the body, for polling a call
that is recorded but not transcribed yet.

`recording_id` is the value `GET /user/history` returned, the same one the recording
download takes — but that value changed shape with this feature. PortaBilling keys the
two halves differently: the audio by `i_xdr` (`CDR/get_call_recording`), the transcript
by `call_recording_id`, which is the xDR's `h323_conf_id`. **No API method in any realm
maps one to the other** — there is no xDR-by-id method and no `i_xdr` filter on any xDR
list — and both keys are visible only while serving the call history. So `recording_id`
now carries both, base64url-encoded because `h323_conf_id` contains spaces and Core
interpolates the value into a URL path unescaped.

An id a client stored before the upgrade is a bare number. It still downloads the
audio; asking it for a transcript answers `404`, and re-reading the call history yields
an id that works.

An id of another account's call answers `403 forbidden_account_access` on both the
download and the transcript (WT-2046), so a client polling for a transcript stops.

## Public recording links (PortaSwitch)

For a CRM that saves a call's recording and transcript as links in a ticket, opened by
anyone who can see the ticket, with no WebTrit session (WT-1993).

- `GET /user/recordings/{recording_id}/link?ttl=<seconds>` (the agent's session) returns
  `{"link_id": "...", "transcription": true}`. Without `ttl` the link never expires; at
  most ten years. The adapter first asks PortaBilling for both halves of the recording
  with the agent's own token, so another account's `recording_id` answers `403`. A
  transcript key PortaBilling does not recognise is left out of the link
  (`"transcription": false`) rather than trusted.
- `GET /public/recordings/{link_id}` and `GET /public/recordings/{link_id}/transcription`
  (no session; `format` and `check_only` as above) are served with the **admin** token,
  so they keep working after the agent signs out. An unknown or altered link, or a
  recording deleted in PortaBilling since, answers `404 recording_link_not_found`; a
  transcript not produced (yet) `404 transcription_not_found`; an expired link `410`.

`link_id` is the recording's `i_xdr` and `call_recording_id` plus the expiry, signed with
an HMAC keyed by **`SECRET_KEY_BASE`** and the host of `PORTASWITCH_ADMIN_API_URL`,
and about 90 characters long. Nothing is stored: a single link cannot be revoked, and
changing either voids every link already handed out — keep it stable across upgrades and identical on every replica. Without it
these endpoints answer `501`. Needs `CAPABILITIES_RECORDINGS` (and
`CAPABILITIES_TRANSCRIPTION` for the transcript).

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

## Add-on restricted sign-in (PortaSwitch)

| Variable | Default | Purpose |
|---|---|---|
| `PORTASWITCH_ALLOWED_ADDONS` | empty — no restriction | Names of PortaBilling add-on products, any one of which lets an account sign in. Several are separated by `;` |

While the list is empty the gate is off. Once it is set, both sign-in methods —
login/password and OTP — read the account's `assigned_addons` and answer
`403 addon_required` unless at least one allowed add-on is assigned.

Add-ons are assigned to the **master** account: an alias row carries none of its own, and
`Account/get_account_info(id=<alias>)` answers with that alias row, not the master's. Both
paths therefore resolve `i_master_account` first and check the master's add-ons, so signing
in with an alias number is allowed exactly when signing in with the master's own id would
be (WT-1926).

The gate is a sign-in check only — an established session outlives the removal of the
add-on until its token expires.

## Accounts without a SIP password (PortaSwitch)

PortaBilling leaves an empty SIP password out of the account record. For such an account
`GET /user` answers `403 sip_credentials_missing` instead of SIP credentials, and sign-in
with `PORTASWITCH_SIGNIN_CREDENTIALS=sip` is refused with `401 incorrect_credentials`
(WT-2196). An account with no self-care login cannot open an account session, so its
sign-in (password or OTP) answers `422 self_care_login_missing`.

## OTP sign-in by email (PortaSwitch)

`POST /session/otp-create` takes the account's email as well as its id (WT-2212), and
`GET /system-info` advertises both as `otp_login_identifiers`, so the apps ask for a phone
number or an email. The identifier is looked up as an account id first, as before, so an
account whose id is itself an address still signs in by id. Only when no account has that
id, and the identifier looks like an address (one `@`, a dot in the domain, no `%`), are
the open accounts searched by their email field — across every customer the admin user
can see.

Exactly one account must have the address as its whole email field (WT-2279). A field
that lists several addresses (`a@x.com, b@x.com`) or holds a name (`John <a@x.com>`) does
not match. Case is ignored as far as the PortaBilling database ignores it, which its
default collations do. With none the answer is `404`, with several `422
ambiguous_user_ref`, and no code is sent either way; at 10 000 matching rows or more the
sign-in is refused with the same `422` rather than decided on part of the list. No
account is ever created.

PortaBilling has no index on the account email field, so on a large installation every
lookup reads the whole accounts table and can run past `PORTASWITCH_API_TIMEOUT`. The
search sends the address without wildcards so that an index, once added, serves it.

Review accounts listed in `OTP_IGNORE_ACCOUNTS` should keep signing in by id. That list
is compared with what the user typed, and an email is not unique in PortaBilling: once
the review account is closed, an email entry would let any other account holding that
address in without a valid code.

## Device and session limits (PortaSwitch)

| Variable | Default | Purpose |
|---|---|---|
| `PORTASWITCH_DEFAULT_ACCOUNT_MAX_DEVICES` | unset — no limit | Devices an account may have signed in at once, when its `portaphone_max_devices` custom field is not set. `0` means no limit |
| `PORTASWITCH_DEFAULT_ACCOUNT_MAX_DEVICE_SWITCHES` | unset — no limit | The same for `portaphone_max_device_switches` |
| `PORTASWITCH_DEVICE_LIMITS_CACHE_TTL` | `60` | Seconds to reuse the custom fields read for an account, and separately for a customer, before reading them again. `0` reads them on every sign-in; a blank value means the default |

Every sign-in that returns a session — login/password, OTP and sign-up, not a session
refresh — adds `customer_id` (the account's `i_customer`) and four limits that Core
enforces: `account_max_devices`, `account_max_device_switches`, `customer_max_devices`
and `customer_max_device_switches` (WT-2005). They are read from the PortaBilling custom
fields `portaphone_max_devices` and `portaphone_max_device_switches` of the **master**
account and of its customer. An account field that is missing or blank falls back to the
default above; a customer field has no default. `null` means no limit, and so does `0`,
whether set on the account, the customer or as a default (an account's own `0` still
overrides the default). A value that is not
a non-negative whole number is ignored with a warning; a default that is not one stops
the adapter from starting.

A failed read never blocks the sign-in. It is logged as a warning and its fields count as
unset — the account limits take the defaults above, the customer limits stay `null` —
while whatever the other read returned is still used. A failed read is not cached.

A limit changed in PortaBilling takes effect within `PORTASWITCH_DEVICE_LIMITS_CACHE_TTL`.

## CRM of the company (PortaSwitch)

| Variable | Default | Purpose |
|---|---|---|
| `PORTASWITCH_CRM_HOST_CUSTOM_FIELD` | empty — not read | Name of the PortaBilling customer custom field that holds the address of the company's CRM, for example `acme.zendesk.com`. The Add-on Mart deployment sets `CRM host` when the Zendesk integration is on. Empty, nothing is read and `crm` is `null` |

The user info (`GET /user`) carries `crm`, the CRM of the user's company, which Core
connects the user's dialer to (WT-2109). The address is read from the custom field of the
account's customer; a branch office whose field is blank takes its main office's address.

- `{"host": "acme.zendesk.com"}` — the company's CRM, as entered in the field;
- `{"host": null}` — the company has none (the field is missing or blank);
- `null` — the adapter cannot tell: the lookup is switched off, or the read failed and
  there is no earlier answer for the company. Core then keeps what it knew.

The answer is reused for 5 minutes per company, so an address changed in PortaBilling
reaches Core within that time. A read is given 5 seconds, so a switch that does not answer
costs the CRM address and not the whole user info. A failed read is logged as a warning, serves
the last answer for the company, however old, and is not tried again for 30 seconds.
