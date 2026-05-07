// chat.cpp — chat send and receive via the Zoom Meeting SDK.

#include "chat.h"

#include <atomic>
#include <iostream>
#include <mutex>
#include <string>

#include "meeting_service_interface.h"
#include "meeting_chat_interface.h"

#include <nlohmann/json.hpp>

using json = nlohmann::json;
using namespace ZOOMSDK;

namespace Chat {

namespace {

EventEmitter g_emit;
std::mutex g_emit_mu;

IMeetingChatController* g_chat = nullptr;
std::atomic<bool> g_attached{false};

void emit_safe(const json& j) {
    std::lock_guard<std::mutex> lk(g_emit_mu);
    if (g_emit) g_emit(j);
}

std::string z2s(const zchar_t* p) {
    return p ? std::string(p) : std::string();
}

// ----- Chat event listener -------------------------------------------

class ChatEventListener : public IMeetingChatCtrlEvent {
public:
    virtual void onChatMsgNotification(IChatMsgInfo* chatMsg, const zchar_t* content = nullptr) override {
        if (!chatMsg) return;

        std::string text = content ? z2s(content) : z2s(chatMsg->GetContent());
        std::string sender_name = z2s(chatMsg->GetSenderDisplayName());
        unsigned int sender_id  = chatMsg->GetSenderUserId();
        bool is_private = !chatMsg->IsChatToAll();

        json j = {
            {"type", "chat_message"},
            {"sender_name", sender_name},
            {"sender_id", std::to_string(sender_id)},
            {"sender_email", nullptr},
            {"text", text},
            {"is_private", is_private},
        };
        emit_safe(j);
    }

    virtual void onChatStatusChangedNotification(ChatStatus*) override {}
    virtual void onChatMsgDeleteNotification(const zchar_t*, SDKChatMessageDeleteType) override {}
    virtual void onChatMessageEditNotification(IChatMsgInfo*) override {}
    virtual void onShareMeetingChatStatusChanged(bool) override {}
    virtual void onFileSendStart(ISDKFileSender*) override {}
    virtual void onFileReceived(ISDKFileReceiver*) override {}
    virtual void onFileTransferProgress(SDKFileTransferInfo*) override {}
};

ChatEventListener g_listener;

}  // namespace

// ============================================================
// Public API
// ============================================================

void set_event_emitter(EventEmitter emit) {
    std::lock_guard<std::mutex> lk(g_emit_mu);
    g_emit = std::move(emit);
}

bool attach(IMeetingService* ms) {
    if (!ms) return false;
    g_chat = ms->GetMeetingChatController();
    if (!g_chat) {
        std::cerr << "[bridge] GetMeetingChatController returned null\n";
        return false;
    }
    g_chat->SetEvent(&g_listener);
    g_attached = true;

    emit_safe({{"type", "chat_attached"}});
    return true;
}

bool send(const std::string& text, const std::string& to, std::string& err) {
    if (!g_attached || !g_chat) {
        err = "chat controller not attached (are we in a meeting?)";
        return false;
    }

    IChatMsgInfoBuilder* builder = g_chat->GetChatMessageBuilder();
    if (!builder) { err = "GetChatMessageBuilder returned null"; return false; }

    builder->SetContent(text.c_str());
    if (to == "everyone" || to.empty()) {
        builder->SetReceiver(0);
        builder->SetMessageType(SDKChatMessageType_To_All);
    } else {
        unsigned int uid = static_cast<unsigned int>(std::stoul(to));
        builder->SetReceiver(uid);
        builder->SetMessageType(SDKChatMessageType_To_Individual);
    }

    IChatMsgInfo* msg = builder->Build();
    if (!msg) { err = "IChatMsgInfoBuilder::Build() returned null"; return false; }

    SDKError r = g_chat->SendChatMsgTo(msg);
    if (r != SDKERR_SUCCESS) {
        err = "SendChatMsgTo failed: " + std::to_string(r);
        return false;
    }
    return true;
}

}  // namespace Chat
