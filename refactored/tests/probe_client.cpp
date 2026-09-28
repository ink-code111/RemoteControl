// ============================================================================
//  probe_client.cpp —— 第二阶段端到端探针
//
//  【为什么不用 Python 写这个探针】
//    v1 的 tests/smoke_protocol.py 靠 struct.pack 手搓二进制包，协议一改就得同步
//    改两份定义（C++ 一份、Python 一份），漂移是迟早的事。v2 用了 FlatBuffers，
//    再手搓字节就变成"手工构造偏移量"，出错概率极高且错了很难看出错在哪。
//    这个探针直接链接 rc_common —— 用的就是服务端/客户端同一份 frame.cpp 与
//    proto_codec.cpp。协议改了它自动跟着改，不存在两份定义不一致的问题。
//    它也不需要任何第三方 Python 包（本机网络受限，pip 装 flatbuffers 不一定通）。
//
//  【覆盖的验收点（对应 docs/02-phase2-async-network.md 的验证计划）】
//    1) 握手：Hello -> HelloAck，session_id 非 0、版本协商通过
//    2) 心跳：Ping -> Pong，seq 与客户端时间戳原样回带，RTT 可算
//    3) 抓屏：ScreenRequest -> ScreenFrame，PNG 签名正确（GDI+ 编码链路通）
//    4) 粘包：两条帧塞进一次 send，服务端必须切成两条（旧版手工 memmove 的痛点）
//    5) 半包：一条帧拆成两次 send，服务端必须等满再解析
//    6) 未握手先发业务消息 -> 服务端应拒绝并断开
//    7) 协议版本不匹配 -> 先回 HelloAck(accepted=false, 带理由) 再断开
//    8) 帧头魔数非法 -> 服务端应直接断开，不做"尽力解析"
//    9) 多客户端并发：3 条连接同时存活，各自都能拿到屏幕帧
//
//  【用法】
//    rc_probe.exe [host] [port] [only] [save-frame]
//       only       只跑某一项：handshake/heartbeat/screen/coalesce/split/nohandshake/
//                  version/badheader/multi/churn/churn_ns，缺省 all
//       save-frame 把 "3) 抓屏" 收到的帧原始字节写到该路径（可选）
//    默认 127.0.0.1 9999
//    退出码：0 全部通过；非 0 为失败项数量（上限 127）
//
//  【超时策略】
//    每条连接配一个看门狗线程，到点强制 close socket —— 阻塞中的 read 会被唤醒
//    并报错返回。这样"服务端卡住不回"不会让探针永久挂死（CI 里最怕这个）。
//    注意：跨线程 close socket 严格说不是 asio 保证的用法，但对一个只跑几秒的
//    测试进程来说，这是最省事又能可靠兜住超时的办法。
// ============================================================================

#include "asio_common.hpp"
#include "frame.hpp"
#include "proto_codec.hpp"

#include <atomic>
#include <chrono>
#include <cstdio>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace {

using rc::net::error_code;
using rc::net::tcp;

int g_pass = 0;
int g_fail = 0;

void check(bool ok, const std::string& what, const std::string& detail = std::string()) {
    // 注意必须 .c_str()：printf 是可变参数函数，传 std::string 对象属于未定义行为
    // （MSVC 会报 C4840/C4477，运行期则可能打印出乱码甚至踩坏栈）
    if (ok) {
        ++g_pass;
        std::printf("[PASS] %s\n", what.c_str());
    } else {
        ++g_fail;
        std::printf("[FAIL] %s%s%s\n", what.c_str(), detail.empty() ? "" : " -- ", detail.c_str());
    }
}

std::string g_host = "127.0.0.1";
std::uint16_t g_port = 9999;

/// 非空时把 "3) 抓屏" 用例收到的帧原始字节写到这个路径。
/// 用途：服务端画面出问题时（光标没画上、某个区域花屏……）把服务端真正
/// 发出来的 PNG 拿到手，用外部工具解码比对——光看日志没法判断画面内容。
std::string g_save_frame;

/// PNG 文件签名：GDI+ 编码出来的屏幕帧必须以此开头
constexpr std::uint8_t kPngSig[8] = {0x89, 'P', 'N', 'G', '\r', '\n', 0x1A, '\n'};

// ---------------------------------------------------------------------------
//  一条探测连接
// ---------------------------------------------------------------------------

