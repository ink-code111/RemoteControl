#include "dxgi_capturer.hpp"

#include "capture_internal.hpp"
#include "logger.hpp"

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <Windows.h>
#include <atlimage.h>
#include <d3d11.h>
#include <dxgi1_2.h>
#include <wrl/client.h>

#include <chrono>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

namespace rc::server {

namespace {

using Microsoft::WRL::ComPtr;

std::string hr_str(HRESULT hr) {
    char buf[32];
    std::snprintf(buf, sizeof(buf), "0x%08lX", static_cast<unsigned long>(hr));
    return std::string(buf);
}

double ms_between(const std::chrono::steady_clock::time_point& a,
                  const std::chrono::steady_clock::time_point& b) {
    return std::chrono::duration<double, std::milli>(b - a).count();
}

const char* fmt_name(DXGI_FORMAT f) {
    switch (f) {
    case DXGI_FORMAT_B8G8R8A8_UNORM:      return "B8G8R8A8_UNORM";
    case DXGI_FORMAT_B8G8R8A8_UNORM_SRGB: return "B8G8R8A8_UNORM_SRGB";
    case DXGI_FORMAT_R8G8B8A8_UNORM:      return "R8G8B8A8_UNORM";
    case DXGI_FORMAT_R10G10B10A2_UNORM:   return "R10G10B10A2_UNORM";
    case DXGI_FORMAT_R16G16B16A16_FLOAT:  return "R16G16B16A16_FLOAT";
    default:                              return "其它";
    }
}

/// CImage 的实际行 pitch（字节）。
/// 用相邻两行的地址差，而不是直接拿 width*4 —— CImage 的 DIB 可能是自下而上
/// （pitch 为负），手算符号极易把 Y 轴搞反（§6.13 记录过这个坑：现象是差异矩形
/// 上下颠倒，而画面上"看不出错"，只觉得偶尔花屏）。
std::ptrdiff_t image_pitch(ATL::CImage& img, int h, std::size_t fallback) {
    auto* p0 = static_cast<std::uint8_t*>(img.GetPixelAddress(0, 0));
    if (p0 == nullptr) {
        return static_cast<std::ptrdiff_t>(fallback);
    }
    if (h >= 2) {
        auto* p1 = static_cast<std::uint8_t*>(img.GetPixelAddress(0, 1));
        if (p1 != nullptr) {
            return p1 - p0;
        }
    }
    return static_cast<std::ptrdiff_t>(fallback);
}

} // namespace

struct DxgiScreenCapturer::Impl {
    ComPtr<ID3D11Device>           device;
    ComPtr<ID3D11DeviceContext>    ctx;
    ComPtr<IDXGIOutputDuplication> dup;
    ComPtr<ID3D11Texture2D>        staging;

    int         width       = 0;
    int         height      = 0;
    DXGI_FORMAT format      = DXGI_FORMAT_UNKNOWN;

    /// staging 里是否已经有内容。它同时充当"上一帧"的持久副本：
    /// D3D11_MAP_READ 是只读映射，Map/Unmap 不会改动纹理内容，
    /// 所以 AcquireNextFrame 超时时直接再 Map 一次就能拿到同样的像素。
    bool have_pixels = false;

    // 事件驱动语义的观测（每 5 秒一条，另起一行，不动既有日志格式）
    std::uint64_t                         n_new = 0, n_timeout = 0, n_error = 0, n_accum_gt1 = 0;
    std::chrono::steady_clock::time_point last_stats{};

    bool init(std::string& err);
    bool capture_into(ATL::CImage& cur, GrabTiming& t);

private:
    void report_stats();
};

bool DxgiScreenCapturer::Impl::init(std::string& err) {
    // ---- 1) D3D11 设备 ----
    // 只用 HARDWARE：Desktop Duplication 不支持 WARP（软件光栅器）。
    // 显式指定 feature level 列表而不是传 nullptr，避免驱动给一个过低的等级。
    const D3D_FEATURE_LEVEL want[] = {D3D_FEATURE_LEVEL_11_1, D3D_FEATURE_LEVEL_11_0};
    D3D_FEATURE_LEVEL       got{};
    HRESULT hr = ::D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, 0, want,
                                     ARRAYSIZE(want), D3D11_SDK_VERSION, &device, &got, &ctx);
    if (FAILED(hr)) {
        err = "D3D11CreateDevice(HARDWARE) 失败 " + hr_str(hr) +
              "（无可用硬件设备；Desktop Duplication 不支持 WARP）";
        return false;
    }

