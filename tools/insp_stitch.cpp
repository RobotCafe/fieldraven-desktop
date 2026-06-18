// insp_stitch.cpp
// CLI wrapper around ins::ImageStitcher from the Insta360 Media SDK.
// Converts a single .insp file to a full-resolution equirectangular JPEG.
//
// Usage:
//   insp_stitch.exe <input.insp> <output.jpg> [width] [height]
//
// Defaults to 11968x5984 (X4 full resolution).
// Exits 0 on success, 1 on failure.
// On success, prints the output path to stdout.

#include <iostream>
#include <string>
#include <vector>
#include "stitcher/ins_stitcher.h"

int main(int argc, char* argv[]) {
    if (argc < 3) {
        std::cerr << "Usage: insp_stitch <input.insp> <output.jpg> [width] [height]" << std::endl;
        return 1;
    }

    std::string input_path  = argv[1];
    std::string output_path = argv[2];
    int width  = 11968;
    int height = 5984;

    if (argc >= 5) {
        try {
            width  = std::stoi(argv[3]);
            height = std::stoi(argv[4]);
        } catch (...) {
            std::cerr << "Invalid width/height" << std::endl;
            return 1;
        }
    }

    ins::InitEnv();

    ins::ImageStitcher stitcher;
    stitcher.SetInputPath({input_path});
    stitcher.SetOutputPath(output_path);
    stitcher.SetOutputSize(width, height);
    stitcher.SetStitchType(ins::STITCH_TYPE::TEMPLATE);
    stitcher.EnableCuda(false);

    bool ok = stitcher.Stitch();
    if (!ok) {
        std::cerr << "ERROR: stitch failed for " << input_path << std::endl;
        return 1;
    }

    std::cout << output_path << std::endl;
    return 0;
}
