/**
 * robot_agent.cpp
 *
 * C++ DDS bridge for Jarvis NLP pipeline.
 * Runs on AGX Thor, handles all Unitree SDK/DDS calls so Python never touches DDS.
 *
 * TCP :7788  — gesture commands  (JSON: {"gesture":"wave_hello"})
 * TCP :7789  — audio PCM stream  (protocol: 4-byte LE length + PCM bytes; length=0 → PlayStop)
 *
 * On startup:
 *   1. Init DDS (ChannelFactory)
 *   2. Activate robot mic via Voice API 1008 mode=1
 *   3. Init AudioClient (PlayStream / SetVolume)
 *   4. Init LocoClient (WaveHand / ShakeHand / Move)
 *   5. Serve gesture + audio TCP servers
 *
 * Usage: robot_agent <network_interface>
 *   e.g: robot_agent enP2p1s0
 */

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include <atomic>
#include <chrono>
#include <cstring>
#include <iostream>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include <unitree/robot/channel/channel_factory.hpp>
#include <unitree/robot/client/client.hpp>
#include <unitree/robot/g1/audio/g1_audio_client.hpp>
#include <unitree/robot/g1/loco/g1_loco_client.hpp>

// ─── Ports ───────────────────────────────────────────────────────────────────
static constexpr int GESTURE_PORT = 7788;
static constexpr int AUDIO_PORT   = 7789;
static constexpr int CHUNK_SIZE   = 96000;  // 3 s @ 16kHz mono int16

// ─── Globals ─────────────────────────────────────────────────────────────────
static unitree::robot::g1::AudioClient* g_audio = nullptr;
static std::mutex g_audio_mutex;
// LocoClient is NOT kept alive — it is created per-gesture and destroyed
// immediately after so the robot's loco_service drops back to joystick mode.

// ─── Voice service client (mic activation) ───────────────────────────────────
class VoiceClient : public unitree::robot::Client {
public:
    VoiceClient() : unitree::robot::Client("voice", false) {}

    void Init() {
        SetApiVersion("1.0.0.0");
        RegistApi(1008, 0);
    }

    int32_t SetMode(int mode) {
        std::string param = "{\"mode\":" + std::to_string(mode) + "}";
        return Call(1008, param);
    }
};

// ─── Helpers ─────────────────────────────────────────────────────────────────

// Read exactly n bytes from fd; return false on EOF/error.
static bool read_exact(int fd, void* buf, size_t n) {
    char* p = static_cast<char*>(buf);
    size_t remaining = n;
    while (remaining > 0) {
        ssize_t r = recv(fd, p, remaining, 0);
        if (r <= 0) return false;
        p += r;
        remaining -= r;
    }
    return true;
}

// Send exactly n bytes to fd; return false on error.
static bool send_exact(int fd, const void* buf, size_t n) {
    const char* p = static_cast<const char*>(buf);
    size_t remaining = n;
    while (remaining > 0) {
        ssize_t s = send(fd, p, remaining, MSG_NOSIGNAL);
        if (s <= 0) return false;
        p += s;
        remaining -= s;
    }
    return true;
}

// Simple gesture name extraction from JSON {"gesture":"NAME"}
static std::string parse_gesture(const std::string& json) {
    auto key = json.find("\"gesture\"");
    if (key == std::string::npos) return "";
    auto colon = json.find(':', key);
    if (colon == std::string::npos) return "";
    auto q1 = json.find('"', colon + 1);
    if (q1 == std::string::npos) return "";
    auto q2 = json.find('"', q1 + 1);
    if (q2 == std::string::npos) return "";
    return json.substr(q1 + 1, q2 - q1 - 1);
}

// ─── Gesture server ───────────────────────────────────────────────────────────
static void handle_gesture_client(int fd) {
    char buf[256];
    while (true) {
        ssize_t n = recv(fd, buf, sizeof(buf) - 1, 0);
        if (n <= 0) break;
        buf[n] = '\0';
        std::string msg(buf);
        std::string gesture = parse_gesture(msg);

        if (gesture.empty()) {
            std::cerr << "[GESTURE] Bad JSON: " << msg << "\n";
            continue;
        }

        std::cout << "[GESTURE] ▶ " << gesture << "\n";
        int32_t ret = 0;

        // Create LocoClient only for the duration of the gesture so the
        // loco_service releases back to joystick mode when we're done.
        unitree::robot::g1::LocoClient loco;
        loco.Init();
        loco.SetTimeout(10.0f);

        if (gesture == "wave_hello") {
            ret = loco.WaveHand(false);
        } else if (gesture == "wave_goodbye") {
            ret = loco.WaveHand(true);
        } else if (gesture == "shake_hand") {
            ret = loco.ShakeHand(0);  // extend
            std::this_thread::sleep_for(std::chrono::milliseconds(1000));
            ret = loco.ShakeHand(1);  // retract
        } else if (gesture == "move_forward") {
            ret = loco.Move(0.3f, 0.0f, 0.0f, false);
        } else if (gesture == "move_backward") {
            ret = loco.Move(-0.3f, 0.0f, 0.0f, false);
        } else {
            std::cout << "[GESTURE] Unknown: " << gesture << " — ignored\n";
        }

        std::cout << "[GESTURE] ✓ " << gesture << " ret=" << ret << "\n";
    }
    close(fd);
}

