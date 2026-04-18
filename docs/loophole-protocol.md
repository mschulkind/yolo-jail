# Loophole wire protocol — v1

This is the framed-socket protocol spoken between a jail-side client
and a host-side loophole daemon that uses the `src/host_service.py`
helper library (transport: `unix-socket`, lifecycle: `spawned`).

External loophole authors can rely on this spec: breaking changes
will bump `PROTOCOL_VERSION` and ship a transition window.

## Handshake

There is no handshake. A client opens the Unix socket, sends one
length-prefixed JSON request, and reads framed response data until the
server closes the connection or emits an exit frame.

## Request

A single JSON object, length-prefixed with a 4-byte big-endian unsigned
int giving the UTF-8-encoded JSON body length:

```
+------------------+---------------------------------------+
| 4-byte big-endian| UTF-8 JSON body, exactly <length>     |
| unsigned length  | bytes                                 |
+------------------+---------------------------------------+
```

Canonical fields (convention, not enforced):

| Field      | Type    | Meaning                                                  |
|------------|---------|----------------------------------------------------------|
| `jail_id`  | string  | Jail identifier for logging (daemons must not trust it). |
| `mode`     | string  | Request kind, per-daemon vocabulary.                     |
| others     | any     | Daemon-specific; see its module.                         |

Example (host-processes list):

```json
{"jail_id": "yolo-a1b2c3", "mode": "list"}
```

## Response

After the request, the daemon writes zero or more **frames** then
optionally a final **exit frame**. Each frame:

```
+---------+-------------------+------------------+
| 1 byte  | 4 bytes big-endian| <length> bytes    |
| stream  | unsigned length   | payload           |
| id      |                   |                   |
+---------+-------------------+------------------+
```

Stream IDs:

| ID | Name   | Payload                                                                  |
|----|--------|--------------------------------------------------------------------------|
| 0  | stdout | Bytes the client should forward to its own stdout.                       |
| 1  | stderr | Bytes the client should forward to its own stderr.                       |
| 2  | exit   | Exactly 4 bytes: big-endian signed int32 exit code. Terminates response. |

A client consumes frames until it sees stream id 2 (exit) or the
socket closes. A daemon that finishes without sending an exit frame is
treated as exit code 0 by the library; closure without frames counts
as a protocol error (client's choice how to report).

## Framing rules

- Frames are independent; payload may be empty (length 0). Clients must
  handle zero-length frames.
- stdout and stderr frames may arrive interleaved; the client should
  forward each to the corresponding stream without reordering.
- After an exit frame, the daemon MUST NOT send additional frames. The
  library enforces this via `Session._exited`.
- Neither side should hold the socket open after the exit frame. Clients
  close after reading exit; daemons close after writing it.

## Versioning

`PROTOCOL_VERSION = 1` is exposed by the library. Future revisions that
break wire format bump this number and add a separate frame/field for
the version negotiation (so v1 clients continue to work against v1
daemons with no change).

A daemon SHOULD log its advertised version on startup. Clients do not
currently send a version with the request; if we need per-request
versioning later, it will be an optional `_v` field in the request
body.

## Security posture

- The socket is chmod 0600 and lives under the user's socket dir (one
  per jail boot; the path lives in the `YOLO_*_SOCKET` env var the jail
  sees).
- The socket file is the authentication. A daemon trusts whoever can
  `connect()` — which is the jail (and anything else running as the
  same user on the host).
- Daemons must never trust request fields as argv material. The
  library's `Session.exec_allowlisted(argv_builder, allowlist=...)`
  helper enforces this by construction: argv positions are validated
  against a server-owned allowlist before the subprocess runs.

## Access logging

The helper library logs one structured line per request:

```
INFO host_service: jail=<id> keys=<sorted-req-keys> rc=<code> elapsed_ms=<n> bytes_out=<n>
```

Full request bodies are not logged (could be large or sensitive); just
the top-level key names, the exit code, and the total bytes written
across stdout+stderr frames. Enough to audit "what did jail X ask for"
without hoarding payload data.

## Writing a client from scratch

Not strictly necessary — the existing `yolo-ps` is the reference
implementation — but if you need a non-Python client:

1. Connect `AF_UNIX` stream socket to the path in
   `$YOLO_<SERVICE>_SOCKET`.
2. Write a 4-byte big-endian request length, then the JSON body.
3. Read response:
   - Read 5-byte header `(stream_id:u8, length:u32)`.
   - Read `length` bytes; forward or capture by `stream_id`.
   - If `stream_id == 2`, payload is a 4-byte signed exit code; done.
4. Close the socket.