    // ---- 2) adapter → output → duplication ----
    // 从**设备的 adapter** 往下找，而不是 CreateDXGIFactory 后枚举第一个适配器：
    // 后者在多显卡机器上可能选中与 device 不同的适配器，DuplicateOutput 会失败。
    ComPtr<IDXGIDevice> dxgi_dev;
    hr = device.As(&dxgi_dev);
    if (FAILED(hr)) {
        err = "ID3D11Device::As(IDXGIDevice) 失败 " + hr_str(hr);
        return false;
    }
    ComPtr<IDXGIAdapter> adapter;
    hr = dxgi_dev->GetAdapter(&adapter);
    if (FAILED(hr)) {
        err = "IDXGIDevice::GetAdapter 失败 " + hr_str(hr);
        return false;
    }
    ComPtr<IDXGIOutput> output;
    hr = adapter->EnumOutputs(0, &output); // 目前只抓第 0 个输出（主屏）；多屏留待后续
    if (FAILED(hr)) {
        err = "IDXGIAdapter::EnumOutputs(0) 失败 " + hr_str(hr);
        return false;
    }
    ComPtr<IDXGIOutput1> output1;
    hr = output.As(&output1);
    if (FAILED(hr)) {
        err = "IDXGIOutput::As(IDXGIOutput1) 失败 " + hr_str(hr);
        return false;
    }

    hr = output1->DuplicateOutput(device.Get(), &dup);
    if (FAILED(hr)) {
        // 常见原因：已有别的进程占着这个输出的 duplication；或当前会话没有物理输出
        //（RDP / 无显示器的机器）。都归为"这个环境用不了 DXGI"，交给装配层回退。
        err = "IDXGIOutput1::DuplicateOutput 失败 " + hr_str(hr);
        return false;
    }

    DXGI_OUTDUPL_DESC dd{};
    dup->GetDesc(&dd);
    width  = static_cast<int>(dd.ModeDesc.Width);
    height = static_cast<int>(dd.ModeDesc.Height);
    format = dd.ModeDesc.Format;

    // ---- 3) 格式检查 ----
    // CImage 的 32bpp DIB 内存布局就是 BGRA，正好对应 B8G8R8A8_UNORM，这条路径
    // 不需要任何转换。其它格式必须转换才能用 —— 1a 明确拒绝，而不是悄悄给出一张
    // **颜色错乱但"看起来有图像"** 的画面：那是最难被发现的一类错。
    if (format != DXGI_FORMAT_B8G8R8A8_UNORM && format != DXGI_FORMAT_B8G8R8A8_UNORM_SRGB) {
        err = std::string("桌面纹理格式 ") + fmt_name(format) +
              " 暂不支持（只支持 B8G8R8A8_UNORM：CImage 的 32bpp 布局就是 BGRA）";
        return false;
    }

    // ---- 4) staging 纹理（CPU 可读）----
    // 用 STAGING 而不是自己建 USAGE_DEFAULT + CPU_ACCESS_READ（后者不允许）。
    D3D11_TEXTURE2D_DESC sd{};
    sd.Width            = static_cast<UINT>(width);
    sd.Height           = static_cast<UINT>(height);
    sd.MipLevels        = 1;
    sd.ArraySize        = 1;
    sd.Format           = format;
    sd.SampleDesc.Count = 1;
    sd.Usage            = D3D11_USAGE_STAGING;
    sd.CPUAccessFlags   = D3D11_CPU_ACCESS_READ;
    hr = device->CreateTexture2D(&sd, nullptr, &staging);
    if (FAILED(hr)) {
        err = "CreateTexture2D(staging) 失败 " + hr_str(hr);
        return false;
    }

    RC_LOG_INFO("dxgi: DuplicateOutput 成功 —— 帧 {}x{} 格式 {}（物理像素，"
                "与调用方 DPI 上下文无关：这就是「看得全」的来源）",
                width, height, fmt_name(format));

    // ---- 5) 预热：立刻抓满第一帧 ----
    // 不做这一步的话，如果服务端起来时桌面是静止的，第一次 grab() 会立刻超时，
    // 而那时 staging 还是空的 —— 于是"抓不到帧"。其实此时完全可以把当前桌面给他。
    // 所以这里循环等最多 1 秒，务必让 staging 有内容。
    {
        ATL::CImage warm;
        GrabTiming tw{};
        for (int attempt = 0; attempt < 20 && !have_pixels; ++attempt) {
            capture_into(warm, tw);
            if (!have_pixels) {
                ::Sleep(50);
            }
        }
        if (!have_pixels) {
            err = "初始化后 1 秒内拿不到任何一帧（Desktop Duplication 无内容）";
            return false;
        }
    }
    RC_LOG_INFO("dxgi: 预热完成，第一帧已就绪");
    return true;
}