class Conn {
public:
    explicit Conn(int watchdog_ms) { start_watchdog(watchdog_ms); }
    ~Conn() {
        stop_watchdog();
        error_code ig;
        sock_.close(ig);
    }

    Conn(const Conn&)            = delete;
    Conn& operator=(const Conn&) = delete;

    bool connect(std::string& why) {
        error_code       ec;
        tcp::resolver    resolver(io_);
        const auto       eps = resolver.resolve(g_host, std::to_string(g_port), ec);
        if (ec) {
            why = "resolve " + g_host + ": " + rc::net::describe(ec);
            return false;
        }
        asio::connect(sock_, eps, ec);
        if (ec) {
            why = "connect: " + rc::net::describe(ec);
            return false;
        }
        return true;
    }

    /// 把一条 Envelope 连帧头一起发出
    bool send(const flatbuffers::DetachedBuffer& body, std::string& why) {
        const auto bytes = rc::net::pack_frame(body.data(), body.size());
        return send_raw(bytes.data(), bytes.size(), why);
    }

    bool send_raw(const std::uint8_t* data, std::size_t n, std::string& why) {
        error_code ec;
        asio::write(sock_, asio::buffer(data, n), ec);
        if (ec) {
            why = "write: " + rc::net::describe(ec);
            return false;
        }
        return true;
    }

    /// 收一帧并做完整的 Verifier 校验
    bool recv(rc::proto::ParsedEnvelope& out, std::vector<std::uint8_t>& storage, std::string& why) {
        std::uint8_t hdr[rc::net::kFrameHeaderSize] = {};
        if (!read_exact(hdr, sizeof(hdr), why)) {
            return false;
        }
        const auto decoded = rc::net::decode_header(hdr);
        if (!decoded.ok()) {
            why = std::string("bad frame header: ") + rc::net::to_string(decoded.error);
            return false;
        }
        storage.resize(decoded.header.payload_len);
        if (!read_exact(storage.data(), storage.size(), why)) {
            return false;
        }
        if (!rc::proto::parse_envelope(storage.data(), storage.size(), out, why)) {
            why = "parse_envelope: " + why;
            return false;
        }
        return true;
    }

    /// 期望服务端已经主动断开：读到 EOF/连接重置算通过，"还能读到数据"算失败
    bool expect_server_closed(std::string& why) {
        std::uint8_t buf[256] = {};
        error_code   ec;
        asio::read(sock_, asio::buffer(buf), ec);
        if (ec == asio::error::eof || ec == asio::error::connection_reset
            || ec == asio::error::connection_aborted) {
            return true;
        }
        if (ec) {
            // operation_aborted / bad_descriptor 一般是看门狗到点强关了 socket：
            // 说明服务端根本没断，属于失败
            why = "connection stayed open (watchdog fired): " + rc::net::describe(ec);
            return false;
        }
        why = "server sent unexpected data instead of closing";
        return false;
    }

    tcp::socket& socket() { return sock_; }

private:
    bool read_exact(void* dst, std::size_t n, std::string& why) {
        error_code ec;
        asio::read(sock_, asio::buffer(dst, n), ec);
        if (ec) {
            why = "read: " + rc::net::describe(ec);
            return false;
        }
        return true;
    }

    void start_watchdog(int ms) {
        watchdog_ms_ = ms;
        watchdog_    = std::thread([this] {
            constexpr int kStepMs = 20;
            for (int waited = 0; waited < watchdog_ms_ && !done_.load(); waited += kStepMs) {
                std::this_thread::sleep_for(std::chrono::milliseconds(kStepMs));
            }
            if (!done_.load()) {
                error_code ig;
                sock_.close(ig); // 唤醒阻塞中的同步读
            }
        });
    }

    void stop_watchdog() {
        done_ = true;
        if (watchdog_.joinable()) {
            watchdog_.join();
        }
    }

    rc::net::io_context io_;
    tcp::socket         sock_{io_};
    std::atomic<bool>   done_{false};
    int                 watchdog_ms_ = 0;
    std::thread         watchdog_;
};

