/*
  WFS-E-NP006S-4 flow-meter pulse test

  Flow-meter wiring:
    red    -> regulated +5 V
    black  -> power GND and ESP32 GND
    yellow -> GPIO12 through a safe 3.3 V logic interface

  The datasheet specifies 515 pulses/L. This sketch assumes the yellow wire
  is an open-collector/open-drain pulse output with a pull-up to 3.3 V.
  Do not connect a verified 5 V push-pull signal directly to ESP32 GPIO12.

  Serial Monitor: 115200 baud
    r = reset pulse counter and accumulated volume
    s = print current values
    h = print help
*/

// -------------------- Private includes --------------------

#include <Arduino.h>

// -------------------- Private define --------------------

static const uint8_t FLOW_SIGNAL_PIN = 12;
static const uint32_t SERIAL_BAUD = 115200;
static const float FLOW_PULSES_PER_LITER = 515.0f;
static const uint32_t REPORT_INTERVAL_MS = 1000;
static const uint32_t MIN_PULSE_INTERVAL_US = 100;

// -------------------- Intermediate variables calculated by private functions --------------------

volatile uint32_t flowPulseCount = 0;
volatile uint32_t flowLastPulseUs = 0;
volatile uint32_t flowPeriodUs = 0;
uint32_t lastReportMs = 0;
uint32_t lastReportedPulseCount = 0;

// -------------------- Private function prototypes --------------------

void IRAM_ATTR onFlowPulse();
void printHelp();
void printFlowValues(bool markAsReported);
void resetFlowCounter();
void processSerialCommands();

// -------------------- Private user code --------------------

void IRAM_ATTR onFlowPulse() {
  const uint32_t nowUs = micros();
  const uint32_t previousUs = flowLastPulseUs;
  if (previousUs != 0 && nowUs - previousUs < MIN_PULSE_INTERVAL_US) {
    return;
  }
  if (previousUs != 0) {
    flowPeriodUs = nowUs - previousUs;
  }
  flowLastPulseUs = nowUs;
  ++flowPulseCount;
}

void printHelp() {
  Serial.println();
  Serial.println("=== WFS flow-meter pulse test ===");
  Serial.printf("Signal pin: GPIO%d\n", FLOW_SIGNAL_PIN);
  Serial.printf("Calibration: %.0f pulses/L\n", FLOW_PULSES_PER_LITER);
  Serial.println("r = reset pulse counter and accumulated volume");
  Serial.println("s = show current values");
  Serial.println("h = show this help");
  Serial.println("Safety: yellow signal must be 0-3.3 V at GPIO12.");
}

void printFlowValues(bool markAsReported) {
  uint32_t pulses;
  uint32_t periodUs;
  noInterrupts();
  pulses = flowPulseCount;
  periodUs = flowPeriodUs;
  interrupts();

  const uint32_t newPulses = pulses - lastReportedPulseCount;
  const float litersPerMinute =
      static_cast<float>(newPulses) * 60000.0f /
      (FLOW_PULSES_PER_LITER * static_cast<float>(REPORT_INTERVAL_MS));
  const float totalLiters = static_cast<float>(pulses) / FLOW_PULSES_PER_LITER;
  const float frequencyHz = periodUs > 0 ? 1000000.0f / periodUs : 0.0f;

  Serial.printf(
      "[FLOW] pulses=%lu | frequency=%.2f Hz | flow=%.3f L/min | "
      "total=%.4f L | level=%s\n",
      static_cast<unsigned long>(pulses), frequencyHz, litersPerMinute,
      totalLiters, digitalRead(FLOW_SIGNAL_PIN) ? "HIGH" : "LOW");

  if (markAsReported) {
    lastReportedPulseCount = pulses;
  }
}

void resetFlowCounter() {
  noInterrupts();
  flowPulseCount = 0;
  flowLastPulseUs = 0;
  flowPeriodUs = 0;
  interrupts();
  lastReportedPulseCount = 0;
  Serial.println("[FLOW] Counter reset.");
}

void processSerialCommands() {
  while (Serial.available() > 0) {
    const char command = static_cast<char>(Serial.read());
    switch (command) {
      case 'r':
      case 'R':
        resetFlowCounter();
        break;
      case 's':
      case 'S':
        printFlowValues(false);
        break;
      case 'h':
      case 'H':
      case '?':
        printHelp();
        break;
      case '\r':
      case '\n':
      case ' ':
        break;
      default:
        Serial.printf("Unknown command: %c\n", command);
        printHelp();
        break;
    }
  }
}

void setup() {
  Serial.begin(SERIAL_BAUD);
  delay(500);

  // The internal pull-up is suitable for a pulse output that sinks current.
  // Use an external 3.3 V pull-up or level shifter when the module requires it.
  pinMode(FLOW_SIGNAL_PIN, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(FLOW_SIGNAL_PIN), onFlowPulse, RISING);

  Serial.println();
  Serial.println("Flow-meter test started.");
  Serial.printf("WFS calibration: %.0f pulses/L\n", FLOW_PULSES_PER_LITER);
  Serial.printf("Pulse input: GPIO%d, RISING edge, 115200 baud\n", FLOW_SIGNAL_PIN);
  Serial.println("Start water flow and watch pulses/frequency/flow.");
  printHelp();
}

void loop() {
  processSerialCommands();

  const uint32_t nowMs = millis();
  if (nowMs - lastReportMs >= REPORT_INTERVAL_MS) {
    lastReportMs = nowMs;
    printFlowValues(true);
  }
  delay(2);
}
