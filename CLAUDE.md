# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run the app locally (from the app/ directory)
cd app && uvicorn main:app --port 8000

# Unit tests (no server needed — they import the adapter and stub its API objects)
pytest tests/test_36_portaswitch_call_queues.py
pytest tests -q -p no:warnings

# Integration tests against a running adapter
pytest --server http://<host:port> --user <user> --password <pw> tests

# Formatting
black --line-length 119 <path>
```

`pytest` needs the app dependencies importable (`app/requirements.txt` plus
`tests/requirements.txt`); the unit tests add `app/` to `sys.path` themselves. `models.py`
imports pydantic and every adapter module is pulled in through `bss.adapters`, so even a
pure-unit test run needs `fastapi`, `pydantic*`, `httpx`, `google-cloud-firestore`,
`google-cloud-pubsub` and `python-jose` present. `asyncio_mode = "strict"` — async tests
carry an explicit `@pytest.mark.asyncio`.

## This is the live adapter repository

`origin` is PortaOne Gerrit (`git.portaone.com:29418/porta-phone/adapter_python`), so
commits follow the corporate review process, not Conventional Commits — invoke the
**`gerrit-review`** skill before committing or sending anything to review.

`~/webtrit/multi-tenant-demo/adapter/webtrit_bss_adapter/` is an **older copy of this
same code** that has drifted (different `adapter.py`, `models.py`, `serializer.py`, and it
lacks `app/metrics.py`). Never read it for current behaviour and never edit it — this
repository is the one that ships.

## Architecture

A FastAPI app that translates WebTrit **Core**'s adapter API into whatever a specific
PBX/BSS speaks. Core is the only client; end-user apps never talk here directly.

- **`app/main.py`** — every route. Each handler validates the bearer token
  (`bss.validate_session`), gates the feature with `is_method_allowed(Capabilities.x)`,
  then delegates to the loaded adapter through `call_bss`.
- **`app/bss/models.py`** — the wire contract as pydantic models. It mirrors Core's
  OpenAPI spec (which Core generates from its Elixir `OpenApiSpexExt` schemas — there is
  no committed spec file on either side, see `core/rel/overlays/bin/gen_openapi_spec`).
  Sections marked `# adding this manually` are hand-written extensions.
- **`app/bss/adapters/__init__.py`** — `BSSAdapter`, the abstract base every vendor
  adapter subclasses. A method left unoverridden raises `NotImplementedError`, and
  nothing catches it — `call_bss` translates only `asyncio.TimeoutError`, and there is
  no exception handler — so it reaches the client as a **`500`**. The only `501` in the
  app comes from `is_method_allowed`, i.e. from a capability that is switched off.
- **`app/bss/adapters/`** — the vendor implementations: `portaswitch/` (the real one),
  plus `netsapiens`, `freepbx`, `ext_db_3cx` and `example` as a template.
- **`app/module_loader.py`** — which adapter loads is runtime config:
  `BSS_ADAPTER_MODULE` + `BSS_ADAPTER_CLASS`.
- **`app/app_config.py`** — `AppConfig.get_conf_val("Section", "Option")` reads an
  env var built by upper-casing and `_`-joining the path, so `("Capabilities", "VOICEMAIL")`
  is `CAPABILITIES_VOICEMAIL`. **Every config option is therefore an env var whose name is
  derived, never declared** — grep for the `get_conf_val` call to find one.

### Capabilities

Two layers, and both must say yes:

- `CAPABILITIES` on the adapter class — what this adapter has *code* for.
- `CONFIG_CAPABILITIES_OPTIONS` on `BSSAdapter` — the per-deployment switch and its
  default, resolved by `calculate_capabilities()`. A capability absent from the class
  list can never be switched on. Where the result lands differs per adapter:
  `BSSAdapter.initialize()` stores it in `self.capabilities`, while `PortaSwitchAdapter`
  computes it in `__init__` into `self._cached_capabilities` and serves that.

The result is served as `supported` in `GET /system-info` and is what Core forwards to
clients so they can show or hide a control. Several entries describe features that live
**entirely in Core** and that PortaSwitch knows nothing about — `conference` and
`conversationMute` are pure advertising, there only so a client can tell a new Core from
an old one. `voicemailSave` and `voicemailTrash` each have their own switch; Core does
not read `voicemailTrash`, but a client only sends `DELETE ?trash=true` when it is
advertised, so switching it off makes Delete immediate and permanent again.
`voicemailForward` is the one Core reads: it refuses forwarding when the entry is
absent, so switching that one off changes behaviour, not just what clients are told.

