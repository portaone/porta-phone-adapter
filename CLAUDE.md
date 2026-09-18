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
an old one, as are `voicemailSave` and `voicemailTrash`, which have no switch and come
with `voicemail` itself. `voicemailForward` is the one Core reads: it refuses forwarding
when the entry is absent, so switching that one off changes behaviour, not just what
clients are told.

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
- **`failover.py`** — disaster-recovery site switching. Detecting that the main site is
  down is reactive (timeouts), while switching *back* is authoritative
  (`operating_mode` from `generic.get_session_data`). It also owns the PortaBilling
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

## Documentation rules

When adding a config option, a route, a capability or any user-facing behaviour:
- document the option in `README.md` (its env-var name is derived by `get_conf_val`, so it
  is invisible unless written down);
- keep the route's FastAPI `description`/`response_model` accurate — that text *is* the
  published OpenAPI spec;
- update this file when a load-bearing constraint changes;
- add it to `webtrit_deploy_internal` (Helm `values.yaml` + ConfigMap template).

This applies to every change — documentation is part of the definition of done.
