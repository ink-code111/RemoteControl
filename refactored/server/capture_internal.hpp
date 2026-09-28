#pragma once
// ============================================================
// 抓屏后端之间的内部共享件
//
// 只给 delta_capturer.cpp 与各后端 .cpp 使用，**不对外暴露** ——
// asio_server / session 只该看见 IScreenSource，不该被拖进 ATL 与 GDI 类型。
//
// 为什么这两个东西要单独抽出来，而不是各自写一份：
//   · ImageDc：CImage::GetDC 拿到的 DC 必须手工 ReleaseDC，而抓屏路径上有好几条
//     提前 return 的分支 —— 漏一次就是每帧泄漏一个 DC。RAII 只该写对一次。
//   · composite_system_cursor：光标合成的两个易错点（GetCursorInfo 要先填 cbSize、
//     DrawIconEx 要用热点偏移）不该在两个后端里各写一遍。
//     如果某个后端忘了减热点偏移，症状是"光标整体偏几个像素"——肉眼几乎看不出来，
//     而它只在一个后端上出现，排查起来会被归因成"那个后端的问题"。
// ============================================================

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <Windows.h>

#include "logger.hpp"
#include "win_raii.hpp"

#include <atlimage.h>

namespace rc::server::detail {

/// CImage 内部 DC 的配对释放。
///
/// 用法：`ImageDc dc(img);` —— 构造时 img.GetDC()，析构时 img.ReleaseDC()。
/// 注意 GetDC 返回 nullptr 时要当失败处理（拿不到 DC 就没法往里画）。
class ImageDc {
public:
    explicit ImageDc(CImage& img) : img_(&img), dc_(img.GetDC()) {}
    ~ImageDc() {
        if (dc_ != nullptr) {
            img_->ReleaseDC();
        }
    }
    ImageDc(const ImageDc&)            = delete;
    ImageDc& operator=(const ImageDc&) = delete;

    HDC get() const noexcept { return dc_; }

private:
    CImage* img_;
    HDC     dc_;
};

/// 把系统光标合成进图像 DC。
///
/// 【为什么两个后端都需要】
///   BitBlt 只拷贝"桌面位图"的内容，而光标是系统在显示管线末端单独合成的对象，
///   不属于桌面 DC 的像素。所以不显式绘制，客户端收到的画面里永远没有鼠标指针。
///   DXGI Desktop Duplication 也一样：它给的是 DWM 合成的**桌面图层**，
///   鼠标指针是它之上独立的图层，同样不在里面。
///
/// 【两个容易写错的点】
///   1) GetCursorInfo 必须先填 cbSize，否则调用直接失败（最常见的用法错误）；
///   2) ptScreenPos 是光标的"热点"，而 DrawIconEx 的 x/y 指的是图标左上角，
///      两者相差 (xHotspot, yHotspot)。箭头热点通常是 (0,0)，看不出区别；
///      换成 I 型/十字/缩放光标就会整体偏移 —— 实测撞到过 hotspot=(9,10) 的情况。
///
/// 【关于 alpha：已确认不需要额外补偿，别再"顺手"加】
///   桌面抓屏得到的是 32bpp XRGB，alpha 通道实测恒为 0xFF；
///   而 DrawIconEx 走 source-over 混合，结果 alpha = src_a + dst_a*(1-src_a)，
///   dst_a = 1 时恒等于 1。所以画上去之后 alpha 仍然是 FF，不会出现"光标周围
///   一圈透明方块"那个经典故障。这一点做过 A/B 实验验证，不是推测。
///
/// 【与差异帧的配合（重要）】
///   光标必须在**比对之前**画上去。否则：光标移动时"桌面像素"没变，比对认为无变化，
///   客户端画面上的光标就会卡住不动。先合成再比对，光标的新旧位置会自然表现为
///   两块脏区（并集包围盒把它们一起圈住），客户端一次贴图就完成"擦除旧位置 + 画新位置"。
inline void composite_system_cursor(HDC image_dc) {
    CURSORINFO ci{};
    ci.cbSize = sizeof(ci);
    if (!::GetCursorInfo(&ci)) {
        return; // 拿不到光标信息就当没有光标：抓屏本身不该因此失败
    }
    if ((ci.flags & CURSOR_SHOWING) == 0 || ci.hCursor == nullptr) {
        return; // 光标当前被隐藏（全屏播放、触摸输入等），不该画
    }

    int left = static_cast<int>(ci.ptScreenPos.x);
    int top  = static_cast<int>(ci.ptScreenPos.y);

    // GetIconInfo 会新建两张位图（掩码 + 彩色），必须归还。
    // 抓屏是每帧调用，漏删就是每秒泄漏几十个 GDI 对象。
    ICONINFO ii{};
    if (::GetIconInfo(ci.hCursor, &ii)) {
        win::GdiObjectPtr mask(ii.hbmMask);
        win::GdiObjectPtr color(ii.hbmColor);
        left -= static_cast<int>(ii.xHotspot);
        top -= static_cast<int>(ii.yHotspot);
    }

    // 宽高传 0：按光标自身尺寸绘制，尊重用户在系统设置里调过的大小。
    // 不能传 DI_DEFAULTSIZE —— 那会强制换回系统默认尺寸，用户调大的光标会缩回去。
    // 失败也不影响整帧：光标画不上比整帧抓不到轻得多，所以只记 debug。
    if (!::DrawIconEx(image_dc, left, top, ci.hCursor, 0, 0, 0, nullptr, DI_NORMAL)) {
        RC_LOG_DEBUG("DrawIconEx failed, frame kept without cursor overlay");
    }
}

} // namespace rc::server::detail
