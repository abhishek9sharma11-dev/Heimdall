// chat.h — wraps Zoom IMeetingChatController + IMeetingChatControllerEvent.
//
// attach() is called once we're inside a meeting (after MEETING_STATUS_INMEETING).
// send() posts a chat message, either to everyone or to a specific user_id.
// Incoming messages are emitted via the event emitter set in main.cpp.

#pragma once

#include <functional>
#include <string>

#include <nlohmann/json.hpp>

namespace ZOOMSDK { class IMeetingService; }

namespace Chat {

using EventEmitter = std::function<void(const nlohmann::json&)>;

void set_event_emitter(EventEmitter emit);

// Wires the chat controller's events. Call once after joining.
bool attach(ZOOMSDK::IMeetingService* meeting_service);

// Send a chat message. `to` = "everyone" or a user_id string.
// Returns false on failure with err populated.
bool send(const std::string& text, const std::string& to, std::string& err);

}  // namespace Chat