/// 建连 + 握手，返回一个已进入 kReady 状态的连接
bool open_ready(Conn& c, std::string& why) {
    if (!c.connect(why)) {
        return false;
    }
    if (!c.send(rc::proto::make_hello("probe", "2.0.0", rc::net::kProtocolMajor), why)) {
        return false;
    }
    rc::proto::ParsedEnvelope env;
    std::vector<std::uint8_t> storage;
    if (!c.recv(env, storage, why)) {
        return false;
    }
    if (env.body_type() != rc::proto::v2::Body::HelloAck) {
        why = "expected HelloAck, got " + std::string(rc::proto::to_string(env.body_type()));
        return false;
    }
    if (!env.as_hello_ack()->accepted()) {
        why = "handshake rejected";
        return false;
    }
    return true;
}

// ---------------------------------------------------------------------------
//  各测试项
// ---------------------------------------------------------------------------

void test_handshake() {
    Conn c(10000);
    std::string why;
    if (!c.connect(why)) {
        check(false, "1) 握手：TCP 连接", why);
        return;
    }
    check(true, "1) 握手：TCP 连接建立");

    if (!c.send(rc::proto::make_hello("probe", "2.0.0", rc::net::kProtocolMajor), why)) {
        check(false, "1) 握手：发送 Hello", why);
        return;
    }
    check(true, "1) 握手：发送 Hello");

    rc::proto::ParsedEnvelope env;
    std::vector<std::uint8_t> storage;
    if (!c.recv(env, storage, why)) {
        check(false, "1) 握手：接收 HelloAck", why);
        return;
    }
    if (env.body_type() != rc::proto::v2::Body::HelloAck) {
        check(false, "1) 握手：收到 HelloAck",
              std::string("实际类型 = ") + rc::proto::to_string(env.body_type()));
        return;
    }
    const auto* ack = env.as_hello_ack();
    check(ack->accepted(), "1) 握手：服务端接受连接");
    check(ack->session_id() != 0,
          "1) 握手：分配了非 0 的 session_id",
          "session_id=" + std::to_string(ack->session_id()));
    check(ack->protocol_version() == rc::net::kProtocolMajor,
          "1) 握手：协议版本协商一致",
          "server=" + std::to_string(ack->protocol_version()) +
              " local=" + std::to_string(rc::net::kProtocolMajor));
}

void test_heartbeat() {
    Conn        c(10000);
    std::string why;
    if (!open_ready(c, why)) {
        check(false, "2) 心跳：建立已握手连接", why);
        return;
    }

    constexpr std::uint32_t kSeq = 12345;
    const std::int64_t      t0   = rc::net::now_ms();
    if (!c.send(rc::proto::make_ping(kSeq, t0), why)) {
        check(false, "2) 心跳：发送 Ping", why);
        return;
    }

    rc::proto::ParsedEnvelope env;
    std::vector<std::uint8_t> storage;
    if (!c.recv(env, storage, why)) {
        check(false, "2) 心跳：接收 Pong", why);
        return;
    }
    if (env.body_type() != rc::proto::v2::Body::Pong) {
        check(false, "2) 心跳：收到 Pong",
              std::string("实际类型 = ") + rc::proto::to_string(env.body_type()));
        return;
    }
    const auto* pong = env.as_pong();
    check(pong->seq() == kSeq, "2) 心跳：Pong 序号与 Ping 一致",
          "seq=" + std::to_string(pong->seq()));
    check(pong->client_time_ms() == t0, "2) 心跳：客户端时间戳原样回带（RTT 可算）",
          "got=" + std::to_string(pong->client_time_ms()));

    const std::int64_t rtt = rc::net::now_ms() - t0;
    check(rtt >= 0 && rtt < 5000, "2) 心跳：RTT 在合理范围",
          "rtt=" + std::to_string(rtt) + "ms");
}

