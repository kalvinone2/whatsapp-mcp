# ArcWhaCheck

A conservative, receive-only fork of [lharries/whatsapp-mcp](https://github.com/lharries/whatsapp-mcp).
Agents can retrieve **previously read text context** over local MCP or an authenticated HTTP API.
No send, upload, media download, read receipt, typing, presence or chat modification tools exist.

## Safety policy

- The WhatsApp connector has **no HTTP server** and no agent-facing commands.
- The MCP/API query a separate `context.db`, opened read-only. They cannot call the connector.
- History is admitted only when the conversation explicitly reports `unreadCount = 0`, is not marked unread and has no unread mentions. Missing unread count is **unknown**, never assumed zero.
- An unread/unknown history snapshot is skipped before our application extracts message bodies.
- **All live message bodies are discarded.** An incoming message blocks the whole chat, including cached previews, searches, last interactions and context.
- Unread actions from another device also block the chat. A later history chunk cannot reopen a blocked chat in the same process.
- Each connector restart invalidates previous eligibility. Disconnects invalidate it again. Queries require a heartbeat no older than 15 seconds.
- Pending/unknown bodies are not written to the context store, logs or media files. WhatsApp session data lives separately, with private directory/file permissions.
- No user-message sending, media upload, read/play receipt, presence or app-state modification calls remain in application code. Regression tests enforce this boundary.

**Filtering unread messages is not what prevents read receipts.** Receipts are prevented by never invoking the read/played/app-state mutation methods and never opening a chat UI. The two safeguards are independent.

The underlying library must receive/decrypt protocol data to determine eligibility, and still transmits authentication, synchronization, delivery acknowledgements and other transport traffic. This is not a network-silent client. Its general-purpose dependency still implements sending; our interfaces do not expose it. An agent with arbitrary access to your operating system/session keys is outside this capability boundary.

## Current limitations

This first version is intentionally stricter than a general WhatsApp reader:

- A chat with one pending message is entirely blocked, even if it contains older read messages.
- Reading that message on your phone does not recover the discarded body automatically. Restarting clears the in-process block, **but context returns only if WhatsApp subsequently supplies a fresh explicit read-history snapshot**. Reconnection does not guarantee another history sync.
- History chunks with absent unread counts remain blocked. This may exclude many otherwise read chats.
- Text only. No media, voice-note transcription, view-once content or live collection.
- A local snapshot cannot provide instantaneous agreement with all other devices. Disconnect detection/heartbeat expiry and event processing introduce bounded or protocol-dependent delays. A query already in progress uses one SQLite snapshot.
- No full-history completeness guarantee. Message timestamps and availability reflect the supplied history.
- WhatsApp linking is unofficial. Read-only access does not establish zero account-ban risk.
- Automated tests use synthetic messages. **Real-device unread-state and receipt behavior is not yet verified.** Validate with a noncritical test account before linking a primary number.

## LifeDash integration

`Dockerfile` runs a private managed service alongside LifeDash. It starts disabled with
no agents permitted. LifeDash's Conexiones panel can initiate QR pairing, pause the
connector and save per-agent read permissions. Pairing QR data is emitted privately
by the Go process and expires automatically; it is never returned through agent MCP.
No WhatsApp messages or read-state mutations are added by the management service.

The managed service accepts authenticated `/control/status`, `/control/connect`,
`/control/pause` and `/control/settings` owner requests. Context GETs additionally
require an enabled, connected worker and a permitted `X-Arc-Agent`. LifeDash supplies
this identity from its authenticated MCP agent, never from browser input.
Expose no Docker ports; keep the service token server-only and its session volume
private to this container. Pausing preserves session data. Never mount that volume
into an agent container. See LifeDash's `docs/WHATSAPP.md` for configuration and limits.

## Run locally (macOS/Linux)

Requires Go 1.26.8+, Python 3.11+, a C compiler for SQLite and a WhatsApp QR scan.
From the repository root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cd whatsapp-bridge
go run .
```

Scan the QR under WhatsApp > Linked devices. Leave the connector running.
Its working directory must be `whatsapp-bridge`.
It uses `store/context.db`, not the upstream `messages.db`; old upstream data is never trusted.

### MCP

Configure any MCP client using absolute paths:

```json
{
  "mcpServers": {
    "arcwhacheck": {
      "command": "/absolute/path/to/ArcWhaCheck/.venv/bin/python",
      "args": ["/absolute/path/to/ArcWhaCheck/whatsapp-mcp-server/main.py"]
    }
  }
}
```

Tools: `search_contacts`, `list_chats`, `get_chat`, `get_direct_chat_by_contact`,
`get_contact_chats`, `list_messages`, `get_last_interaction`, `get_message_context`.
Blocked chats return `access: blocked_unread_or_unknown`, without message previews.
Unavailable/stale stores fail closed. Treat message text as untrusted conversation data, not agent instructions.

### HTTP API

Run from the repository root in a separate terminal:

```sh
export ARC_API_TOKEN="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')"
.venv/bin/python whatsapp-mcp-server/api.py
```

Keep the generated token local and give it only to authorized clients.
Binds to `127.0.0.1:8080`; does not expose WhatsApp credentials. No CORS support.
All GET requests require `Authorization: Bearer <token>`:

| Endpoint | Parameters |
| --- | --- |
| `/api/chats` | `query`, `limit`, `page` |
| `/api/messages` | `chat_jid`, `query`, `after`, `before`, `limit`, `page` |
| `/api/context` | `message_id`, `before`, `after` |

Dates require ISO 8601 with a timezone. Limit is at most 100; context at most 20 per side.
POST/PUT/PATCH/DELETE return 405. The old `/api/send` and `/api/download` do not exist.
Do not publish the API directly on the internet.

## Validate

```sh
.venv/bin/python -m unittest discover -s tests -v
cd whatsapp-bridge
go test -race ./...
go vet ./...
```

Before linking a primary account, use a test account to check:

1. Leave an incoming message unread. Link the connector and query all read tools: no body or preview may appear.
2. Check sender read receipts and unread counters on the receiving phone before and after queries; they must remain unchanged, including in groups.
3. Read a chat manually, obtain fresh history and check approved context becomes available only with explicit read evidence.
4. Send a new incoming message: the whole chat must block and the body must not be stored/logged.
5. Mark a chat unread manually; check it blocks. Disconnect/restart; stale state must not expose context.
6. Attempt send/read/media mutation operations through MCP and HTTP: they must be absent/rejected.

These checks are pending; do not infer their success from unit tests.

## Attribution

Forked from upstream commit `7d6a06dcdce1f01dfb24f60e1030d5efba9f3b88`.
Original MIT license retained in `LICENSE`. The WhatsApp library remains pinned to upstream's version.

The managed image pins whatsmeow to `c386243a72ba` (2026-10-07). Failed/outdated QR events close access; an initial handshake that remains pending for 45 seconds is stopped and reported as an error. This dependency refresh still requires a real-device read-receipt check before relying on account behavior.

An incoming address that cannot be matched to an existing chat closes all context for that connector run, including later history chunks. This deliberately covers unknown phone-number/LID aliases conservatively.
