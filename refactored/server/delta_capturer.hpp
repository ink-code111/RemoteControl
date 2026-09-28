#pragma once
// ============================================================
// 抓屏管线的公共骨架：DeltaCapturerBase
//
// 【为什么要抽这一层】
//   两个后端（GDI BitBlt / DXGI Desktop Duplication）唯一真正的区别，是
//   **那几 MB 像素是怎么到 CPU 内存的**：
//     · GDI ：GetDC(nullptr) + BitBlt —— §6.12 实测 ≈12 ms 固定 + 0.47 GB/s 边际，
//             卡在 GDI 自己的读回路径上，不是内存带宽；
//     · DXGI：AcquireNextFrame + CopyResource + Map —— §6.14 实测 2.0–2.5 ms，
//             等效读回 12.9 GB/s ≈ 普通内存拷贝速度。
//   而它**之后**的一切 —— 相对每个消费者的比对、脏区裁剪、PNG 编码、基准帧更新、
//   每 5 秒的统计汇报 —— 一字不差地相同。
//
//   把这部分复制一份给 DXGI，就是制造两份会各自漂移的实现：改一处忘另一处，
//   而且症状极其隐蔽（两个后端统计口径不同，或某个 bug 只在一个后端上被修掉）。
//   所以这里用"模板方法"把管线钉在基类：子类只实现 grab()（抓像素），其余共享。
//
// 【为什么 grab() 的产物是 CImage，而不是让子类直接给裸缓冲】
//   "DXGI 的 Map 出来就是裸 BGRA，直接在上面比对能省一次 6.5 MB 拷贝" —— 听着诱人，
//   但代价是 diff / crop / encode 全部要改成裸指针版，并自己处理 pitch 与
//   自下而上/自上而下的方向问题（§6.13 记录过 Y 轴搞反的坑：现象是差异矩形上下颠倒，
//   画面上看不出"错"、只觉得偶尔花屏）。
//   而 §6.12/§6.14 的实测说得很清楚：这次拷贝值 0.7–1.1 ms，相对于
//   "13.7 ms → 2.2 ms"是零头。1a 的目标是**先把骨架接通、可回退、可自证**，
//   不是把最后一个毫秒榨干；真要省掉它，应该等 1a 的判据全部立住之后，
//   用数据决定（docs/03 方法论 #1）。
// ============================================================

#include "screen_source.hpp"

#include <chrono>
#include <cstdint>
#include <memory>
#include <vector>

// ATL 的 CImage 定义在 <atlimage.h> 的 **ATL 命名空间**里（全局那个 CImage 只是别名）。
// 前置声明必须落在 ATL 命名空间内 —— 写一个全局的 `class CImage;` 会**凭空造出另一个类**，
// 于是所有同时看得见两者（任何 include 了 atlimage.h 的 .cpp）的地方都会报
// "CImage: 不明确的符号"，并且下游还会连锁出一堆莫名其妙的"非静态成员非法引用"
// （签名解析失败 -> 成员函数定义被当成另一个函数）。编译期撞过，别再改回去。
namespace ATL {
class CImage;
}

namespace rc::server {

/// 一次抓屏的分段计时。由 grab() 填写，基类负责汇总与汇报。
///
/// 字段名沿用既有日志的措辞（"DC / BitBlt / 光标 / 释放"）而不是通用名，
/// 是为了让 **GDI 后端的日志行与改造前逐字可比** —— `[capture-x]` 的格式被
/// `tests/run_frame_rate_probe.py` 按位置解析，且旧版二进制打同样的行，
/// 改了历史数据就再也对不上（项目铁律）。DXGI 的对应关系写在它自己的 grab() 里。
struct GrabTiming {
    double      total_ms   = 0.0; ///< 抓屏总计
    double      prep_ms    = 0.0; ///< 其中：准备（GDI = CImage DC + GetDC；DXGI = 帧资源获取）
    double      move_ms    = 0.0; ///< 其中：像素搬移（GDI = BitBlt；DXGI = CopyResource + Map + memcpy）
    double      cursor_ms  = 0.0; ///< 其中：光标合成（DrawIconEx）
    double      release_ms = 0.0; ///< 其中：释放（GDI = ReleaseDC；DXGI = ReleaseFrame）
};

class DeltaCapturerBase : public IScreenSource {
public:
    DeltaCapturerBase(bool enable_delta, std::uint32_t keyframe_interval);
    ~DeltaCapturerBase() override;

    DeltaCapturerBase(const DeltaCapturerBase&)            = delete;
    DeltaCapturerBase& operator=(const DeltaCapturerBase&) = delete;

    /// 抓一帧。**final** —— 整条管线（抓屏 → 比对 → 裁剪 → 编码 → 基准帧 → 统计）
    /// 只有这一份实现，两个后端共享。子类只覆写 grab()。
    bool capture(CapturedFrame& out, std::uint32_t consumer_id) final;