void test_screen_frame() {
    Conn        c(15000);
    std::string why;
    if (!open_ready(c, why)) {
        check(false, "3) 抓屏：建立已握手连接", why);
        return;
    }

    if (!c.send(rc::proto::make_screen_request(1280, 80), why)) {
        check(false, "3) 抓屏：发送 ScreenRequest", why);
        return;
    }

    rc::proto::ParsedEnvelope env;
    std::vector<std::uint8_t> storage;
    if (!c.recv(env, storage, why)) {
        check(false, "3) 抓屏：接收 ScreenFrame", why);
        return;
    }
    if (env.body_type() != rc::proto::v2::Body::ScreenFrame) {
        check(false, "3) 抓屏：收到 ScreenFrame",
              std::string("实际类型 = ") + rc::proto::to_string(env.body_type()));
        return;
    }

    const auto* f = env.as_screen_frame();
    check(f->width() > 0 && f->height() > 0, "3) 抓屏：帧尺寸有效",
          std::to_string(f->width()) + "x" + std::to_string(f->height()));

    const auto* data = f->data();
    const bool  big  = data != nullptr && data->size() > 1024;
    check(big, "3) 抓屏：图像数据非空",
          data ? (std::to_string(data->size()) + " 字节") : std::string("data=null"));

    bool png = false;
    if (data != nullptr && data->size() >= sizeof(kPngSig)) {
        png = true;
        for (std::size_t i = 0; i < sizeof(kPngSig); ++i) {
            // FlatBuffers 的 Get() 参数是 uoffset_t（32 位），显式转换以消 C4267
            if (data->Get(static_cast<flatbuffers::uoffset_t>(i)) != kPngSig[i]) {
                png = false;
                break;
            }
        }
    }
    check(png, "3) 抓屏：PNG 签名正确（GDI+ 编码链路通）");
    check(f->format() == rc::proto::v2::ImageFormat::Png, "3) 抓屏：format 字段为 Png");

    // 把服务端真正发出来的字节落盘，便于外部逐像素核对画面内容
    if (!g_save_frame.empty() && data != nullptr) {
        std::FILE* fp = nullptr;
        // 用 fopen_s：本项目编译带 /sdl，fopen 的 C4996 在这里是错误不是警告
        if (::fopen_s(&fp, g_save_frame.c_str(), "wb") == 0 && fp != nullptr) {
            const std::size_t wrote =
                std::fwrite(data->data(), 1, static_cast<std::size_t>(data->size()), fp);
            std::fclose(fp);
            std::printf("       [save-frame] %s (%zu 字节, 写入 %zu)\n", g_save_frame.c_str(),
                        static_cast<std::size_t>(data->size()), wrote);
        } else {
            std::printf("       [save-frame] 无法写入 %s\n", g_save_frame.c_str());
        }
    }
}

/// 把两条帧拼成一次 send —— 服务端必须靠帧头切流，不能把两条揉成一条
void test_coalesced_frames() {
    Conn        c(10000);
    std::string why;
    if (!c.connect(why)) {
        check(false, "4) 粘包：TCP 连接", why);
        return;
    }

    const auto hello = rc::net::pack_frame(
        rc::proto::make_hello("probe", "2.0.0", rc::net::kProtocolMajor).data(),
        rc::proto::make_hello("probe", "2.0.0", rc::net::kProtocolMajor).size());
    // 上面那种写法会构造两次，语义上没错但浪费；这里重写清楚一点：
    const auto hello_body = rc::proto::make_hello("probe", "2.0.0", rc::net::kProtocolMajor);
    const auto hello_pkt  = rc::net::pack_frame(hello_body.data(), hello_body.size());

    constexpr std::uint32_t kSeq = 777;
    const auto ping_body = rc::proto::make_ping(kSeq, rc::net::now_ms());
    const auto ping_pkt  = rc::net::pack_frame(ping_body.data(), ping_body.size());

    // 两帧首尾相接，一次 write 发出
    std::vector<std::uint8_t> both;
    both.reserve(hello_pkt.size() + ping_pkt.size());
    both.insert(both.end(), hello_pkt.begin(), hello_pkt.end());
    both.insert(both.end(), ping_pkt.begin(), ping_pkt.end());

    if (!c.send_raw(both.data(), both.size(), why)) {
        check(false, "4) 粘包：一次发出两条帧", why);
        return;
    }

    rc::proto::ParsedEnvelope    env;
    std::vector<std::uint8_t>    storage;
    if (!c.recv(env, storage, why) || env.body_type() != rc::proto::v2::Body::HelloAck) {
        check(false, "4) 粘包：拆出第一条（HelloAck）", why);
        return;
    }
    check(true, "4) 粘包：拆出第一条（HelloAck）");

    if (!c.recv(env, storage, why) || env.body_type() != rc::proto::v2::Body::Pong) {
        check(false, "4) 粘包：拆出第二条（Pong）", why);
        return;
    }
    check(env.as_pong()->seq() == kSeq, "4) 粘包：第二条内容正确（Pong.seq 匹配）");
}

