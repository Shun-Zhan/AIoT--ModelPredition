// Host-side golden runner: executes the exact ESP32 edge_model.cpp against
// deterministically generated inputs and prints outputs. The Python test
// wrapper compiles this file, feeds inputs, and compares against PyTorch.
#include "edge_model.h"

#include <cstdio>
#include <cstring>

int main(int argc, char **argv) {
  if (argc != 2) {
    std::fprintf(stderr, "usage: run_host_model <input.bin>\n");
    return 2;
  }
  FILE *f = std::fopen(argv[1], "rb");
  if (!f) {
    std::fprintf(stderr, "cannot open input\n");
    return 3;
  }
  // Input layout: 24 floats (ET0), then 288*9 floats (soil), all float32.
  float et0[edge_model::kEt0InputSize];
  float soil[edge_model::kSoilInputSteps][edge_model::kSoilFeatureCount];
  if (std::fread(et0, sizeof(float), edge_model::kEt0InputSize, f) != edge_model::kEt0InputSize) {
    std::fclose(f);
    return 4;
  }
  if (std::fread(soil, sizeof(float),
                 edge_model::kSoilInputSteps * edge_model::kSoilFeatureCount, f) !=
      edge_model::kSoilInputSteps * edge_model::kSoilFeatureCount) {
    std::fclose(f);
    return 5;
  }
  std::fclose(f);

  float et0Out = 0.0f;
  float soilOut[edge_model::kSoilOutputSteps] = {};
  const bool et0Ok = edge_model::predictEt0(et0, &et0Out);
  const bool soilOk = edge_model::predictSoil(soil, soilOut);

  // Output layout: one float (et0Ok flag), one float (et0Mm), one float
  // (soilOk flag), then 12 floats (soil moisture points).
  const float flagsAndEt0[3] = {et0Ok ? 1.0f : 0.0f, et0Out, soilOk ? 1.0f : 0.0f};
  std::fwrite(flagsAndEt0, sizeof(float), 3, stdout);
  std::fwrite(soilOut, sizeof(float), edge_model::kSoilOutputSteps, stdout);
  return 0;
}