    void release_consumer(std::uint32_t consumer_id) noexcept final;

    /// 【诊断，见 config.hpp 的 `debug_leak_gdi_per_frame`】
    /// 每个抓屏帧故意泄漏这么多 GDI 对象。默认 0 = 关闭（与本字段存在之前**逐字等价**）。
    /// 只在 `run_soak_check.py --reverse-control` 里非 0 —— 用来证明"资源不泄漏"的判据
    /// 真的看得见泄漏，而不是永远报绿。
    /// 必须在 start_capture/首个请求之前调用（与其它配置注入点同一条约定）。
    void set_debug_leak_gdi_per_frame(std::uint32_t n) noexcept {
        debug_leak_gdi_per_frame_ = n;
    }

protected:
    /// 把当前屏幕的像素抓进 @p cur。
    ///
    /// **子类负责 cur.Create(w, h, 32, 0) 并填充像素** —— 因为"抓屏尺寸"本身就是
    /// 后端相关的：GDI 在 DPI-unaware 下只能拿到 1707×960，而 DXGI 拿的是物理
    /// 2560×1440（docs §6.13 / §6.14 实测）。基类不预设尺寸，只在之后从 cur 读
    /// 实际值，并据此做基准帧的尺寸校验（尺寸一变就强制发整帧）。
    ///
    /// @return false 表示这一帧抓不到（安全桌面、会话锁定、显示器拓扑变化、设备丢失）。
    ///         注意：**"没有变化"不是失败** —— DXGI 的事件驱动超时要在这里被翻译成
    ///         "填上一次的像素"并返回 true，而不是 return false（见 dxgi_capturer.cpp）。
    virtual bool grab(ATL::CImage& cur, GrabTiming& t) = 0;

    /// 后端名字（日志与配置校验用），如 "gdi" / "dxgi"
    virtual const char* backend() const noexcept = 0;

    /// 把系统光标合成进图像。**两个后端都需要**：
    ///   BitBlt 拿不到光标（光标不属于桌面 DC 的像素）；
    ///   DXGI Desktop Duplication 拿到的也是 DWM 合成的**桌面图层**，
    ///   鼠标指针是它之上独立的图层，同样不在里面。
    /// 调用时机必须在自己 grab() 的**比对之前**（基类在 grab() 之后才比对），
    /// 否则光标移动不会被判成脏区，客户端画面上的指针会卡住不动。
    void composite_cursor(ATL::CImage& image) const;

    bool capture_cursor_ = true;

    /// 差异帧是否启用。子类拼 name() 时要用（"gdi+png" vs "gdi+png+delta"）。
    bool delta_enabled() const noexcept { return enable_delta_; }

private:
    /// 用已填好像素的 cur 走完剩余管线（比对 → 裁剪 → 编码 → 基准帧 → 统计）。
    /// @param wait_ms 「请求到达 → 抓屏开始」的空档，由 capture() 在调用 grab() 前算好。
    ///        它不属于抓屏成本，但必须与抓屏、比对、编码一起进同一批统计，
    ///        否则"分段之和远小于真实帧周期"这个现象就没有了归因的入口。
    bool finish(ATL::CImage& cur, const GrabTiming& t, double wait_ms, CapturedFrame& out,
                std::uint32_t consumer_id);

    /// 每 5 秒打一次统计行（格式与改造前完全一致）
    void maybe_report(int width, int height);

    struct BaseStore;

    bool          enable_delta_      = true;
    std::uint32_t keyframe_interval_ = 60;

    /// 反向对照用的故意泄漏量（0 = 关）。见 set_debug_leak_gdi_per_frame。
    /// 只被 finish() 读，UI/抓屏线程单线程访问，不需要原子。
    std::uint32_t debug_leak_gdi_per_frame_ = 0;

    /// 基准帧表（按消费者）。unique_ptr + 前置声明：本头文件不必见到它的定义。
    std::unique_ptr<BaseStore> bases_;

    /// GDI+ 生命周期。**放在基类**而不是 GDI 后端里 ——
    /// PNG 编码发生在基类的 finish()，两个后端都要用它。
    std::uintptr_t gdiplus_token_ = 0;