void DxgiScreenCapturer::Impl::report_stats() {
    const auto now = std::chrono::steady_clock::now();
    if (last_stats.time_since_epoch().count() == 0) {
        last_stats = now; // 首次只起表，不产出一行"半截"统计
        return;
    }
    if (std::chrono::duration<double>(now - last_stats).count() < 5.0) {
        return;
    }
    RC_LOG_INFO("[capture-dxgi] 帧来源 | 新帧 {} 超时复用 {} 错误 {} | "
                "AcquireNextFrame 报告 AccumulatedFrames>1 共 {} 次",
                n_new, n_timeout, n_error, n_accum_gt1);
    n_new = n_timeout = n_error = n_accum_gt1 = 0;
    last_stats = now;
}

bool DxgiScreenCapturer::Impl::capture_into(ATL::CImage& cur, GrabTiming& t) {
    const auto t0 = std::chrono::steady_clock::now();

    ComPtr<IDXGIResource>   resource;
    DXGI_OUTDUPL_FRAME_INFO info{};
    // timeout=0：不等待。拉屏模型下每帧都会调用它，等待只会白占抓屏线程；
    // 屏幕没变化时立刻返回 WAIT_TIMEOUT，那时复用上一份像素即可。
    const HRESULT hr = dup->AcquireNextFrame(0, &info, &resource);
    const auto    t1 = std::chrono::steady_clock::now();

    bool fresh = false;
    if (hr == DXGI_ERROR_WAIT_TIMEOUT) {
        ++n_timeout;
        if (!have_pixels) {
            RC_LOG_WARN("dxgi: AcquireNextFrame 超时，且 staging 里还没有任何一帧可复用");
            return false;
        }
        // 复用 staging 里的上一份内容 —— 注意这**不是失败**，语义见头文件注释。
        //
        // 【这条分支的代价有多大：不做反向对照是看不出来的】
        //   2026-09-23 反向对照实测：把这里改成 `return false` 之后，
        //     · 服务端日志出现一次 `session 2 capture failed (count=1)`，**就一次**；
        //     · 此后链路再无任何帧 —— 客户端画面**永久冻结**，直到重连。
        //   原因是 session.cpp:297 收到 ok=false 后**什么都不发**，而客户端
        //   "收不到回应就不会请求下一帧"（同文件 296 行注释已经写明）。
        //   也就是说 WAIT_TIMEOUT 处理错不是"偶尔少一帧"，而是"桌面静止一下就把
        //   整条链路打死"。判据 tests/run_dxgi_timeout_check.py 就是钉这件事的，
        //   它认 `capture failed` 这一行为 FAIL（退出码 1）。
    } else if (FAILED(hr)) {
        ++n_error;
        // DXGI_ERROR_ACCESS_LOST：分辨率变化 / 切换用户桌面 / UAC 安全桌面。
        // 正确的长期处理是重建 duplication；1a 先如实报出来（重建留给后续版本，
        // 而"如实报出来"这一步不能省 —— 静默失败正是本项目反复栽的坑）。
        RC_LOG_WARN("dxgi: AcquireNextFrame 失败 {}（可能是分辨率变化或安全桌面）", hr_str(hr));
        return false;
    } else {
        fresh = true;
        ++n_new;
        if (info.AccumulatedFrames > 1) {
            ++n_accum_gt1;
        }
        ComPtr<ID3D11Texture2D> tex;
        const HRESULT           hr2 = resource.As(&tex);
        if (FAILED(hr2)) {
            dup->ReleaseFrame();
            RC_LOG_WARN("dxgi: 帧资源不是 ID3D11Texture2D（{}）", hr_str(hr2));
            return false;
        }
        // CopyResource 只是**提交**一条 GPU 侧拷贝命令，本身几乎不花时间
        //（§6.14 实测 0.003 ms）；真正的代价在后面的 Map —— 它会等 GPU 完成，
        // 也就是等 DMA 把像素送回系统内存（那才是 2 ms 那一段）。
        ctx->CopyResource(staging.Get(), tex.Get());
        have_pixels = true;
    }
    const auto t2 = std::chrono::steady_clock::now();

    // ---- 像素进城：staging（新拷入或上次留下的） → CImage ----
    D3D11_MAPPED_SUBRESOURCE mapped{};
    const HRESULT            hrm = ctx->Map(staging.Get(), 0, D3D11_MAP_READ, 0, &mapped);
    if (FAILED(hrm)) {
        if (fresh) {
            dup->ReleaseFrame();
        }
        RC_LOG_WARN("dxgi: Map(staging) 失败 {}", hr_str(hrm));
        return false;
    }

    if (cur.Create(width, height, 32, 0) != 1) {
        ctx->Unmap(staging.Get(), 0);
        if (fresh) {
            dup->ReleaseFrame();
        }
        RC_LOG_ERROR("dxgi: CImage::Create({}x{}) 失败", width, height);
        return false;
    }

    const std::size_t row  = static_cast<std::size_t>(width) * 4;
    auto*             dst0 = static_cast<std::uint8_t*>(cur.GetPixelAddress(0, 0));
    if (dst0 == nullptr) {
        ctx->Unmap(staging.Get(), 0);
        if (fresh) {
            dup->ReleaseFrame();
        }
        RC_LOG_ERROR("dxgi: CImage::GetPixelAddress 失败");
        return false;
    }
    const std::ptrdiff_t dst_pitch = image_pitch(cur, height, row);
    const auto* src = static_cast<const std::uint8_t*>(mapped.pData);
    // 逐行拷：staging 的 RowPitch 可能大于 width*4（驱动会做对齐），
    // 一次性 memcpy 整块会把行尾的对齐填充当成像素，画面会斜。
    for (int y = 0; y < height; ++y) {
        std::memcpy(dst0 + static_cast<std::ptrdiff_t>(y) * dst_pitch,
                    src + static_cast<std::size_t>(y) * mapped.RowPitch, row);
    }
    ctx->Unmap(staging.Get(), 0);
    const auto t3 = std::chrono::steady_clock::now();

    if (fresh) {
        // ReleaseFrame 必须在下一次 AcquireNextFrame **之前**调用，
        // 否则后续 Acquire 一直失败（DXGI_ERROR_INVALID_CALL）。
        dup->ReleaseFrame();
    }
    const auto t4 = std::chrono::steady_clock::now();

    // 字段名沿用既有日志口径（"DC / BitBlt / 释放"）以便两代后端的数字能并排读：
    //   prep    ↔ GDI 的 DC
    //   move    ↔ GDI 的 BitBlt
    //   release ↔ GDI 的释放
    // cursor 由外层补（光标画在 CImage 上，不属于这个 Impl）
    t.prep_ms    = ms_between(t0, t1);
    t.move_ms    = ms_between(t1, t3);
    t.release_ms = ms_between(t3, t4);

    report_stats();
    return true;
}

