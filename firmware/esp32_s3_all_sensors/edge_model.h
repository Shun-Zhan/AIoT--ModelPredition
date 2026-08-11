#pragma once

#include <stddef.h>

namespace edge_model {

// Fixed model contracts. Inputs are raw sensor/model values in the order
// documented by kSoilFeatureNames in scripts/export_esp32_models.py.
static const size_t kEt0InputSize = 24;
static const size_t kSoilInputSteps = 288;
static const size_t kSoilFeatureCount = 9;
static const size_t kSoilOutputSteps = 12;

struct ModelInput {
  float et0[kEt0InputSize];
  float soil[kSoilInputSteps][kSoilFeatureCount];
};

struct ModelOutput {
  float et0Mm;
  float soilMoisturePercent[kSoilOutputSteps];
};

struct ModelMetadata {
  const char *et0ModelVersion;
  const char *soilModelVersion;
  const char *et0ModelSha256;
  const char *et0ScalerSha256;
  const char *soilModelSha256;
  const char *soilXScalerSha256;
  const char *soilYScalerSha256;
  const char *artifactManifestSha256;
};

// Predict one model synchronously. No heap, PSRAM, Python, or TFLite state is
// used; callers own the input and output buffers for the duration of the call.
bool predictEt0(const float input[kEt0InputSize], float *outputMm);
bool predictSoil(const float input[kSoilInputSteps][kSoilFeatureCount],
                float outputPercent[kSoilOutputSteps]);
bool predict(const ModelInput &input, ModelOutput &output);

// Return flash-resident version/hash pointers for diagnostics or telemetry.
ModelMetadata metadata();

}  // namespace edge_model
