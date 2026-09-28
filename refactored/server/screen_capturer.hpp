#pragma once
// ============================================================
// GDI BitBlt 抓屏后端
//
// 它只负责一件事：把桌面像素搬进一张 CImage（grab()）。
// 之后的一切 —— 相对每个消费者的比对、脏区裁剪、PNG 编码、基准帧更新、
// 每 5 秒的统计 —— 都在 DeltaCapturerBase 里，与 DXGI 后端共用同一份实现。
// 为什么这样切分，见 delta_capturer.hpp 的文件头注释。
//
// 【它的代价特征（docs §6.12 实测，本机 150% 缩放、DPI-unaware 上下文）】
//   BitBlt ≈ **12 ms 固定 + 0.47 GB/s 边际** —— 卡在 GDI 自己的读回路径上，
//   远低于内存带宽（"拷 6.5 MB 本该只要 0.5 ms"）。
//   而且抓到的 1707×960 是物理画面**左上角 1:1 的裁剪**（§6.13），不是"桌面缩小版"，
//   所以它同时是"最慢"和"看不全"的那个后端。
//   DXGI 存在的全部意义就是这两条（§6.14：读回 2.0–2.5 ms，且给物理 2560×1440）。
// ============================================================

#include "delta_capturer.hpp"

namespace rc::server {

class GdiScreenCapturer final : public DeltaCapturerBase {
public:
    /// @param capture_cursor 是否把鼠标光标合成进画面。
    ///        BitBlt 拿不到光标（光标不属于桌面 DC 的像素），必须显式绘制，
    ///        所以默认开启；关掉只用于对照测试或需要纯净画面的场合。
    /// @param enable_delta 是否启用差异帧。关掉就退化成"每帧整屏"的第二阶段行为，
    ///        留着这个开关是为了能做 A/B 实测（否则帧率提升无法归因）。
    /// @param keyframe_interval 每隔多少帧强制发一次整帧。
    ///        差异帧是**有状态**的：客户端一旦丢帧，链就断了。周期性整帧是
    ///        不需要额外协议就能做到的最简恢复手段（也顺带覆盖新客户端接入）。
    explicit GdiScreenCapturer(bool          capture_cursor     = true,
                               bool          enable_delta       = true,
                               std::uint32_t keyframe_interval  = 60);

    GdiScreenCapturer(const GdiScreenCapturer&)            = delete;
    GdiScreenCapturer& operator=(const GdiScreenCapturer&) = delete;

    const char* name() const noexcept override {
        return delta_enabled() ? "gdi+png+delta" : "gdi+png";
    }

protected:
    /// BitBlt + 光标合成。
    ///
    /// 【计时口径：这一段的四拆必须保留】
    ///   差异帧之后端到端帧周期（47.2 ms）远大于分段之和（22.4 ms），要回答
    ///   "那 20.1 ms 里有多少是 BitBlt 那次整屏拷贝"，就必须把抓屏再拆成
    ///   DC / BitBlt / 光标 / 释放 四段 —— **只有 BitBlt 是 DXGI 能替代的**，
    ///   DC 取放与光标合成它一个都省不掉。拆不开，就没有"DXGI 值不值得做"的依据。
    bool grab(ATL::CImage& cur, GrabTiming& t) override;

    const char* backend() const noexcept override { return "gdi"; }
};

} // namespace rc::server
