// main.cpp — entry point for the zoom-bridge daemon.
//
// Wires up the HTTP server (cpp-httplib) to the meeting + chat controllers.
// Runs the Zoom SDK message pump on a dedicated thread because the SDK uses
// blocking event loops internally.
//
// Build: see bridge/CMakeLists.txt

#include "httplib.h"
#include "meeting.h"
#include "chat.h"

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <deque>
#include <iostream>
#include <mutex>
#include <nlohmann/json.hpp>  // OR write tiny json by hand if you don't want this dep
#include <string>
#include <thread>

using json = nlohmann::json;

namespace {

// ----- Shared event queue (bridge -> SSE clients) ---------------------

struct EventBus {
    std::mutex mu;
    std::condition_variable cv;
    std::deque<std::string> events;        // each is one JSON line
    std::atomic<bool> stopped{false};

    void push(const std::string& json_str) {
        {
            std::lock_guard<std::mutex> lk(mu);
            events.push_back(json_str);
            // Bound the queue so a slow consumer can't OOM us
            while (events.size() > 1024) events.pop_front();
        }
        cv.notify_all();
    }

    // Blocking pop with timeout
    bool pop(std::string& out, std::chrono::milliseconds timeout) {
        std::unique_lock<std::mutex> lk(mu);
        if (!cv.wait_for(lk, timeout, [&]{ return !events.empty() || stopped.load(); }))
            return false;
        if (events.empty()) return false;
        out = std::move(events.front());
        events.pop_front();
        return true;
    }
};

EventBus g_bus;

// Called by meeting.cpp / chat.cpp when the SDK fires an event
void EmitEvent(const json& j) {
    g_bus.push(j.dump());
}

// ----- Tiny helpers ----------------------------------------------------

std::string body_str(const httplib::Request& req) { return req.body; }

json parse_or_empty(const std::string& s) {
    try { return json::parse(s); } catch (...) { return json::object(); }
}

void json_response(httplib::Response& res, int code, const json& j) {
    res.status = code;
    res.set_content(j.dump(), "application/json");
}

}  // namespace

int main(int argc, char** argv) {
    const char* host = std::getenv("BRIDGE_BIND") ? std::getenv("BRIDGE_BIND") : "127.0.0.1";
    int port = 8765;
    if (const char* p = std::getenv("BRIDGE_PORT")) port = std::atoi(p);

    // Init the Zoom SDK once at startup. Auth happens on /join.
    if (!Meeting::init_sdk()) {
        std::cerr << "[bridge] failed to init Zoom SDK\n";
        return 1;
    }

    // Hook the SDK's events to our bus
    Meeting::set_event_emitter(&EmitEvent);
    Chat::set_event_emitter(&EmitEvent);

    // Run the SDK's blocking message pump on its own thread
    std::thread sdk_pump_thread([]() { Meeting::run_pump(); });

    httplib::Server svr;

    // ----- /health ----------------------------------------------------
    svr.Get("/health", [](const httplib::Request&, httplib::Response& res) {
        json j = {{"status", "ok"}, {"sdk_initialized", Meeting::is_initialized()}};
        json_response(res, 200, j);
    });

    // ----- /join ------------------------------------------------------
    svr.Post("/join", [](const httplib::Request& req, httplib::Response& res) {
        auto j = parse_or_empty(body_str(req));
        Meeting::JoinRequest jr;
        jr.meeting_id     = j.value("meeting_id",     std::string{});
        jr.password       = j.value("password",       std::string{});
        jr.zak            = j.value("zak",            std::string{});
        jr.display_name   = j.value("display_name",   std::string{"Bot"});
        jr.avatar_url     = j.value("avatar_url",     std::string{});
        jr.webinar_token  = j.value("webinar_token",  std::string{});

        if (jr.meeting_id.empty()) {
            return json_response(res, 400, {{"error", "meeting_id required"}});
        }

        std::string err;
        if (!Meeting::join(jr, err)) {
            return json_response(res, 500, {{"error", err}});
        }
        json_response(res, 200, {{"ok", true}});
    });

    // ----- /send_chat -------------------------------------------------
    svr.Post("/send_chat", [](const httplib::Request& req, httplib::Response& res) {
        auto j = parse_or_empty(body_str(req));
        std::string text = j.value("text", "");
        std::string to   = j.value("to", "everyone");
        if (text.empty()) {
            return json_response(res, 400, {{"error", "text required"}});
        }
        std::string err;
        if (!Chat::send(text, to, err)) {
            return json_response(res, 500, {{"error", err}});
        }
        json_response(res, 200, {{"ok", true}});
    });

    // ----- /leave -----------------------------------------------------
    svr.Post("/leave", [](const httplib::Request&, httplib::Response& res) {
        Meeting::leave();
        json_response(res, 200, {{"ok", true}});
    });

    // ----- /events (SSE) ---------------------------------------------
    svr.Get("/events", [](const httplib::Request&, httplib::Response& res) {
        res.set_chunked_content_provider(
            "text/event-stream",
            [](size_t, httplib::DataSink& sink) -> bool {
                std::string line;
                // Block up to 15s for an event; emit a heartbeat otherwise so
                // proxies / load balancers don't kill the connection.
                if (g_bus.pop(line, std::chrono::milliseconds(15000))) {
                    std::string msg = "data: " + line + "\n\n";
                    sink.write(msg.c_str(), msg.size());
                } else {
                    const char* hb = ": keepalive\n\n";
                    sink.write(hb, std::strlen(hb));
                }
                return true;  // keep connection open
            }
        );
    });

    std::cout << "[bridge] listening on " << host << ":" << port << "\n";
    svr.listen(host, port);

    // Shutdown
    Meeting::leave();
    g_bus.stopped = true;
    g_bus.cv.notify_all();
    Meeting::stop_pump();
    if (sdk_pump_thread.joinable()) sdk_pump_thread.join();
    Meeting::cleanup_sdk();
    return 0;
}
