#include "edge_model.h"
#include "generated/model_data.h"

#include <math.h>
#include <string.h>

namespace {

using edge_model_data::kEt0InputSize;
using edge_model_data::kLstmHiddenSize;
using edge_model_data::kNBeatsHiddenSize;
using edge_model_data::kSoilFeatureCount;
using edge_model_data::kSoilInputSteps;
using edge_model_data::kSoilOutputSteps;

// Private function prototypes keep the data-only inference pipeline visible
// before its implementation below.
float readFlashFloat(const float *address);
float sigmoid(float value);
float clampPercent(float value);
void dense(const float *input, size_t inputSize, const float *weights,
           const float *bias, size_t outputSize, float *output);
void denseRelu(const float *input, size_t inputSize, const float *weights,
               const float *bias, size_t outputSize, float *output);
void nbeatsBlock(float residual[kEt0InputSize], float *forecast,
                 const float *body0Weight, const float *body0Bias,
                 const float *body1Weight, const float *body1Bias,
                 const float *body2Weight, const float *body2Bias,
                 const float *backcastWeight, const float *backcastBias,
                 const float *forecastWeight, const float *forecastBias);
void lstmStep(const float *input, size_t inputSize, const float *inputWeight,
              const float *recurrentWeight, const float *inputBias,
              const float *recurrentBias, float hidden[kLstmHiddenSize],
              float cell[kLstmHiddenSize], float output[kLstmHiddenSize]);

// Flash reads keep the generated weights and scaler constants out of RAM.
float readFlashFloat(const float *address) {
  float value = 0.0f;
  memcpy_P(&value, address, sizeof(value));
  return value;
}

float sigmoid(float value) {
  return 1.0f / (1.0f + expf(-value));
}

float clampPercent(float value) {
  if (value < 0.0f) {
    return 0.0f;
  }
  if (value > 100.0f) {
    return 100.0f;
  }
  return value;
}

void dense(const float *input, size_t inputSize, const float *weights,
           const float *bias, size_t outputSize, float *output) {
  for (size_t row = 0; row < outputSize; ++row) {
    float sum = readFlashFloat(bias + row);
    for (size_t column = 0; column < inputSize; ++column) {
      sum += readFlashFloat(weights + row * inputSize + column) * input[column];
    }
    output[row] = sum;
  }
}

void denseRelu(const float *input, size_t inputSize, const float *weights,
               const float *bias, size_t outputSize, float *output) {
  dense(input, inputSize, weights, bias, outputSize, output);
  for (size_t index = 0; index < outputSize; ++index) {
    if (output[index] < 0.0f) {
      output[index] = 0.0f;
    }
  }
}

void nbeatsBlock(float residual[kEt0InputSize], float *forecast,
                 const float *body0Weight, const float *body0Bias,
                 const float *body1Weight, const float *body1Bias,
                 const float *body2Weight, const float *body2Bias,
                 const float *backcastWeight, const float *backcastBias,
                 const float *forecastWeight, const float *forecastBias) {
  float hidden0[kNBeatsHiddenSize];
  float hidden1[kNBeatsHiddenSize];
  float hidden2[kNBeatsHiddenSize];
  float backcast[kEt0InputSize];
  float blockForecast[1];

  denseRelu(residual, kEt0InputSize, body0Weight, body0Bias,
            kNBeatsHiddenSize, hidden0);
  denseRelu(hidden0, kNBeatsHiddenSize, body1Weight, body1Bias,
            kNBeatsHiddenSize, hidden1);
  denseRelu(hidden1, kNBeatsHiddenSize, body2Weight, body2Bias,
            kNBeatsHiddenSize, hidden2);
  dense(hidden2, kNBeatsHiddenSize, backcastWeight, backcastBias,
        kEt0InputSize, backcast);
  dense(hidden2, kNBeatsHiddenSize, forecastWeight, forecastBias, 1,
        blockForecast);

  // This is the N-BEATS residual head: residual -= backcast and accumulate
  // each block forecast, in the same order as the PyTorch model.
  for (size_t index = 0; index < kEt0InputSize; ++index) {
    residual[index] -= backcast[index];
  }
  *forecast += blockForecast[0];
}

void lstmStep(const float *input, size_t inputSize, const float *inputWeight,
              const float *recurrentWeight, const float *inputBias,
              const float *recurrentBias, float hidden[kLstmHiddenSize],
              float cell[kLstmHiddenSize], float output[kLstmHiddenSize]) {
  float gates[4 * kLstmHiddenSize];
  for (size_t gate = 0; gate < 4 * kLstmHiddenSize; ++gate) {
    float value = readFlashFloat(inputBias + gate) +
                  readFlashFloat(recurrentBias + gate);
    for (size_t index = 0; index < inputSize; ++index) {
      value += readFlashFloat(inputWeight + gate * inputSize + index) * input[index];
    }
    for (size_t index = 0; index < kLstmHiddenSize; ++index) {
      value += readFlashFloat(recurrentWeight + gate * kLstmHiddenSize + index) * hidden[index];
    }
    gates[gate] = value;
  }

  // PyTorch nn.LSTM stores gates in i/f/g/o order. Its two bias vectors are
  // added before the nonlinearities, matching bias_ih + bias_hh here.
  for (size_t index = 0; index < kLstmHiddenSize; ++index) {
    const float inputGate = sigmoid(gates[index]);
    const float forgetGate = sigmoid(gates[kLstmHiddenSize + index]);
    const float cellGate = tanhf(gates[2 * kLstmHiddenSize + index]);
    const float outputGate = sigmoid(gates[3 * kLstmHiddenSize + index]);
    cell[index] = forgetGate * cell[index] + inputGate * cellGate;
    output[index] = outputGate * tanhf(cell[index]);
  }
  for (size_t index = 0; index < kLstmHiddenSize; ++index) {
    hidden[index] = output[index];
  }
}

}  // namespace