/// 一条帧拆成两次 send —— 服务端必须"读满才解析"，不能半截就当一条
void test_split_frame() {
    Conn        c(10000);
    std::string why;
    if (!c.connect(why)) {
        check(false, "5) 半包：TCP 连接", why);
        return;
    }

    const auto body = rc::proto::make_hello("probe", "2.0.0", rc::net::kProtocolMajor);
    const auto pkt  = rc::net::pack_frame(body.data(), body.size());
    if (pkt.size() < 12) {
        check(false, "5) 半包：包长度异常", std::to_string(pkt.size()));
        return;
    }

    // 前 5 字节只够半个帧头，服务端此时必须还在等
    if (!c.send_raw(pkt.data(), 5, why)) {
        check(false, "5) 半包：发送前半段", why);
        return;
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(150));
    if (!c.send_raw(pkt.data() + 5, pkt.size() - 5, why)) {
        check(false, "5) 半包：发送后半段", why);
        return;
    }

    rc::proto::ParsedEnvelope env;
    std::vector<std::uint8_t> storage;
    if (!c.recv(env, storage, why) || env.body_type() != rc::proto::v2::Body::HelloAck) {
        check(false, "5) 半包：服务端拼齐后才解析", why);
        return;
    }
    check(env.as_hello_ack()->accepted(), "5) 半包：服务端拼齐后才解析");
}

/// 握手前发业务消息：服务端应拒绝并断开
void test_handshake_required() {
    Conn        c(8000);
    std::string why;
    if (!c.connect(why)) {
        check(false, "6) 握手前置校验：TCP 连接", why);
        return;
    }
    if (!c.send(rc::proto::make_ping(1, rc::net::now_ms()), why)) {
        check(false, "6) 握手前置校验：发送 Ping", why);
        return;
    }
    check(c.expect_server_closed(why), "6) 握手前置校验：未 Hello 先发消息被断开", why);
}

/// 版本不匹配：应先回一条 accepted=false 的 HelloAck，再断开
void test_version_mismatch() {
    Conn        c(8000);
    std::string why;
    if (!c.connect(why)) {
        check(false, "7) 版本协商：TCP 连接", why);
        return;
    }
    if (!c.send(rc::proto::make_hello("probe-old", "0.9.0", 99), why)) {
        check(false, "7) 版本协商：发送 v99 Hello", why);
        return;
    }

    rc::proto::ParsedEnvelope env;
    std::vector<std::uint8_t> storage;
    if (!c.recv(env, storage, why) || env.body_type() != rc::proto::v2::Body::HelloAck) {
        check(false, "7) 版本协商：收到 HelloAck", why);
        return;
    }
    const auto* ack = env.as_hello_ack();
    check(!ack->accepted(), "7) 版本协商：accepted=false");
    check(ack->reject_reason() != nullptr && !ack->reject_reason()->str().empty(),
          "7) 版本协商：拒绝理由带回给客户端",
          ack->reject_reason() ? ack->reject_reason()->str() : std::string("<null>"));
    check(c.expect_server_closed(why), "7) 版本协商：回完理由后断开", why);
}

/// 帧头魔数不对：服务端应直接断开，不尝试解析
void test_bad_frame_header() {
    Conn        c(8000);
    std::string why;
    if (!c.connect(why)) {
        check(false, "8) 非法帧头：TCP 连接", why);
        return;
    }
    const std::uint8_t junk[8] = {0xDE, 0xAD, 0xBE, 0xEF, 0x10, 0x00, 0x00, 0x00};
    if (!c.send_raw(junk, sizeof(junk), why)) {
        check(false, "8) 非法帧头：发送垃圾帧头", why);
        return;
    }
    check(c.expect_server_closed(why), "8) 非法帧头：服务端拒绝并断开", why);
}