`directPresence` (WT-1834) is the same kind of entry as `voicemailForward`, not the same
kind as `conference`: the feature is Core's own - presence exchanged between WebTrit apps
over PubSub, never reaching PortaSwitch - but Core *requires* the entry. A controller
whose tenant does not advertise it neither publishes its own status nor reads anyone
else's. There is no implicit default on the Core side, so an adapter that does not report
`directPresence` switches the feature off for every tenant it serves; `default=True` here
is what keeps an upgrade from doing that silently.

`calculate_capabilities()` copies the class list before editing it
(`capabilities = list(self.CAPABILITIES)`), and that copy is load-bearing: for the
PortaSwitch adapter the method **runs twice** — once in `__init__`, once in
`initialize()` — so editing the class attribute in place used to leave a
config-disabled capability missing from the shared list, where no later call could
switch it back on. Keep it a copy.

`example.py` overrides `calculate_capabilities` entirely and returns its class list
verbatim, so neither the config switches nor `CAPABILITY_DEPENDENCIES` apply there.
That is deliberate — the demo adapter advertises everything it codes for — but it also
means the documented template to copy does not honour either mechanism.

### PortaSwitch adapter

- **Two realms.** `api/account.py` acts as the signed-in subscriber (its access token is
  the session's); `api/admin.py` acts as the installation's admin account. Some data only
  one of them can see — the call-queue list, for instance, is admin-only, because the
  account realm returns just the caller's own membership row.
- **`async_http_api.py`** is the httpx/asyncio stack that only this adapter inherits
  (WT-1720). In-flight non-streamed requests are capped strictly below the connection-pool
  size, because a request cancelled while queued for a full pool permanently loses that
  pool slot (WT-1922). Streamed downloads hand their permit back once headers arrive.
  `MAX_KEEPALIVE_CONNECTIONS` is deliberately unset (WT-1973), which httpx reads as
  "as many as the pool holds": httpcore charges *active* connections against the
  keep-alive budget — its cleanup pass, run on every response close, drops an idle
  connection whenever the pool's **total** count exceeds that budget — so httpx's own
  default of 20 against a pool of 100 closed every connection the moment it answered,
  once more than 20 were open. One switch, one pool: a budget below the pool size buys
  nothing and costs a handshake per call. `keepalive_expiry` stays at httpx's 5 s and
  is not exposed, so a burst still reconnects after a lull; that is the known residual.
- **`failover.py`** — disaster-recovery site switching. When the sites report
  `operating_mode` (`generic.get_session_data`, BA-47641), a monitor polls both sites on
  wall-clock aligned ticks (`SITE_POLL_INTERVAL`) and is the only thing that switches:
  every replica asks at the same instant and gets the same answers, and requests go to
  the active site only, with no per-request retry on the other one. That matters because
  the secondary-site session sync (BA-47622) resolves a session used on both sites by
  last-write-wins, so pods that switched on their own request errors had one session on
  both sites and lost refresh tokens (WT-2201). Without `operating_mode` the tracker stays
  reactive: a connect error switches the pod and retries on the standby, a read timeout
  only probes the main site, and switching *back* waits for `normal`. It also owns the PortaBilling
  fault-code sets both realms read. `SESSION_AUTH_FAULTS` is the one that outlives the
  switch: PortaSwitch sessions are site-local, so after a failover every token the app
  still holds was minted by the other site and is refused. The admin realm logs in again
  and retries (`admin.py`); the account realm cannot — that session is the subscriber's —
  so `account.py` reports `401 access_token_expired`, and only for Bearer-authenticated
  calls (WT-1814). `Session/login`, `/refresh_access_token`, `/logout` and `/ping` pass
  their token as a *parameter*, not a header, and keep their own, more specific errors —
  `refresh_session`'s `refresh_token_invalid` among them. The one crossover is the Bearer
  `get_account_info` that `refresh_session` makes inside that same `try`: it now answers
  `access_token_expired`, which is the deliberate trade-off of mapping centrally. Carrying
  the session across sites is a platform gap (BA-47630 / BA-47622), not something the
  adapter can close.
  TLS verification is per site (WT-2035): `VERIFY_HTTPS_STANDBY` unset follows
  `VERIFY_HTTPS`, so `_send_rest_request` fetches the shared client **per target**
  (`_verify_for`) — the standby may run on the other verify key's pool, so a pod can
  hold two pools. The admission permit is still the main key's; that bounds the
  standby's pool too only because no connector in the process has the standby's verify
  value as its *main* one (admin and account share settings). Keep it that way.
- **Add-ons gate sign-in, and only the master account has them.** With
  `PORTASWITCH_ALLOWED_ADDONS` set, `_check_allowed_addons` refuses an account whose
  `assigned_addons` holds none of the configured names. An *alias* row carries no add-ons
  of its own and `Account/get_account_info(id=<alias>)` answers with that row, so both
  `authenticate` and `generate_otp` resolve `i_master_account` to the master account
  *before* the check. Skipping the check for a row that has `i_master_account` — which the
  adapter did between January and September 2026 — is what let every OTP sign-in by alias
  through the gate (WT-1926). Resolve, never skip.
  `i_master_account` is not only an alias's: a **beneficiary** (`billing_model` 4, an
  account with a sponsor) has it too, pointing at the sponsor. A beneficiary signs in as
  itself - own password, OTP and session - so `authenticate` and `generate_otp` do not
  resolve it. Its own row already lists the add-ons it inherits from the sponsor (seen
  on a live switch), so the add-on gate reads that row too. Resolving it like an alias
  checked the sponsor's password, which has none - `KeyError` and a 500 (WT-2209).
  The same holds wherever `i_master_account` is followed: `_read_device_limits` reads
  the beneficiary's own custom fields (they are per account, not inherited), and the
  contact lookup by number (`_resolve_master`) shows the beneficiary, not its sponsor.
  Every such site goes through `_is_beneficiary`.
