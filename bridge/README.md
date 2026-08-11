# C++ Bridge — Zoom Meeting SDK Wrapper

A small Linux daemon that uses the official Zoom Linux Meeting SDK to join webinars, send/receive chat, and expose everything to the Python orchestrator over HTTP.

This is the only part of the project that touches the Zoom SDK directly. Everything else is pure Python.

## What you need

1. **Linux** — Ubuntu 22.04 or 24.04. The Zoom Linux SDK only runs here.
2. **Build tools**: `cmake`, `g++`, `pkg-config`
3. **System libs the SDK needs** (apt names):
   ```
   libssl-dev libxcb-shape0-dev libxcb-shm0-dev libxcb-xfixes0-dev
   libxcb-randr0-dev libxcb-image0-dev libfontconfig1-dev libxcb-icccm4-dev
   libxcb-keysyms1-dev libxcb-render-util0-dev libxcb-util-dev
   libxcb-xkb-dev libxkbcommon-x11-dev libdbus-1-dev libglib2.0-dev
   libgbm-dev libpulse-dev
   ```
   Run `apt list --installed 2>/dev/null | grep libxcb` to check what's there.
4. **The Zoom Linux Meeting SDK** — download from your Zoom Marketplace app's Embed → Meeting SDK → Download Linux SDK. Unzip into `bridge/zoomsdk/`.

## SDK file layout this build expects

After unzipping the SDK, your `bridge/` should look like:

```
bridge/
├── CMakeLists.txt
├── src/
│   ├── main.cpp
│   ├── meeting.cpp
│   ├── meeting.h
│   ├── chat.cpp
│   ├── chat.h
│   └── httplib.h          ← single-header HTTP server (download from yhirose/cpp-httplib)
└── zoomsdk/
    ├── h/                 ← SDK headers (zoom_sdk.h, meeting_service_interface.h, etc.)
    ├── lib/               ← libmeetingsdk.so and friends
    └── version.txt
```

If the SDK ships with a different folder layout, edit `CMakeLists.txt` paths to match.

## Get cpp-httplib

```bash
cd bridge/src
curl -O https://raw.githubusercontent.com/yhirose/cpp-httplib/master/httplib.h
```

It's a single header. No further setup needed.

## Build

```bash
cd bridge
mkdir -p build && cd build
cmake ..
make -j
./zoom-bridge
```

You should see `[bridge] listening on 127.0.0.1:8765`.

## Test the bridge alone

```bash
# in another terminal
curl http://127.0.0.1:8765/health
# → {"status":"ok","sdk_initialized":true}
```

## Run linked to a real meeting

The Python orchestrator drives the bridge. You don't normally call these endpoints by hand — but for testing:

```bash
curl -X POST http://127.0.0.1:8765/join \
  -H 'Content-Type: application/json' \
  -d '{"meeting_id":"123456789","password":"xxx","display_name":"Heimdall AI"}'
```

## Wire protocol (commands the Python side calls)

| Method | Path | Body |
|---|---|---|
| GET | `/health` | — |
| POST | `/join` | `{meeting_id, password, zak, display_name, avatar_url}` |
| POST | `/send_chat` | `{text, to}` (`to` = `"everyone"` or a user_id) |
| POST | `/leave` | `{}` |
| GET | `/events` | Server-Sent Events stream of meeting events |

## Wire protocol (events the bridge emits)

`GET /events` returns `text/event-stream` with one JSON object per `data:` line:

```
data: {"type":"chat_message","sender_name":"Priya","sender_id":"abc","sender_email":null,"text":"how do I sign up?","is_private":false}
data: {"type":"participant_joined","name":"Priya","user_id":"abc","email":null,"role":"attendee"}
data: {"type":"participant_left","name":"Priya","user_id":"abc"}
data: {"type":"meeting_ended"}
```

## What's stubbed and what works

The skeleton in this folder shows the structure of an SDK-based bridge. The auth flow, init/cleanup, and HTTP plumbing are wired correctly. The actual SDK calls are commented with `// TODO` markers because they need to be matched to the exact SDK version you download (Zoom changes signatures between releases).

Specifically you'll need to fill in (using the SDK headers in `zoomsdk/h/` as the source of truth):

1. **`meeting.cpp`** — `IAuthService::SDKAuth`, `IMeetingService::Join`, listening for `IMeetingServiceEvent::onMeetingStatusChanged`
2. **`chat.cpp`** — `IMeetingChatController::SendChatTo` and `IMeetingChatControllerEvent::onChatMsgNotifcation`
3. **Participant tracking** — `IMeetingParticipantsController::onUserJoin/onUserLeft`

The relevant SDK headers are:
- `zoom_sdk.h` — `InitParam`, `InitSDK`, `CleanUPSDK`
- `auth_service_interface.h` — `IAuthService`, `AuthContext`
- `meeting_service_interface.h` — `IMeetingService`, `JoinParam`
- `meeting_chat_interface.h` — chat controller + events
- `meeting_participants_ctrl_interface.h` — participants controller + events

There's a working reference for the init/auth/join pattern in:
- The official sample at `zoomsdk/demo/` (ships with the SDK)
- The community repo: github.com/zoom/meetingsdk-linux-raw-recording-sample

Plan to spend half a day matching the boilerplate to your SDK version. Once it works, it rarely changes.
