#pragma once
// ============================================================
// DXGI Desktop Duplication 抓屏后端
//
// 它和 GDI 后端的差别只有一处：像素怎么到 CPU 内存。
//   GDI  ：GetDC(nullptr) + BitBlt          —— §6.12 实测 ≈12 ms 固定 + 0.47 GB/s 边际
//   DXGI ：AcquireNextFrame + CopyResource + Map —— §6.14 实测 2.0–2.5 ms
// 其余（比对 / 裁剪 / PNG 编码 / 基准帧 / 统计）全在 DeltaCapturerBase 里共用。
//
// 【它顺带修掉的两条已确认缺陷（§6.13、§6.14 实测）】
//   ① 画面覆盖：DXGI 给**物理** 2560×1440，而 GDI 在 DPI-unaware 下只给
//      物理画面左上角 1:1 的 1707×960（远端只看得到 44%）；
//   ② 坐标一致：DXGI 的输出尺寸**不随调用方的 DPI 上下文变化**（12/12 次一致），
//      于是"抓屏 1:1 而输入被虚拟化 ×1.5"这个不一致也就没有了。
//   两条一起消失，而且更快 —— 这是第 1 步的全部价值。
//
// 【为什么用 Pimpl】
//   d3d11.h / dxgi1_2.h 会把一大堆 COM 类型带进任何 include 本头文件的地方。
//   本头文件被 asio_server.cpp 包含（那里只该关心"选哪个后端"），
//   没必要为了一个成员指针把整个 D3D 头链拖进去。
// ============================================================

#include "delta_capturer.hpp"

#include <memory>
#include <string>

namespace rc::server {

class DxgiScreenCapturer final : public DeltaCapturerBase {
public:
    /// 构造**不抛异常**：初始化失败时 ok() 为 false，并把原因放进 init_error()。
    /// 由装配层决定这是"该回退 GDI"（capture_backend=auto）还是"该报错退出"（=dxgi）。
    ///
    /// 为什么不做成构造失败就抛：那会把"某个环境里 DXGI 用不了"变成服务端起不来 ——
    /// 而 GDI 后端在那种环境下完全可用。可用性降级应该由策略决定，不该由构造函数决定。
    explicit DxgiScreenCapturer(bool          capture_cursor     = true,
                                bool          enable_delta       = true,
                                std::uint32_t keyframe_interval  = 60);
    ~DxgiScreenCapturer() override;

    DxgiScreenCapturer(const DxgiScreenCapturer&)            = delete;
    DxgiScreenCapturer& operator=(const DxgiScreenCapturer&) = delete;

    const char* name() const noexcept override {
        return delta_enabled() ? "dxgi+png+delta" : "dxgi+png";
    }

    /// 初始化是否成功。false 时**不要**调用 capture()。
    bool ok() const noexcept { return ok_; }

    /// 初始化失败原因（一行，含 HRESULT），供日志与回退提示使用
    const std::string& init_error() const noexcept { return init_error_; }

protected:
    /// AcquireNextFrame + CopyResource + Map + memcpy 到 CImage，然后合成光标。
    ///
    /// 【事件驱动语义在这里被翻译】
    ///   AcquireNextFrame 只在屏幕**真的变化**时才给新帧，静止时返回
    ///   DXGI_ERROR_WAIT_TIMEOUT。但我们的协议是"客户端请求 → 服务端抓一帧"，
    ///   上层要的是"一张当前桌面的图"，不是"有没有变化"。
    ///   所以超时**不是失败**：复用 staging 纹理里的上一份内容（Map 是只读的，
    ///   内容不会被 Map/Unmap 动过），照常返回 true。
    ///   接下来基类的比对自然会判定"无变化"，客户端收到一个空增量帧 ——
    ///   这与差异帧阶段已经建立的"无变化帧"语义正好对上，不需要新增协议概念。
    bool grab(ATL::CImage& cur, GrabTiming& t) override;

    const char* backend() const noexcept override { return "dxgi"; }

private:
    struct Impl; ///< D3D11/DXGI 类型都藏在 .cpp 里
    std::unique_ptr<Impl> impl_;
    std::string           init_error_;
    bool                  ok_ = false;
};

} // namespace rc::server