- **OTP sign-in by email (WT-2212)** is a fallback, never a replacement: `generate_otp`
  looks the identifier up as an account id first, because in PortaBilling an id can
  itself be an address, and only on a miss, for an identifier shaped like one, searches
  the email field. There is no switch: an address used to answer 404 on every tenant.
  `Account/get_account_list`'s `email` filter is `=` unless the value holds a `%`
  (`Porta/Account.pm`), and the address goes without one (WT-2279): PortaBilling has no
  index on `Accounts.email`, `%addr%` could never use one, and an exact match can once
  it is added. So a field listing several addresses or in the `Name <address>` form no
  longer matches - a trade-off the first customer confirmed against its data. `_EMAIL_RE`
  refusing `%` is what keeps the filter exact; do not relax it. `get_total` stays 0: the
  count was a second full scan. `_find_account_by_email` still compares each row itself,
  refuses (`422 ambiguous_user_ref`) when several match, sending no code, pages through
  them all and refuses past `EMAIL_LOOKUP_MAX_ROWS` rather than decide on a partial
  list. It then re-reads the match with `get_account_info`, because the list rows carry
  no `assigned_addons` and the add-on gate would refuse every account.
- **`serializer.py`** — every PortaBilling payload → wire model conversion. Field names
  and date formats live here, not in `adapter.py`.

### `recording_id` carries two PortaBilling keys, not one

PortaBilling keys the two halves of a recorded call differently and offers no bridge
between them: the audio by `i_xdr` (`CDR/get_call_recording`, `i_xdr` required), the
transcript by `call_recording_id` — the xDR's `h323_conf_id` — in
`CDR/get_transcription`, which has no `i_xdr` variant in any realm, MR128 through
MR131. There is no xDR-by-id method anywhere and no `i_xdr` filter on any xDR list, so
given only an `i_xdr` the `call_recording_id` cannot be looked up. (FR-565's HLD did
specify adding `i_xdr`; its post-implementation note says the API requirements were not
implemented, because one xDR can still map to several recording files.)

Both keys sit side by side in the `Account/get_xdr_list` row and nowhere else, so
`Serializer.compose_recording_id` packs both into the `recording_id` the call history
hands out — base64url, because `h323_conf_id` holds spaces and Core interpolates path
params unescaped. `parse_recording_id` splits it back. A bare numeric id is one minted
before WT-1963: it still downloads, and the transcript answers 404 rather than
guessing. Do not "simplify" this back to `str(i_xdr)` — that silently removes the only
path to a transcript.

### Whether a call has a recording depends on the release

From MR129 on, `get_xdr_list` no longer sets `bit_flags` bit 64 for a recorded call
(IUN-1014). `with_cr_download_ids=1` does report recordings (WT-1939), but PortaBilling
builds it by listing the recording storage over SFTP for every recorded call — up to a
minute per page on a busy switch, and slower still before MR129 (WT-2241). It is never
sent. Instead `retrieve_calls` asks `get_xdr_list` a second time with
`call_recording=1`, a database filter on `Call_Recordings`, and marks the page rows it
returns (`_recorded_xdrs`, WT-2283). That request is narrowed to the `bill_time` of the
page's own rows — the field `from_date`/`to_date` filter on, `to_date` exclusive, hence
+1 s: the page's whole range with offset 0 would return the newest recorded calls, not
this page's. Below MR129 (`Generic/get_version`, cached an hour, a failure a minute)
there is no second request and bit 64 is read; do not read it on MR129+, it is stale
there. An unreadable version takes the second request: below MR129 `call_recording=1`
filters by that same bit, so it is correct everywhere.

### A call voicemail took is one history item (WT-2281)