DxgiScreenCapturer::DxgiScreenCapturer(bool capture_cursor, bool enable_delta,
                                       std::uint32_t keyframe_interval)
    : DeltaCapturerBase(enable_delta, keyframe_interval), impl_(std::make_unique<Impl>()) {
    capture_cursor_ = capture_cursor;

    std::string err;
    ok_ = impl_->init(err);
    if (!ok_) {
        init_error_ = err;
        impl_.reset(); // 失败后不持有任何 D3D 资源
    }
}

DxgiScreenCapturer::~DxgiScreenCapturer() = default;

bool DxgiScreenCapturer::grab(ATL::CImage& cur, GrabTiming& t) {
    if (!ok_ || impl_ == nullptr) {
        // 装配层不该走到这里（ok()==false 时就不该用它）。报出来而不是静默返回 false ——
        // 静默的"抓不到帧"会让排查方向完全跑偏。
        RC_LOG_ERROR("dxgi: grab() 被调用，但该后端初始化失败（{}）", init_error_);
        return false;
    }

    const auto t_begin = std::chrono::steady_clock::now();
    if (!impl_->capture_into(cur, t)) {
        return false;
    }

    // 光标：Desktop Duplication 给的是 DWM 合成的**桌面图层**，鼠标指针在它之上
    // 是独立图层，同样不在里面 —— 所以两个后端都要显式合成。
    // 注意**每次都要画**（包括复用旧像素的那些帧）：复用的只是桌面像素，
    // 而光标可能刚动过。漏掉这一步的症状是"远程画面里指针卡住不动"。
    const auto t_cursor0 = std::chrono::steady_clock::now();
    composite_cursor(cur);
    const auto t_cursor1 = std::chrono::steady_clock::now();

    t.cursor_ms = ms_between(t_cursor0, t_cursor1);
    t.total_ms  = ms_between(t_begin, t_cursor1);
    return true;
}

} // namespace rc::server
