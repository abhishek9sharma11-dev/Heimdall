// meeting.cpp — SDK initialization, JWT-based auth, join/leave.
//
// IMPORTANT: signatures and headers shift between SDK versions. This file is
// written against the v6.x Linux Meeting SDK family. If the headers in
// zoomsdk/h/ don't match, the compiler will tell you exactly which call to
// adjust — usually a renamed param struct or callback name.
//
// References (ship with the SDK):
//   zoomsdk/demo/        — Zoom's official sample
//   zoomsdk/h/zoom_sdk.h — InitParam / CleanUPSDK
//   zoomsdk/h/auth_service_interface.h — IAuthService, AuthContext, IAuthServiceEvent
//   zoomsdk/h/meeting_service_interface.h — IMeetingService, JoinParam
//   zoomsdk/h/meeting_participants_ctrl_interface.h — onUserJoin/onUserLeft

#include "meeting.h"
#include "chat.h"

#include <atomic>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

// Zoom SDK headers
#include "zoom_sdk.h"
#include "auth_service_interface.h"
#include "meeting_audio_interface.h"
#include "meeting_service_interface.h"
#include "meeting_participants_ctrl_interface.h"

// For JWT signing
#include <openssl/hmac.h>
#include <openssl/sha.h>

// nanopb-free JSON for emitting events
#include <nlohmann/json.hpp>

using json = nlohmann::json;
using namespace ZOOMSDK;

namespace Meeting {

namespace {

std::atomic<bool> g_initialized{false};
std::atomic<bool> g_pump_running{false};
EventEmitter g_emit;
std::mutex g_emit_mu;

IAuthService*       g_auth_service    = nullptr;
IMeetingService*    g_meeting_service = nullptr;
IUserInfo*          g_self_info       = nullptr;

void emit_safe(const json& j) {
    std::lock_guard<std::mutex> lk(g_emit_mu);
    if (g_emit) g_emit(j);
}

// ----- Helpers ---------------------------------------------------------

// Convert a wide string (zchar_t* in the SDK on Linux is usually char*)
// to std::string. On Linux SDK builds zchar_t is typedef'd to char,
// so this is a copy. On Windows it'd be wchar_t — adjust if porting.
std::string z2s(const zchar_t* p) {
    return p ? std::string(p) : std::string();
}

// HMAC-SHA256 → base64url (for SDK JWT). Inlined to avoid extra deps.
std::string base64url(const unsigned char* data, size_t len) {
    static const char tbl[] =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    std::string out;
    out.reserve(((len + 2) / 3) * 4);
    for (size_t i = 0; i < len; i += 3) {
        unsigned a = data[i];
        unsigned b = i + 1 < len ? data[i + 1] : 0;
        unsigned c = i + 2 < len ? data[i + 2] : 0;
        unsigned tri = (a << 16) | (b << 8) | c;
        out.push_back(tbl[(tri >> 18) & 0x3F]);
        out.push_back(tbl[(tri >> 12) & 0x3F]);
        if (i + 1 < len) out.push_back(tbl[(tri >> 6) & 0x3F]);
        if (i + 2 < len) out.push_back(tbl[tri & 0x3F]);
    }
    return out;
}

// Build a Meeting SDK JWT. The SDK requires this exact set of claims.
// Reference: developers.zoom.us/docs/meeting-sdk/auth/
std::string build_sdk_jwt(const std::string& key, const std::string& secret) {
    auto now = std::chrono::duration_cast<std::chrono::seconds>(
        std::chrono::system_clock::now().time_since_epoch()).count();
    auto exp = now + 60 * 60 * 2;  // 2 hours

    json header = {{"alg", "HS256"}, {"typ", "JWT"}};
    json payload = {
        {"appKey",     key},
        {"sdkKey",     key},      // some SDK versions check sdkKey, some appKey
        {"mn",         ""},        // meeting number — left empty here, set per-call elsewhere
        {"role",       0},         // 0 = attendee, 1 = host
        {"iat",        now},
        {"exp",        exp},
        {"tokenExp",   exp},
    };

    std::string h_str = header.dump();
    std::string p_str = payload.dump();

    std::string h_b64 = base64url(reinterpret_cast<const unsigned char*>(h_str.data()), h_str.size());
    std::string p_b64 = base64url(reinterpret_cast<const unsigned char*>(p_str.data()), p_str.size());
    std::string signing_input = h_b64 + "." + p_b64;

    unsigned char mac[32];
    unsigned int mac_len = 0;
    HMAC(EVP_sha256(),
         secret.data(), static_cast<int>(secret.size()),
         reinterpret_cast<const unsigned char*>(signing_input.data()),
         signing_input.size(),
         mac, &mac_len);
    std::string sig_b64 = base64url(mac, mac_len);

    return signing_input + "." + sig_b64;
}

// ----- Auth service event handler -------------------------------------

class AuthListener : public IAuthServiceEvent {
public:
    std::atomic<AuthResult> result{AUTHRET_NONE};