PortaSwitch writes two xDRs on the callee's account for it, with one `call_id`: the
incoming leg that rang (missed/declined) and a forwarded leg (`bit_flags & 12 == 12`,
accepted) to the callee's own number, rated as `UMRECORD|<own number>`.
`Serializer.drop_own_number_forwards` drops the forwarded leg when the page or its margin
holds an incoming leg with the same `i_account`, `call_id` and `CLD` number - a real forward
(follow-me, transfer, Auto Attendant redirect) carries the target's `CLD`. Do not key it
on `destination`: it is the tariff's prefix (`UM` on one switch, `UMRECORD` on another)
and is not returned at all before MR128 (BA-41943). The list is ordered by `bill_time`,
so `retrieve_calls` reads `CALL_HISTORY_PAIR_MARGIN` rows past each edge of the page to
match a pair a page edge splits; those rows are never returned. A pair split by the
`time_from`/`time_to` edge stays unmatched. `total` stays the switch's count, so a page
can come back short; lowering it would drop the last page from a client's count.

### Callers waiting are keyed by `i_c_queue`, not `huntgroup_id`

`count_callers_waiting` keys a `queued` call of `CallControl/get_sip_calls_list` by
`queue_info.i_c_queue` and `_build_queues` matches it to `assigned_callqueue.i_c_queue`.
`callee.huntgroup_id` marks the callee's hunt group *membership*, not routing
(`Porta/SIP/CallControl.pm`), so a caller queued via an auto attendant has none and was
never counted (WT-2293).

### Voicemail is an IMAP mailbox, and that shapes the whole feature

PortaBilling exposes exactly five mailbox calls (`api/account.py`):
`get_mailbox_message_list` (filterable only by `from_date`/`to_date`),
`get_mailbox_message_details`, `get_mailbox_message_attachment` (`wav`/`mp3`/`au`),
`set_mailbox_messages_flag` and `delete_mailbox_messages`.

Consequences that are easy to get wrong:

- **There are no folders.** The list is flat. `flags` on each message is the only
  per-message state, and `set_mailbox_messages_flag` accepts only `Seen`, `Answered` and
  `Flagged` (`types.py`). `\Deleted` is not reachable.
- **`saved` is `\Flagged`.** That keeps the state in PortaSwitch, shared across the
  subscriber's devices and consistent with the UM webmail, and costs no storage anywhere.
- **`delete_mailbox_messages` is permanent** — there is no trash, and nothing to restore
  from.
- **There is no forward API**, and none of the flags can carry one.

So a **Trash** folder and **forwarding** cannot be implemented here at all: both are
Core-side features. Core keeps the per-user trash state and, for a forward, downloads the
attachment through `GET /user/voicemails/{id}/attachment` and stores that one copy itself.
This adapter's share of the feature is `saved` and the capability flags; do not add a
`folder` query parameter here, because only Core knows what is in the trash and the
partitioning has to happen where both halves of the state are.

The same mailbox is real e-mail: a flag set here is visible in the subscriber's UM
webmail and any IMAP client, which is why repurposing `Answered` to mean anything else
would corrupt what they see.

### A vanished `message_uid` is not reported as missing

A message deleted straight on PortaSwitch - over the IVR, from the UM webmail, from
another device - leaves every client's list stale, and PortaBilling does not answer
"not found" for the id that list still shows. `get_mailbox_message_details` returns
**200 describing a different message of the same mailbox**, stripped of `subject`,
`from`, `to` and `voicemail_duration`; `get_mailbox_message_attachment` returns a JSON
body instead of the media file, which `decode_response` hands back as a dict (WT-1992).

So both read paths verify what came back before using it - the details record against
the requested `message_uid` *and* for the presence of `from`/`to`, the attachment for
being a `(content_type, iterator)` pair - and report a mismatch as
`404 message_not_found`. The id half is the load-bearing one: the stripped record
happens to raise `KeyError: 'from'`, but everything `get_voicemail_message` needs is
present, so a *complete* foreign record would be served as a 200 under the requested
id. Do not replace the check with a tolerant `.get("from")` - `sender`/`receiver` are
required by `VoicemailMessageDetails`, so that only moves the failure into pydantic
and, worse, hides the wrong-message answer.

## Documentation rules

When adding a config option, a route, a capability or any user-facing behaviour:
- document the option in `README.md` (its env-var name is derived by `get_conf_val`, so it
  is invisible unless written down);
- keep the route's FastAPI `description`/`response_model` accurate — that text *is* the
  published OpenAPI spec;
- update this file when a load-bearing constraint changes;
- add it to `webtrit_deploy_internal` (Helm `values.yaml` + ConfigMap template).

This applies to every change — documentation is part of the definition of done.