namespace edge_model {

bool predictEt0(const float input[kEt0InputSize], float *outputMm) {
  if (outputMm == nullptr) {
    return false;
  }
  float residual[kEt0InputSize];
  for (size_t index = 0; index < kEt0InputSize; ++index) {
    if (!isfinite(input[index])) {
      *outputMm = 0.0f;
      return false;
    }
    residual[index] = (input[index] - readFlashFloat(edge_model_data::kEt0ScalerMean)) /
                      readFlashFloat(edge_model_data::kEt0ScalerScale);
  }

  float forecast = 0.0f;
  nbeatsBlock(residual, &forecast,
              edge_model_data::kEt0Block0Body0Weight,
              edge_model_data::kEt0Block0Body0Bias,
              edge_model_data::kEt0Block0Body1Weight,
              edge_model_data::kEt0Block0Body1Bias,
              edge_model_data::kEt0Block0Body2Weight,
              edge_model_data::kEt0Block0Body2Bias,
              edge_model_data::kEt0Block0BackcastWeight,
              edge_model_data::kEt0Block0BackcastBias,
              edge_model_data::kEt0Block0ForecastWeight,
              edge_model_data::kEt0Block0ForecastBias);
  nbeatsBlock(residual, &forecast,
              edge_model_data::kEt0Block1Body0Weight,
              edge_model_data::kEt0Block1Body0Bias,
              edge_model_data::kEt0Block1Body1Weight,
              edge_model_data::kEt0Block1Body1Bias,
              edge_model_data::kEt0Block1Body2Weight,
              edge_model_data::kEt0Block1Body2Bias,
              edge_model_data::kEt0Block1BackcastWeight,
              edge_model_data::kEt0Block1BackcastBias,
              edge_model_data::kEt0Block1ForecastWeight,
              edge_model_data::kEt0Block1ForecastBias);
  nbeatsBlock(residual, &forecast,
              edge_model_data::kEt0Block2Body0Weight,
              edge_model_data::kEt0Block2Body0Bias,
              edge_model_data::kEt0Block2Body1Weight,
              edge_model_data::kEt0Block2Body1Bias,
              edge_model_data::kEt0Block2Body2Weight,
              edge_model_data::kEt0Block2Body2Bias,
              edge_model_data::kEt0Block2BackcastWeight,
              edge_model_data::kEt0Block2BackcastBias,
              edge_model_data::kEt0Block2ForecastWeight,
              edge_model_data::kEt0Block2ForecastBias);
  nbeatsBlock(residual, &forecast,
              edge_model_data::kEt0Block3Body0Weight,
              edge_model_data::kEt0Block3Body0Bias,
              edge_model_data::kEt0Block3Body1Weight,
              edge_model_data::kEt0Block3Body1Bias,
              edge_model_data::kEt0Block3Body2Weight,
              edge_model_data::kEt0Block3Body2Bias,
              edge_model_data::kEt0Block3BackcastWeight,
              edge_model_data::kEt0Block3BackcastBias,
              edge_model_data::kEt0Block3ForecastWeight,
              edge_model_data::kEt0Block3ForecastBias);

  const float scaled = forecast;
  const float restored = scaled * readFlashFloat(edge_model_data::kEt0ScalerScale) +
                         readFlashFloat(edge_model_data::kEt0ScalerMean);
  if (!isfinite(restored)) {
    *outputMm = 0.0f;
    return false;
  }
  *outputMm = restored > 0.0f ? restored : 0.0f;
  return true;
}

bool predictSoil(const float input[kSoilInputSteps][kSoilFeatureCount],
                 float outputPercent[kSoilOutputSteps]) {
  if (outputPercent == nullptr) {
    return false;
  }
  float hidden[2][kLstmHiddenSize] = {};
  float cell[2][kLstmHiddenSize] = {};
  float layer0Output[kLstmHiddenSize];
  float layer1Output[kLstmHiddenSize];
  float scaledInput[kSoilFeatureCount];

  for (size_t step = 0; step < kSoilInputSteps; ++step) {
    for (size_t feature = 0; feature < kSoilFeatureCount; ++feature) {
      if (!isfinite(input[step][feature])) {
        for (size_t output = 0; output < kSoilOutputSteps; ++output) {
          outputPercent[output] = 0.0f;
        }
        return false;
      }
      scaledInput[feature] =
          (input[step][feature] - readFlashFloat(edge_model_data::kSoilXScalerMean + feature)) /
          readFlashFloat(edge_model_data::kSoilXScalerScale + feature);
    }
    lstmStep(scaledInput, kSoilFeatureCount,
             edge_model_data::kSoilLstmLayer0InputWeight,
             edge_model_data::kSoilLstmLayer0RecurrentWeight,
             edge_model_data::kSoilLstmLayer0InputBias,
             edge_model_data::kSoilLstmLayer0RecurrentBias,
             hidden[0], cell[0], layer0Output);
    lstmStep(layer0Output, kLstmHiddenSize,
             edge_model_data::kSoilLstmLayer1InputWeight,
             edge_model_data::kSoilLstmLayer1RecurrentWeight,
             edge_model_data::kSoilLstmLayer1InputBias,
             edge_model_data::kSoilLstmLayer1RecurrentBias,
             hidden[1], cell[1], layer1Output);
  }

  float headHidden[kLstmHiddenSize];
  float headOutput[kSoilOutputSteps];
  denseRelu(layer1Output, kLstmHiddenSize,
            edge_model_data::kSoilHead0Weight,
            edge_model_data::kSoilHead0Bias,
            kLstmHiddenSize, headHidden);
  dense(headHidden, kLstmHiddenSize,
        edge_model_data::kSoilHead2Weight,
        edge_model_data::kSoilHead2Bias,
        kSoilOutputSteps, headOutput);

  const float lastScaledSoil =
      (input[kSoilInputSteps - 1][0] - readFlashFloat(edge_model_data::kSoilXScalerMean)) /
      readFlashFloat(edge_model_data::kSoilXScalerScale);
  for (size_t output = 0; output < kSoilOutputSteps; ++output) {
    const float scaledPrediction = lastScaledSoil + headOutput[output];
    const float restored = scaledPrediction * readFlashFloat(edge_model_data::kSoilYScalerScale) +
                           readFlashFloat(edge_model_data::kSoilYScalerMean);
    if (!isfinite(restored)) {
      for (size_t reset = 0; reset < kSoilOutputSteps; ++reset) {
        outputPercent[reset] = 0.0f;
      }
      return false;
    }
    outputPercent[output] = clampPercent(restored);
  }
  return true;
}

bool predict(const ModelInput &input, ModelOutput &output) {
  if (!predictEt0(input.et0, &output.et0Mm)) {
    return false;
  }
  return predictSoil(input.soil, output.soilMoisturePercent);
}

ModelMetadata metadata() {
  return {
      edge_model_data::kEt0ModelVersion,
      edge_model_data::kSoilModelVersion,
      edge_model_data::kEt0ModelSha256,
      edge_model_data::kEt0ScalerSha256,
      edge_model_data::kSoilModelSha256,
      edge_model_data::kSoilXScalerSha256,
      edge_model_data::kSoilYScalerSha256,
      edge_model_data::kArtifactManifestSha256,
  };
}

}  // namespace edge_model