    virtual void onAuthenticationReturn(AuthResult ret) override {
        result.store(ret);
        emit_safe({{"type", "auth_result"}, {"code", static_cast<int>(ret)}});
    }
    virtual void onLoginReturnWithReason(LOGINSTATUS, IAccountInfo*, LoginFailReason) override {}
    virtual void onLogout() override {}
    virtual void onZoomIdentityExpired() override {}
    virtual void onZoomAuthIdentityExpired() override {}
};

// ----- Participants event handler -------------------------------------

class ParticipantsListener : public IMeetingParticipantsCtrlEvent {
public:
    virtual void onUserJoin(IList<unsigned int>* user_ids, const zchar_t*) override {
        if (!user_ids || !g_meeting_service) return;
        auto* pc = g_meeting_service->GetMeetingParticipantsController();
        if (!pc) return;
        for (int i = 0; i < user_ids->GetCount(); ++i) {
            unsigned int uid = user_ids->GetItem(i);
            IUserInfo* u = pc->GetUserByUserID(uid);
            if (!u) continue;
            UserRole role = u->GetUserRole();
            std::string role_str = (role == USERROLE_HOST)    ? "host"
                                 : (role == USERROLE_COHOST)  ? "co-host"
                                 : (role == USERROLE_PANELIST)? "panelist"
                                 : "attendee";
            json j = {
                {"type", "participant_joined"},
                {"name", z2s(u->GetUserName())},
                {"user_id", std::to_string(uid)},
                {"role", role_str},
            };
            emit_safe(j);
        }
    }
    virtual void onUserLeft(IList<unsigned int>* user_ids, const zchar_t*) override {
        if (!user_ids) return;
        for (int i = 0; i < user_ids->GetCount(); ++i) {
            json j = {
                {"type", "participant_left"},
                {"user_id", std::to_string(user_ids->GetItem(i))},
                {"name", ""},
            };
            emit_safe(j);
        }
    }
    virtual void onHostChangeNotification(unsigned int) override {}
    virtual void onLowOrRaiseHandStatusChanged(bool, unsigned int) override {}
    virtual void onUserNamesChanged(IList<unsigned int>*) override {}
    virtual void onCoHostChangeNotification(unsigned int, bool) override {}
    virtual void onInvalidReclaimHostkey() override {}
    virtual void onAllHandsLowered() override {}
    virtual void onLocalRecordingStatusChanged(unsigned int, RecordingStatus) override {}
    virtual void onAllowParticipantsRenameNotification(bool) override {}
    virtual void onAllowParticipantsUnmuteSelfNotification(bool) override {}
    virtual void onAllowParticipantsStartVideoNotification(bool) override {}
    virtual void onAllowParticipantsShareWhiteBoardNotification(bool) override {}
    virtual void onRequestLocalRecordingPrivilegeChanged(LocalRecordingRequestPrivilegeStatus) override {}
    virtual void onAllowParticipantsRequestCloudRecording(bool) override {}
    virtual void onInMeetingUserAvatarPathUpdated(unsigned int) override {}
    virtual void onParticipantProfilePictureStatusChange(bool) override {}
    virtual void onFocusModeStateChanged(bool) override {}
    virtual void onFocusModeShareTypeChanged(FocusModeShareType) override {}
    virtual void onBotAuthorizerRelationChanged(unsigned int) override {}
    virtual void onVirtualNameTagStatusChanged(bool, unsigned int) override {}
    virtual void onVirtualNameTagRosterInfoUpdated(unsigned int) override {}
    virtual void onGrantCoOwnerPrivilegeChanged(bool) override {}
};

// ----- Meeting service event handler ----------------------------------

class MeetingListener : public IMeetingServiceEvent {
public:
    virtual void onMeetingStatusChanged(MeetingStatus status, int reason) override {
        emit_safe({
            {"type", "meeting_status"},
            {"status", static_cast<int>(status)},
            {"reason", reason},
        });

        if (status == MEETING_STATUS_INMEETING) {
            Chat::attach(g_meeting_service);
            auto* pc = g_meeting_service->GetMeetingParticipantsController();
            if (pc) {
                static ParticipantsListener pl;
                pc->SetEvent(&pl);
            }
        } else if (status == MEETING_STATUS_ENDED ||
                   status == MEETING_STATUS_FAILED) {
            emit_safe({{"type", "meeting_ended"}});
        }
    }
    virtual void onMeetingStatisticsWarningNotification(StatisticsWarningType) override {}
    virtual void onMeetingParameterNotification(const MeetingParameter*) override {}
    virtual void onSuspendParticipantsActivities() override {}
    virtual void onAICompanionActiveChangeNotice(bool) override {}
    virtual void onMeetingTopicChanged(const zchar_t*) override {}
    virtual void onMeetingFullToWatchLiveStream(const zchar_t*) override {}
    virtual void onUserNetworkStatusChanged(MeetingComponentType, ConnectionQuality, unsigned int, bool) override {}
};

// ----- Singletons -----------------------------------------------------

AuthListener     g_auth_listener;
MeetingListener  g_meeting_listener;

}  // namespace

// ============================================================
// Public API
// ============================================================

bool init_sdk() {
    InitParam ip;
    ip.strWebDomain    = "https://zoom.us";
    ip.strBrandingName = "Om Asnani AI Bot";
    ip.emLanguageID    = LANGUAGE_English;
    ip.enableLogByDefault = true;

    SDKError err = InitSDK(ip);
    if (err != SDKERR_SUCCESS) {
        std::cerr << "[bridge] InitSDK failed: " << err << "\n";
        return false;
    }

    err = CreateAuthService(&g_auth_service);
    if (err != SDKERR_SUCCESS || !g_auth_service) {
        std::cerr << "[bridge] CreateAuthService failed\n";
        return false;
    }
    g_auth_service->SetEvent(&g_auth_listener);

    err = CreateMeetingService(&g_meeting_service);
    if (err != SDKERR_SUCCESS || !g_meeting_service) {
        std::cerr << "[bridge] CreateMeetingService failed\n";
        return false;
    }
    g_meeting_service->SetEvent(&g_meeting_listener);

    g_initialized = true;
    return true;
}

void cleanup_sdk() {
    if (g_meeting_service) DestroyMeetingService(g_meeting_service);
    if (g_auth_service) DestroyAuthService(g_auth_service);
    CleanUPSDK();
    g_initialized = false;
}

bool is_initialized() { return g_initialized.load(); }

void set_event_emitter(EventEmitter emit) {
    std::lock_guard<std::mutex> lk(g_emit_mu);
    g_emit = std::move(emit);
}

void run_pump() {
    // SDK v7 manages its own internal threads — no SDK_Run() call exists.
    // This thread simply keeps the process alive while the SDK operates.
    g_pump_running = true;
    while (g_pump_running.load()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
}

void stop_pump() {
    g_pump_running = false;
}

bool join(const JoinRequest& req, std::string& err) {
    if (!g_initialized) { err = "SDK not initialized"; return false; }
    if (!g_auth_service || !g_meeting_service) { err = "services missing"; return false; }

    // 1. Auth with SDK JWT
    const char* key    = std::getenv("ZOOM_SDK_KEY");
    const char* secret = std::getenv("ZOOM_SDK_SECRET");
    if (!key || !secret) { err = "ZOOM_SDK_KEY/SECRET env vars not set"; return false; }

    std::string jwt = build_sdk_jwt(key, secret);

    AuthContext ctx;
    ctx.jwt_token = jwt.c_str();
    SDKError ar = g_auth_service->SDKAuth(ctx);
    if (ar != SDKERR_SUCCESS) {
        err = "SDKAuth call failed: " + std::to_string(ar);
        return false;
    }

    // Wait for AuthListener to fire (with timeout)
    auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(60);
    while (g_auth_listener.result.load() == AUTHRET_NONE &&
           std::chrono::steady_clock::now() < deadline) {
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
    if (g_auth_listener.result.load() != AUTHRET_SUCCESS) {
        err = "Auth failed: " + std::to_string(g_auth_listener.result.load());
        return false;
    }

    // 2. Join the meeting as a participant
    JoinParam jp;
    jp.userType = SDK_UT_WITHOUT_LOGIN;

    // Without login: meeting number, password, display name. ZAK only needed
    // when starting a meeting as the host.
    auto& wn = jp.param.withoutloginuserJoin;
    wn.meetingNumber = std::stoull(req.meeting_id);
    wn.userName      = req.display_name.c_str();
    wn.psw           = req.password.c_str();
    wn.vanityID      = nullptr;
    wn.customer_key  = nullptr;
    wn.webinarToken  = req.webinar_token.empty() ? nullptr : req.webinar_token.c_str();
    wn.isVideoOff    = true;
    wn.isAudioOff    = true;

    SDKError jr = g_meeting_service->Join(jp);
    if (jr != SDKERR_SUCCESS) {
        err = "Join failed: " + std::to_string(jr);
        return false;
    }

    return true;
}

void leave() {
    if (g_meeting_service) {
        g_meeting_service->Leave(LEAVE_MEETING);
    }
}

}  // namespace Meeting