    // ---- 分段耗时观测（每 5 秒向日志汇总一条）----
    // 计数只在抓屏线程里动，无需加锁。
    std::uint64_t capture_count_  = 0;
    std::uint64_t keyframe_count_ = 0; ///< 其中整帧（含周期性关键帧）
    std::uint64_t delta_count_    = 0; ///< 其中增量帧
    std::uint64_t idle_count_     = 0; ///< 其中"无变化"的空增量帧
    double        grab_ms_sum_    = 0.0; ///< 抓屏总计：准备 + 搬移 + 光标 + 释放
    double        grab_dc_ms_sum_ = 0.0; ///< 其中：准备
    double        grab_blit_ms_sum_ = 0.0; ///< 其中：像素搬移（DXGI 要替换的就是它）
    double        grab_cursor_ms_sum_ = 0.0; ///< 其中：光标合成（DrawIconEx）
    double        grab_rel_ms_sum_  = 0.0; ///< 其中：释放
    double        diff_ms_sum_    = 0.0; ///< 比对 + 裁剪累计
    double        diff_scan_ms_sum_ = 0.0; ///< 其中：逐行扫描求脏区包围盒
    double        diff_crop_ms_sum_ = 0.0; ///< 其中：从整屏裁出脏区（DXGI 原生给脏矩形，这段也能省）
    double        wait_ms_sum_    = 0.0; ///< 请求到达 → 抓屏开始的空档（限流等待 + 定时器调度延迟）
    /// 空档超过 10 ms 的帧数。只看均值分不清"平均等了 5 ms"到底是
    /// "每次等 5 ms"还是"一半不等、一半等 10 ms"——后者才是定时器粒度问题。
    std::uint64_t wait_gt10_count_ = 0;
    double        encode_ms_sum_  = 0.0; ///< PNG 编码累计

    // ---- 只统计"有变化"帧的同一批分段耗时 ----
    // 为什么必须再存一套：客户端那边的"往返/解码/贴图"只能在**变化帧**上取样
    // （"无变化"的空增量帧不进它的解码队列），而上面那些和是**全部帧**的平均。
    // 拿后者的"服务端净工作"去减前者的"往返"，差出来的根本不是"未观测的时间"，
    // 只是两个不同的样本集合 —— 实测踩过：会凭空多出 20 ms，看起来像丢了数据。
    std::uint64_t chg_count_         = 0; ///< 其中"有变化"的帧数
    double        chg_grab_ms_sum_   = 0.0;
    double        chg_diff_ms_sum_   = 0.0;
    double        chg_encode_ms_sum_ = 0.0;
    std::uint64_t bytes_sum_         = 0; ///< 编码后字节数累计
    std::uint64_t dirty_px_sum_      = 0; ///< 编码的实际像素数累计（整帧时等于整屏）

    // 【增量帧专用】—— "脏区占比"与"归一化编码"都必须在**这一套**上算。
    // 整帧样本的脏区恒等于整屏（占比 100%），混进来会把两个量一起带偏：
    //   · 脏区占比会被整帧抬到 90%+，与"客户端按脏区重绘能省很多"自相矛盾；
    //   · ms/Mpx 会把"整屏编码"和"crop 编码"两种不同开销混成一个数。
    // 归一化是 I6 归因的前提：原始编码耗时随脏区大小变（实测两轮同配置差 3.5 倍），
    // 不除以像素数就没法比（§6.19(13)）。
    std::uint64_t delta_px_sum_        = 0; ///< 增量帧编码的像素数累计
    double        delta_encode_ms_sum_ = 0.0; ///< 增量帧的编码耗时累计

    // 【多块脏矩形上限】—— §"客户端按脏区重绘" 之后的下一个候选方向。
    // 现在整条链只传 1 个**并集包围盒**；要问"改成多块能省多少"，必须先量这个上限。
    // 量法：diff_bbox 本来就在做 64px 瓦片级 memcmp，顺手把"命中瓦片"记下来：
    //   · tile_hit_row_px_sum_ / (tile_bbox_px_sum_) = 主指标 —— 与包围盒**同一口径**
    //     （纵向精确到行、横向 64px 瓦片），所以可以跨格读取。
    //   · tile_hit_tile_px_sum_/blocks_max 只用于"换更粗粒度会怎样"的对照，不进主指标。
    // 只在**增量帧**上累加（与 dirty_px_sum_ 同一套样本口径）。
    std::uint64_t tile_hit_row_px_sum_  = 0; ///< 命中瓦片面积累计（行精确 × 横向瓦片）
    std::uint64_t tile_bbox_px_sum_     = 0; ///< 包围盒面积累计（同一口径下的分母）
    std::uint64_t tile_hit_tile_px_sum_ = 0; ///< 2D 瓦片粒度下的命中面积（对照用）
    std::uint64_t tile_blocks_sum_      = 0; ///< 2D 瓦片图的 4-连通块数累计
    std::uint64_t tile_blocks_max_      = 0; ///< 单帧块数最大值（决定协议要预留几块）
    std::uint64_t tile_delta_count_     = 0; ///< 进入这块统计的增量帧数
    /// 每帧「命中面积 / 包围盒面积」（主指标的分位用）。5s 一批，至多几百条。
    std::vector<double> tile_ratio_;
    /// 每帧的 4-连通块数（分位用）。
    std::vector<int>    tile_blocks_;
    double              diff_tile_ms_sum_ = 0.0; ///< 瓦片统计本身的耗时（自证观测代价）

    std::chrono::steady_clock::time_point last_report_{};
};

} // namespace rc::server
