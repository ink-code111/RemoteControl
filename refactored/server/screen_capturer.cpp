#include "screen_capturer.hpp"

#include "capture_internal.hpp"
#include "logger.hpp"
#include "win_raii.hpp"

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <Windows.h>
#include <atlimage.h>

#include <chrono>

namespace rc::server {

namespace {

inline double ms_between(const std::chrono::steady_clock::time_point& begin,
                         const std::chrono::steady_clock::time_point& end) {
    return std::chrono::duration<double, std::milli>(end - begin).count();
}

} // namespace

GdiScreenCapturer::GdiScreenCapturer(bool capture_cursor, bool enable_delta,
                                     std::uint32_t keyframe_interval)
    : DeltaCapturerBase(enable_delta, keyframe_interval) {
    capture_cursor_ = capture_cursor;
}

bool GdiScreenCapturer::grab(ATL::CImage& cur, GrabTiming& t) {
    // 【尺寸只能在这里取】
    //   GetSystemMetrics 的结果取决于**调用线程的 DPI 上下文**：
    //   在 unaware 下是 1707×960，在 aware 下是 2560×1440（§6.12/§6.13 实测）。
    //   所以"服务端能抓到多大一块桌面"这件事，由这一句 + 服务端的 DPI 状态共同决定。
    const int width  = ::GetSystemMetrics(SM_CXSCREEN);
    const int height = ::GetSystemMetrics(SM_CYSCREEN);
    if (width <= 0 || height <= 0) {
        RC_LOG_ERROR("GetSystemMetrics failed");
        return false;
    }

    // 固定 32bpp：摆脱对桌面 BITSPIXEL 的依赖，PNG 编码路径稳定
    if (cur.Create(width, height, 32, 0) != 1) {
        RC_LOG_ERROR("CImage::Create failed");
        return false;
    }

    const auto t_begin = std::chrono::steady_clock::now();

    const auto      t_dc0 = std::chrono::steady_clock::now();
    detail::ImageDc cur_dc(cur);
    if (cur_dc.get() == nullptr) {
        RC_LOG_ERROR("CImage::GetDC failed");
        return false;
    }
    // RAII：屏幕 DC 由 unique_ptr 自动释放，任何 return 路径都不泄漏
    win::ScreenDcPtr screen_dc(::GetDC(nullptr));
    if (!screen_dc) {
        RC_LOG_ERROR("GetDC failed");
        return false;
    }
    const auto t_dc1 = std::chrono::steady_clock::now();

    if (!::BitBlt(cur_dc.get(), 0, 0, width, height, screen_dc.get(), 0, 0, SRCCOPY)) {
        RC_LOG_ERROR("BitBlt failed");
        return false;
    }
    const auto t_blit1 = std::chrono::steady_clock::now();

    // 光标必须在 ReleaseDC 之前合成：DrawIconEx 要画进同一个位图的 DC，
    // 而且必须在比对之前（否则光标移动不会被判成脏区 —— 完整论证见
    // capture_internal.hpp 里 composite_system_cursor 的注释）。
    // 这里复用已经取好的 cur_dc，而不是走基类的 composite_cursor(cur)（那会再取一次 DC）：
    // 多一次 DC 取放虽然无害，但会让"光标"这一段的历史数字不再可比。
    if (capture_cursor_) {
        detail::composite_system_cursor(cur_dc.get());
    }
    const auto t_cur1 = std::chrono::steady_clock::now();

    screen_dc.reset(); // 屏幕 DC 用完即放，差异检测与裁剪都不需要它
    const auto t_rel1 = std::chrono::steady_clock::now();

    t.prep_ms    = ms_between(t_dc0, t_dc1);
    t.move_ms    = ms_between(t_dc1, t_blit1);
    t.cursor_ms  = ms_between(t_blit1, t_cur1);
    t.release_ms = ms_between(t_cur1, t_rel1);
    t.total_ms   = ms_between(t_begin, t_rel1);
    return true;
}

} // namespace rc::server
