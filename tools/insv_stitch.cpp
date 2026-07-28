// insv_stitch.cpp — Insta360 MediaSDK video stitcher CLI
// Usage: insv_stitch.exe <input.insv> <output.mp4> [options]
//
// Options:
//   --width W           Output width  (default: source size)
//   --height H          Output height (default: source size)
//   --stitch-type TYPE  template|optflow|dynamic|ai  (default: template)
//   --lens-guard TYPE   none|a|s|as|waterproof       (default: none)
//   --flowstate         Enable FlowState gyro leveling (corrects horizon)
//   --cuda              Enable CUDA acceleration
//
// Converts a raw (unstitched) Insta360 .insv video directly into an
// equirectangular .mp4, so downstream frame extraction (ffmpeg) can run
// against already-stitched footage exactly like it does today, unmodified.
//
// Exits 0 on success, 1 on failure. Prints "PROGRESS:<0-100>" lines to
// stdout while stitching so the Python wrapper can report live progress.

#include <iostream>
#include <string>
#include <vector>
#include <thread>
#include <chrono>
#include <atomic>
#include "stitcher/ins_stitcher.h"

int main(int argc, char* argv[]) {
    if (argc < 3) {
        std::cerr << "Usage: insv_stitch <input.insv> <output.mp4> [--width W] [--height H]" << std::endl;
        std::cerr << "       [--stitch-type template|optflow|dynamic|ai]" << std::endl;
        std::cerr << "       [--lens-guard none|a|s|as|waterproof]" << std::endl;
        std::cerr << "       [--flowstate] [--cuda]" << std::endl;
        return 1;
    }

    std::string input_path  = argv[1];
    std::string output_path = argv[2];
    int width  = 0;   // 0 == leave at source size (VideoStitcher default)
    int height = 0;
    ins::STITCH_TYPE stitch_type = ins::STITCH_TYPE::TEMPLATE;
    ins::CameraAccessoryType accessory = ins::CameraAccessoryType::kNormal;
    bool flowstate = false;
    bool cuda      = false;

    for (int i = 3; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "--width" && i + 1 < argc) {
            try { width = std::stoi(argv[++i]); } catch (...) {}
        } else if (arg == "--height" && i + 1 < argc) {
            try { height = std::stoi(argv[++i]); } catch (...) {}
        } else if (arg == "--stitch-type" && i + 1 < argc) {
            std::string t = argv[++i];
            if      (t == "optflow") stitch_type = ins::STITCH_TYPE::OPTFLOW;
            else if (t == "dynamic") stitch_type = ins::STITCH_TYPE::DYNAMICSTITCH;
            else if (t == "ai")      stitch_type = ins::STITCH_TYPE::AIFLOW;
            // else leave as TEMPLATE
        } else if (arg == "--lens-guard" && i + 1 < argc) {
            std::string g = argv[++i];
            if      (g == "a")          accessory = ins::CameraAccessoryType::kLensGuardA;
            else if (g == "s")          accessory = ins::CameraAccessoryType::kLensGuardS;
            else if (g == "as")         accessory = ins::CameraAccessoryType::kLensGuardAS;
            else if (g == "waterproof") accessory = ins::CameraAccessoryType::kInvisibleDiveCaseAir;
        } else if (arg == "--flowstate") {
            flowstate = true;
        } else if (arg == "--cuda") {
            cuda = true;
        }
    }

    const char* stitch_name =
        (stitch_type == ins::STITCH_TYPE::AIFLOW)       ? "ai" :
        (stitch_type == ins::STITCH_TYPE::OPTFLOW)      ? "optflow" :
        (stitch_type == ins::STITCH_TYPE::DYNAMICSTITCH)? "dynamic" : "template";
    std::cout << "[insv_stitch] "
              << (width > 0 ? std::to_string(width) : std::string("source")) << "x"
              << (height > 0 ? std::to_string(height) : std::string("source"))
              << " stitch=" << stitch_name
              << " flowstate=" << (flowstate ? "on" : "off")
              << " cuda=" << (cuda ? "on" : "off")
              << std::endl;

    ins::InitEnv();

    ins::VideoStitcher stitcher;
    stitcher.SetInputPath({input_path});
    stitcher.SetOutputPath(output_path);
    if (width > 0 && height > 0) {
        stitcher.SetOutputSize(width, height);
    }
    stitcher.SetStitchType(stitch_type);
    stitcher.SetCameraAccessoryType(accessory);
    stitcher.EnableFlowState(flowstate);
    stitcher.EnableCuda(cuda);

    std::atomic<bool> done{false};
    std::atomic<bool> failed{false};
    std::atomic<int> error_code{0};

    stitcher.SetStitchStateCallback([&](int error, const char* errinfo) {
        if (error != 0) {
            failed = true;
            error_code = error;
            std::cerr << "ERROR: stitch failed for " << input_path
                      << " (code " << error << "): " << (errinfo ? errinfo : "") << std::endl;
        }
        done = true;
    });

    stitcher.StartStitch();

    // VideoStitcher is asynchronous (unlike ImageStitcher::Stitch(), which
    // blocks) -- poll GetStitchProgress() until the state callback marks us
    // done, reporting progress lines the Python wrapper parses.
    int last_reported = -1;
    while (!done) {
        int progress = stitcher.GetStitchProgress();
        if (progress != last_reported) {
            std::cout << "PROGRESS:" << progress << std::endl;
            last_reported = progress;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(500));
    }

    if (failed) {
        return 1;
    }

    std::cout << "PROGRESS:100" << std::endl;
    std::cout << output_path << std::endl;
    return 0;
}