static void gesture_server() {
    int srv = socket(AF_INET, SOCK_STREAM, 0);
    int opt = 1;
    setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = INADDR_ANY;
    addr.sin_port = htons(GESTURE_PORT);
    bind(srv, reinterpret_cast<sockaddr*>(&addr), sizeof(addr));
    listen(srv, 4);
    std::cout << "[GESTURE] TCP server listening on :" << GESTURE_PORT << "\n";

    while (true) {
        int client_fd = accept(srv, nullptr, nullptr);
        if (client_fd < 0) continue;
        std::thread(handle_gesture_client, client_fd).detach();
    }
}

// ─── Audio server ─────────────────────────────────────────────────────────────
static void handle_audio_client(int fd) {
    std::string stream_id =
        "jarvis_" + std::to_string(
            std::chrono::duration_cast<std::chrono::milliseconds>(
                std::chrono::steady_clock::now().time_since_epoch()
            ).count());

    std::cout << "[AUDIO] New stream: " << stream_id << "\n";
    bool started = false;

    while (true) {
        uint32_t len_le;
        if (!read_exact(fd, &len_le, 4)) break;

        uint32_t len = le32toh(len_le);

        if (len == 0) {
            // End-of-speech signal
            if (started) {
                std::lock_guard<std::mutex> lk(g_audio_mutex);
                g_audio->PlayStop("jarvis_brain");
                std::cout << "[AUDIO] PlayStop sent\n";
            }
            break;
        }

        std::vector<uint8_t> pcm(len);
        if (!read_exact(fd, pcm.data(), len)) break;

        {
            std::lock_guard<std::mutex> lk(g_audio_mutex);
            int32_t ret = g_audio->PlayStream("jarvis_brain", stream_id, pcm);
            if (!started) {
                std::cout << "[AUDIO] First chunk sent, ret=" << ret << "\n";
                started = true;
            }
        }
    }

    if (started) {
        std::lock_guard<std::mutex> lk(g_audio_mutex);
        g_audio->PlayStop("jarvis_brain");
    }

    close(fd);
    std::cout << "[AUDIO] Stream " << stream_id << " done\n";
}

static void audio_server() {
    int srv = socket(AF_INET, SOCK_STREAM, 0);
    int opt = 1;
    setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = INADDR_ANY;
    addr.sin_port = htons(AUDIO_PORT);
    bind(srv, reinterpret_cast<sockaddr*>(&addr), sizeof(addr));
    listen(srv, 4);
    std::cout << "[AUDIO] TCP server listening on :" << AUDIO_PORT << "\n";

    while (true) {
        int client_fd = accept(srv, nullptr, nullptr);
        if (client_fd < 0) continue;
        std::thread(handle_audio_client, client_fd).detach();
    }
}

// ─── Main ─────────────────────────────────────────────────────────────────────
int main(int argc, char* argv[]) {
    if (argc < 2) {
        std::cerr << "Usage: robot_agent <network_interface>\n"
                  << "  e.g: robot_agent enP2p1s0\n";
        return 1;
    }
    const std::string iface = argv[1];

    std::cout << "[AGENT] Initializing DDS on " << iface << "...\n";
    unitree::robot::ChannelFactory::Instance()->Init(0, iface);
    std::cout << "[AGENT] DDS ready\n";

    // Init audio client
    g_audio = new unitree::robot::g1::AudioClient();
    g_audio->Init();
    g_audio->SetTimeout(10.0f);
    g_audio->SetVolume(100);
    std::cout << "[AGENT] AudioClient ready\n";

    std::cout << "[AGENT] LocoClient: lazy init per gesture (joystick stays active)\n";

    // Activate mic streaming (mode=1)
    try {
        VoiceClient vc;
        vc.Init();
        vc.SetTimeout(5.0f);
        int32_t ret = vc.SetMode(1);
        std::cout << "[AGENT] Mic activated (mode=1), ret=" << ret << "\n";
    } catch (const std::exception& e) {
        std::cerr << "[AGENT] Mic activation failed: " << e.what()
                  << " — mic may not stream\n";
    }

    // Start TCP servers in background threads
    std::thread(gesture_server).detach();
    std::thread(audio_server).detach();

    std::cout << "[AGENT] Ready. Ctrl+C to exit.\n";

    while (true) {
        std::this_thread::sleep_for(std::chrono::seconds(60));
    }

    return 0;
}
