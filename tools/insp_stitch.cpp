// insp_stitch.cpp — Insta360 MediaSDK image stitcher CLI
// Usage: insp_stitch.exe <input.insp> <output.jpg> [options]
//
// Options:
//   --width W           Output width  (default: 11968)
//   --height H          Output height (default: 5984)
//   --stitch-type TYPE  template|optflow|dynamic|ai  (default: template)
//   --lens-guard TYPE   none|a|s|as|waterproof       (default: none)
//   --flowstate         Enable FlowState gyro leveling (corrects horizon)
//   --cuda              Enable CUDA acceleration
//
// Exits 0 on success, 1 on failure.

#include <iostream>
#include <string>
#include <vector>
#include "stitcher/ins_stitcher.h"

int main(int argc, char* argv[]) {
    if (argc < 3) {
        std::cerr << "Usage: insp_stitch <input.insp> <output.jpg> [--width W] [--height H]" << std::endl;
        std::cerr << "       [--stitch-type template|optflow|dynamic|ai]" << std::endl;
        std::cerr << "       [--lens-guard none|a|s|as|waterproof]" << std::endl;
        std::cerr << "       [--flowstate] [--cuda]" << std::endl;
        return 1;
    }

    std::string input_path  = argv[1];
    std::string output_path = argv[2];
    int width  = 11968;
    int height = 5984;
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

    // Log config so it appears in the server console and confirms CUDA flag receipt
    const char* stitch_name =
        (stitch_type == ins::STITCH_TYPE::AIFLOW)       ? "ai" :
        (stitch_type == ins::STITCH_TYPE::OPTFLOW)      ? "optflow" :
        (stitch_type == ins::STITCH_TYPE::DYNAMICSTITCH)? "dynamic" : "template";
    std::cout << "[insp_stitch] "
              << width << "x" << height
              << " stitch=" << stitch_name
              << " flowstate=" << (flowstate ? "on" : "off")
              << " cuda=" << (cuda ? "on" : "off")
              << std::endl;

    ins::InitEnv();

    ins::ImageStitcher stitcher;
    stitcher.SetInputPath({input_path});
    stitcher.SetOutputPath(output_path);
    stitcher.SetOutputSize(width, height);
    stitcher.SetStitchType(stitch_type);
    stitcher.SetCameraAccessoryType(accessory);
    stitcher.EnableFlowState(flowstate);
    stitcher.EnableCuda(cuda);

    bool ok = stitcher.Stitch();
    if (!ok) {
        std::cerr << "ERROR: stitch failed for " << input_path << std::endl;
        return 1;
    }

    std::cout << output_path << std::endl;
    return 0;
}
