// meeting.cpp — SDK initialization, JWT-based auth, join/leave.
//
// Zoom Linux SDK delivers auth/meeting callbacks through a GLib main loop.
// Pattern matches zoom/meetingsdk-headless-linux-sample.

#include "meeting.h"
#include "chat.h"

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include <glib.h>

#include "zoom_sdk.h"
#include "auth_service_interface.h"
#include "meeting_audio_interface.h"
#include "meeting_service_interface.h"
#include "meeting_participants_ctrl_interface.h"

#include <openssl/hmac.h>
#include <openssl/sha.h>

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

GMainLoop* g_loop = nullptr;

void emit_safe(const json& j) {
    std::lock_guard<std::mutex> lk(g_emit_mu);
    if (g_emit) g_emit(j);
}

std::string z2s(const zchar_t* p) {
    return p ? std::string(p) : std::string();
}

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

std::string build_sdk_jwt(const std::string& key, const std::string& secret) {
    auto now = std::chrono::duration_cast<std::chrono::seconds>(
        std::chrono::system_clock::now().time_since_epoch()).count();
    // Slightly in the past avoids clock-skew "iat in the future" failures.
    auto iat = now - 30;
    auto exp = iat + 60 * 60 * 2;

    json header = {{"alg", "HS256"}, {"typ", "JWT"}};
    json payload = {
        {"appKey",   key},
        {"iat",      iat},
        {"exp",      exp},
        {"tokenExp", exp},
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
    return signing_input + "." + base64url(mac, mac_len);
}

std::string auth_result_name(AuthResult r) {
    switch (r) {
        case AUTHRET_SUCCESS: return "SUCCESS";
        case AUTHRET_KEYORSECRETEMPTY: return "KEYORSECRETEMPTY";
        case AUTHRET_KEYORSECRETWRONG: return "KEYORSECRETWRONG";
        case AUTHRET_ACCOUNTNOTSUPPORT: return "ACCOUNTNOTSUPPORT";
        case AUTHRET_ACCOUNTNOTENABLESDK: return "ACCOUNTNOTENABLESDK";
        case AUTHRET_UNKNOWN: return "UNKNOWN";
        case AUTHRET_SERVICE_BUSY: return "SERVICE_BUSY";
        case AUTHRET_NONE: return "NONE (no callback / timeout)";
        case AUTHRET_OVERTIME: return "OVERTIME";
        case AUTHRET_NETWORKISSUE: return "NETWORKISSUE";
        case AUTHRET_CLIENT_INCOMPATIBLE: return "CLIENT_INCOMPATIBLE";
        case AUTHRET_JWTTOKENWRONG: return "JWTTOKENWRONG";
        case AUTHRET_LIMIT_EXCEEDED_EXCEPTION: return "LIMIT_EXCEEDED";
        default: return "code=" + std::to_string(static_cast<int>(r));
    }
}

struct PendingJoin {
    JoinRequest req;
    std::string jwt;
    std::string err;
    bool ok = false;
    bool finished = false;
    bool auth_started = false;
    std::mutex mu;
    std::condition_variable cv;
};

std::mutex g_join_mu;
PendingJoin* g_pending_join = nullptr;

void finish_pending(bool ok, const std::string& err) {
    std::lock_guard<std::mutex> jlk(g_join_mu);
    if (!g_pending_join) return;
    {
        std::lock_guard<std::mutex> lk(g_pending_join->mu);
        g_pending_join->ok = ok;
        g_pending_join->err = err;
        g_pending_join->finished = true;
    }
    g_pending_join->cv.notify_all();
    g_pending_join = nullptr;
}

bool start_meeting_join(const JoinRequest& req, std::string& err) {
    if (!g_meeting_service) { err = "meeting service missing"; return false; }

    JoinParam jp{};
    jp.userType = SDK_UT_WITHOUT_LOGIN;

    auto& wn = jp.param.withoutloginuserJoin;
    wn.meetingNumber = std::stoull(req.meeting_id);
    wn.userName      = req.display_name.c_str();
    wn.psw           = req.password.empty() ? nullptr : req.password.c_str();
    wn.vanityID      = nullptr;
    wn.app_privilege_token = nullptr;
    wn.userZAK       = req.zak.empty() ? nullptr : req.zak.c_str();
    wn.customer_key  = nullptr;
    wn.webinarToken  = req.webinar_token.empty() ? nullptr : req.webinar_token.c_str();
    wn.isVideoOff    = true;
    wn.isAudioOff    = true;
    wn.join_token    = nullptr;
    wn.onBehalfToken = nullptr;
    wn.isMyVoiceInMix = false;

    std::cerr << "[bridge] Join meetingNumber=" << req.meeting_id
              << " webinarToken=" << (req.webinar_token.empty() ? "no" : "yes")
              << " psw=" << (req.password.empty() ? "no" : "yes") << "\n";

    SDKError jr = g_meeting_service->Join(jp);
    if (jr != SDKERR_SUCCESS) {
        err = "Join failed: " + std::to_string(jr);
        return false;
    }
    return true;
}

class AuthListener : public IAuthServiceEvent {
public:
    virtual void onAuthenticationReturn(AuthResult ret) override {
        std::cerr << "[bridge] onAuthenticationReturn: " << auth_result_name(ret) << "\n";
        emit_safe({{"type", "auth_result"}, {"code", static_cast<int>(ret)}});

        if (ret != AUTHRET_SUCCESS) {
            finish_pending(false, "Auth failed: " + auth_result_name(ret));
            return;
        }

        JoinRequest req;
        {
            std::lock_guard<std::mutex> jlk(g_join_mu);
            if (!g_pending_join) return;
            req = g_pending_join->req;
        }

        std::string err;
        if (!start_meeting_join(req, err)) {
            finish_pending(false, err);
            return;
        }
        finish_pending(true, "");
    }
    virtual void onLoginReturnWithReason(LOGINSTATUS, IAccountInfo*, LoginFailReason) override {}
    virtual void onLogout() override {}
    virtual void onZoomIdentityExpired() override {}
    virtual void onZoomAuthIdentityExpired() override {}
};

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
            emit_safe({
                {"type", "participant_joined"},
                {"name", z2s(u->GetUserName())},
                {"user_id", std::to_string(uid)},
                {"role", role_str},
            });
        }
    }
    virtual void onUserLeft(IList<unsigned int>* user_ids, const zchar_t*) override {
        if (!user_ids) return;
        for (int i = 0; i < user_ids->GetCount(); ++i) {
            emit_safe({
                {"type", "participant_left"},
                {"user_id", std::to_string(user_ids->GetItem(i))},
                {"name", ""},
            });
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

class MeetingListener : public IMeetingServiceEvent {
public:
    virtual void onMeetingStatusChanged(MeetingStatus status, int reason) override {
        std::cerr << "[bridge] meeting status=" << static_cast<int>(status)
                  << " reason=" << reason << "\n";
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

AuthListener     g_auth_listener;
MeetingListener  g_meeting_listener;

gboolean on_pump_tick(gpointer) {
    PendingJoin* job = nullptr;
    {
        std::lock_guard<std::mutex> lk(g_join_mu);
        job = g_pending_join;
    }
    if (!job || job->auth_started) return G_SOURCE_CONTINUE;

    const char* key    = std::getenv("ZOOM_SDK_KEY");
    const char* secret = std::getenv("ZOOM_SDK_SECRET");
    if (!key || !secret || !*key || !*secret) {
        finish_pending(false, "ZOOM_SDK_KEY/SECRET env vars not set");
        return G_SOURCE_CONTINUE;
    }
    if (!g_auth_service) {
        finish_pending(false, "auth service missing");
        return G_SOURCE_CONTINUE;
    }

    job->jwt = build_sdk_jwt(key, secret);
    job->auth_started = true;

    std::cerr << "[bridge] SDKAuth starting (key len=" << std::strlen(key) << ")\n";
    AuthContext ctx;
    ctx.jwt_token = job->jwt.c_str();
    SDKError ar = g_auth_service->SDKAuth(ctx);
    if (ar != SDKERR_SUCCESS) {
        finish_pending(false, "SDKAuth call failed: " + std::to_string(ar));
    }
    return G_SOURCE_CONTINUE;
}

}  // namespace

bool init_sdk() {
    InitParam ip;
    ip.strWebDomain    = "https://zoom.us";
    ip.strSupportUrl   = "https://zoom.us";
    ip.strBrandingName = "Heimdall AI Bot";
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
    g_pump_running = true;
    g_loop = g_main_loop_new(nullptr, FALSE);
    g_timeout_add(50, on_pump_tick, nullptr);
    std::cerr << "[bridge] GLib main loop running\n";
    g_main_loop_run(g_loop);
    g_main_loop_unref(g_loop);
    g_loop = nullptr;
    g_pump_running = false;
}

void stop_pump() {
    g_pump_running = false;
    if (g_loop) g_main_loop_quit(g_loop);
}

bool join(const JoinRequest& req, std::string& err) {
    if (!g_initialized) { err = "SDK not initialized"; return false; }
    if (!g_pump_running.load()) { err = "SDK pump not running"; return false; }

    PendingJoin job;
    job.req = req;

    {
        std::lock_guard<std::mutex> lk(g_join_mu);
        if (g_pending_join) {
            err = "another join is already in progress";
            return false;
        }
        g_pending_join = &job;
    }

    std::unique_lock<std::mutex> lk(job.mu);
    if (!job.cv.wait_for(lk, std::chrono::seconds(90), [&]{ return job.finished; })) {
        {
            std::lock_guard<std::mutex> jlk(g_join_mu);
            if (g_pending_join == &job) g_pending_join = nullptr;
        }
        err = "join timed out waiting for SDK auth callback";
        return false;
    }

    err = job.err;
    return job.ok;
}

void leave() {
    if (g_meeting_service) {
        g_meeting_service->Leave(LEAVE_MEETING);
    }
}

}  // namespace Meeting