/// 连续 N 轮「连接 -> 握手 -> 取一帧 -> 关闭」。
///
/// 【为什么单独有这一项】
///    它能区分两类截然不同的问题：
///      · 如果是"第 N 个连接开始出问题"（累计型），失败点会稳定出现在同一轮；
///      · 如果是"多连接同时存活"才有问题（并发型），单条连着的 churn 会全绿。
///    排查连接类问题时，"能稳定说清是第几次失败"比任何猜测都值钱。
/// with_screen = false 时只握手不取帧，用来把「抓屏」这个变量单独隔离出来。
void test_connection_churn(int rounds, bool with_screen) {
    const std::string tag = with_screen ? "10) 连接抖动" : "11) 连接抖动(不抓屏)";
    for (int i = 0; i < rounds; ++i) {
        Conn        c(8000);
        std::string why;
        if (!c.connect(why)) {
            check(false, tag + "：第 " + std::to_string(i + 1) + " 轮 TCP 连接", why);
            return;
        }
        rc::proto::ParsedEnvelope    env;
        std::vector<std::uint8_t>    storage;
        if (!c.send(rc::proto::make_hello("probe", "2.0.0", rc::net::kProtocolMajor), why)
            || !c.recv(env, storage, why) || env.body_type() != rc::proto::v2::Body::HelloAck) {
            check(false, tag + "：第 " + std::to_string(i + 1) + " 轮握手", why);
            return;
        }
        if (with_screen) {
            if (!c.send(rc::proto::make_screen_request(640, 50), why) || !c.recv(env, storage, why)
                || env.body_type() != rc::proto::v2::Body::ScreenFrame) {
                check(false, tag + "：第 " + std::to_string(i + 1) + " 轮取帧", why);
                return;
            }
        }
    }
    check(true, tag + "：" + std::to_string(rounds) + " 轮全部成功");
}

/// 多条连接同时存活，各自都能正常工作（第一阶段完全做不到）
void test_multi_clients() {
    constexpr int kClients = 3;
    std::vector<std::unique_ptr<Conn>> conns;
    conns.reserve(kClients);

    std::string why;
    bool        all_ready = true;
    for (int i = 0; i < kClients; ++i) {
        auto c = std::make_unique<Conn>(15000);
        if (!open_ready(*c, why)) {
            check(false, "9) 多客户端：第 " + std::to_string(i + 1) + " 条连接握手", why);
            all_ready = false;
            break;
        }
        conns.push_back(std::move(c));
    }
    if (!all_ready) {
        return;
    }
    check(true, "9) 多客户端：3 条连接同时握手成功");

    int frames = 0;
    for (auto& c : conns) {
        if (!c->send(rc::proto::make_screen_request(1024, 80), why)) {
            continue;
        }
        rc::proto::ParsedEnvelope    env;
        std::vector<std::uint8_t>    storage;
        if (c->recv(env, storage, why) && env.body_type() == rc::proto::v2::Body::ScreenFrame
            && env.as_screen_frame()->data() != nullptr) {
            ++frames;
        }
    }
    check(frames == kClients, "9) 多客户端：每条连接都拿到了屏幕帧",
          std::to_string(frames) + "/" + std::to_string(kClients));
}

} // namespace

int main(int argc, char** argv) {
    if (argc > 1) {
        g_host = argv[1];
    }
    if (argc > 2) {
        g_port = static_cast<std::uint16_t>(std::stoi(argv[2]));
    }
    // 第三个参数可以只跑某一项，便于把问题缩到最小：
    //   rc_probe.exe 127.0.0.1 9999 multi
    const std::string only = argc > 3 ? argv[3] : "all";
    auto              want = [&only](const char* name) { return only == "all" || only == name; };
    // 第四个参数可选：把 "3) 抓屏" 收到的帧存成文件，供外部解码核对画面
    if (argc > 4) {
        g_save_frame = argv[4];
    }

    std::printf("== rc probe (phase 2: asio async + flatbuffers) -> %s:%u  only=%s ==\n\n",
                g_host.c_str(), static_cast<unsigned>(g_port), only.c_str());

    if (want("handshake"))    test_handshake();
    if (want("heartbeat"))    test_heartbeat();
    if (want("screen"))       test_screen_frame();
    if (want("coalesce"))     test_coalesced_frames();
    if (want("split"))        test_split_frame();
    if (want("nohandshake"))  test_handshake_required();
    if (want("version"))      test_version_mismatch();
    if (want("badheader"))    test_bad_frame_header();
    if (want("multi"))        test_multi_clients();
    if (want("churn"))        test_connection_churn(12, true);
    if (want("churn_ns"))     test_connection_churn(12, false);

    std::printf("\n== 汇总：%d 通过, %d 失败 ==\n", g_pass, g_fail);
    return g_fail > 127 ? 127 : g_fail;
}
