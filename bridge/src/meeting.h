// meeting.h — wraps Zoom IAuthService + IMeetingService.
//
// init_sdk() / cleanup_sdk() bracket the SDK lifetime.
// run_pump() blocks until stop_pump() is called — runs the SDK's event loop.
// join() authenticates with JWT, then joins the meeting.
//
// All SDK-touching code lives in meeting.cpp / chat.cpp. The HTTP layer
// in main.cpp talks to these wrappers and never includes Zoom headers
// directly — keeps build dependencies clean.

#pragma once

#include <functional>
#include <string>

#include <nlohmann/json.hpp>

namespace Meeting {

struct JoinRequest {
    std::string meeting_id;
    std::string password;
    std::string zak;             // optional, only when starting as host
    std::string display_name;
    std::string avatar_url;
    std::string webinar_token;   // panelist tk= token from the panelist join link
};

using EventEmitter = std::function<void(const nlohmann::json&)>;

bool init_sdk();
void cleanup_sdk();
bool is_initialized();

void set_event_emitter(EventEmitter emit);

// Blocks until the SDK exits. Run on a dedicated thread.
void run_pump();
void stop_pump();

// Authenticates (using ZOOM_SDK_KEY/ZOOM_SDK_SECRET env vars) then joins.
// Returns false on failure with err populated.
bool join(const JoinRequest& req, std::string& err);

// Leaves the meeting. Safe to call even if not joined.
void leave();

}  // namespace Meeting
