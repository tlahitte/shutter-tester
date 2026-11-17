/*
 * Shutter tester firmware, rev B sensor board (ESP32-C6-WROOM-1-N8)
 *
 * Derived from Ben Pirt's three-point shutter tester
 * (https://github.com/bjpirt/shutter-tester, MIT licence). The BLE protocol is
 * kept compatible with the upstream sketch in ../shutter-tester so the existing
 * web UI works unchanged.
 *
 * Build: Arduino core "esp32 by Espressif" 3.x, FQBN esp32:esp32:esp32c6 with
 * "USB CDC On Boot: Enabled" (CDCOnBoot=cdc) so Serial is the native USB port.
 * Serial monitor: 115200 (the USB CDC port ignores the baud rate, but 115200 is
 * the documented value; the upstream README's 921600 was wrong).
 *
 * ---------------------------------------------------------------------------
 * STATUS: THIS FIRMWARE HAS ONLY BEEN COMPILED. IT HAS NEVER RUN ON HARDWARE.
 * Every timing constant, the calibration flow, the LED behaviour and the BLE
 * exchange are untested. Comments marked UNTESTED point at the places most
 * likely to need adjusting on a real board.
 * ---------------------------------------------------------------------------
 *
 * Signal chain per channel (three channels):
 *   photodiode -> transimpedance amp (idles at VREF = 1.65 V, moves up OR down
 *   with light, polarity not known until calibration) -> LM339 "+" input.
 *   LM339 "-" input = threshold from an RC filtered PWM pin (22k + 4.7uF,
 *   tau about 0.1 s). Comparator output (open collector, 3.3 V pull-up) is HIGH
 *   when amp output > threshold. About 15 mV hysteresis.
 *
 * Sensor 2 is the centre of the frame. Sensors 1 and 3 sit on a diagonal,
 * 32 mm apart horizontally and 20 mm vertically (same as upstream; the UI does
 * the curtain speed maths from that geometry, the firmware only reports times).
 *
 * Differences from upstream, on purpose:
 *  - Edge times come from the MCPWM capture peripheral (hardware timestamps),
 *    not micros() inside GPIO interrupts, so BLE activity cannot add jitter.
 *  - Shot state lives in the main task only. The capture ISR just queues events,
 *    and values shared with the BLE task are std::atomic. Upstream shared plain
 *    non-volatile variables with its ISRs.
 *  - Upstream's LED logic used a chained comparison (a == b == c == X), which
 *    does not test what it looks like it tests. It is not copied; the white LED
 *    here flashes once per completed shot instead.
 *  - Upstream sent the metadata version without quotes ("version":2.2.0), which
 *    is not valid JSON and which the UI schema (version: string) rejects. This
 *    sketch sends "version":"3.0.0".
 */

#include <Arduino.h>
#include <atomic>
#include <Preferences.h>

#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>

#include "driver/gpio.h"
#include "driver/mcpwm_cap.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"

#define VERSION "3.0.0"

// ----------------------------------------------------------------------------
// Pins (rev B)
// ----------------------------------------------------------------------------

static const uint8_t NUM_CH = 3;
static const uint8_t CENTRE = 1;  // index of sensor 2, the centre sensor

static const uint8_t PIN_SIG[NUM_CH] = { 2, 3, 1 };     // amp outputs, ADC1 CH2 / CH3 / CH1
static const uint8_t PIN_PWM[NUM_CH] = { 18, 19, 20 };  // threshold PWM (LEDC)
static const uint8_t PIN_CMP[NUM_CH] = { 21, 22, 23 };  // comparator outputs (MCPWM capture)

static const uint8_t PIN_LED_RED = 16;    // active low
static const uint8_t PIN_LED_WHITE = 17;  // active low
static const uint8_t PIN_BOOT = 9;        // active low, external pull-up, strapping pin

// ----------------------------------------------------------------------------
// Tunables
// ----------------------------------------------------------------------------

// Threshold PWM. Arduino core 3.x clocks LEDC from the 40 MHz crystal on the
// C6, and 40 MHz / 2^11 = 19531 Hz divides exactly. 11 bits is 1.6 mV a step.
static const uint8_t PWM_BITS = 11;
static const uint32_t PWM_FREQ_HZ = 19531;
static const uint16_t PWM_MAX = (1u << PWM_BITS) - 1;
static const uint32_t SUPPLY_MV = 3300;  // nominal, only used to print mV figures

// RC filter settling. tau is about 0.1 s, so 0.6 s leaves about 0.25 % of the step.
static const uint32_t SETTLE_MS = 600;
static const uint32_t SETTLE_FIRST_MS = 1000;  // first search step can be a near full scale jump

// Requested capture resolution. The value actually granted is read back from
// the driver and used for every conversion. 10 MHz is 0.1 us a tick and the
// 32-bit capture counter wraps about every 7 minutes (handled, see extendTicks).
static const uint32_t CAPTURE_RES_REQUEST_HZ = 10000000;

static const uint32_t DEBOUNCE_US = 30;        // ignore edges this close to the previous edge
static const uint32_t SHOT_TIMEOUT_MS = 2500;  // incomplete shot timeout
static const uint32_t SHOT_QUIET_MS = 20;      // quiet time before a shot is reported, so a
                                               // bouncing shutter's last close edge is used

static const uint32_t DEFAULT_OFFSET_MV = 100;  // uncalibrated threshold distance from dark
static const uint32_t RAIL_MARGIN_MV = 150;     // lit ADC reading this close to a rail = saturated
static const uint32_t DIM_WARN_MV = 60;         // lit to dark difference below this = too dim
static const uint32_t DIM_FAIL_MV = 30;         // below this calibration is refused (2x hysteresis)

static const uint32_t CAL_CONFIRM_TIMEOUT_MS = 60000;
static const uint32_t LONG_PRESS_MS = 1000;
static const uint32_t SHOT_FLASH_MS = 60;
static const uint32_t ERROR_LED_MS = 1500;

// ----------------------------------------------------------------------------
// BLE protocol (service, sensor and mode characteristics identical to upstream)
// ----------------------------------------------------------------------------

#define SERVICE_UUID "42de79f1-7248-4c9b-9279-96509b8a9f5c"
#define SENSOR_CHARACTERISTIC_UUID "42de79f1-7248-4c9b-9279-96509b8a9f5d"
#define MODE_CHARACTERISTIC_UUID "42de79f1-7248-4c9b-9279-96509b8a9f5e"
#define CAL_CHARACTERISTIC_UUID "42de79f1-7248-4c9b-9279-96509b8a9f5f"  // new in rev B
#define DEVICE_NAME "ShutterTester"

// Values written to the mode characteristic, as sent by the UI (one byte).
static const uint8_t MODE_SINGLE_POINT = 1;
static const uint8_t MODE_THREE_POINT = 2;

// ----------------------------------------------------------------------------
// Types
// ----------------------------------------------------------------------------

// One comparator edge, queued by the capture ISR.
struct EdgeEvent {
  uint32_t ticks;    // hardware capture timestamp
  int64_t coarseUs;  // esp_timer time taken in the ISR, only used to count timer wraps
  uint8_t ch;
  bool high;  // true = rising edge (comparator went HIGH)
};

enum ChanState : uint8_t { CH_IDLE,
                           CH_OPEN,
                           CH_CLOSED };

struct Channel {
  // Configuration
  uint16_t thrDuty;  // working threshold, PWM counts
  bool openIsHigh;   // comparator level that means "light on the sensor"

  // Uncalibrated default only
  uint16_t darkDuty;   // dark level found at boot, PWM counts
  uint16_t darkAdcMv;  // ADC reading at boot
  bool thrAboveDark;   // which side of dark the default threshold is on

  // Settling after a threshold change: edges are ignored until muteUntilMs
  uint32_t muteUntilMs;
  bool sampleIdleAfterMute;  // then take the idle comparator level as "closed"

  // Current shot
  ChanState state;
  uint64_t openTick;      // first open edge of the shot
  uint64_t closeTick;     // last close edge of the shot
  uint64_t lastEdgeTick;  // last accepted edge, for the debounce
  uint32_t lastEdgeMs;
  bool haveEdge;
};

// Calibration as stored in NVS.
static const uint8_t WARN_SATURATED = 1;
static const uint8_t WARN_DIM = 2;

struct ChannelCal {
  uint16_t darkDuty;
  uint16_t litDuty;
  uint16_t thrDuty;
  uint16_t litAdcMv;
  uint16_t darkAdcMv;
  uint8_t litAbove;  // 1 = lit level is above dark level
  uint8_t warn;      // WARN_* bits
};

struct CalBlob {
  uint32_t magic;
  uint16_t layout;
  uint16_t pwmBits;
  ChannelCal ch[NUM_CH];
};

static const uint32_t CAL_MAGIC = 0x53485442;  // "SHTB"
static const uint16_t CAL_LAYOUT = 1;

enum Command : uint8_t { CMD_NONE,
                         CMD_CAL_START,
                         CMD_CAL_NEXT,
                         CMD_CAL_CANCEL,
                         CMD_STATUS,
                         CMD_ERASE,
                         CMD_MODE_SINGLE,
                         CMD_MODE_THREE,
                         CMD_HELP };

enum WhiteLed : uint8_t { WHITE_OFF,
                          WHITE_BLINK_SLOW,  // waiting for the user
                          WHITE_BLINK_FAST,  // measuring, do not touch
                          WHITE_ON };        // step done

// ----------------------------------------------------------------------------
// State
// ----------------------------------------------------------------------------

// Shared with the BLE task (callbacks run there), hence atomic.
static std::atomic<bool> g_connected{ false };
static std::atomic<bool> g_sendMetadata{ false };
static std::atomic<uint8_t> g_modeRequest{ 0 };              // 0 = none, else MODE_*
static std::atomic<uint8_t> g_bleCommand{ CMD_NONE };        // calibration command from BLE
static std::atomic<uint32_t> g_droppedEvents{ 0 };           // written by the capture ISR

// Everything below is only touched by the main (loop) task.
static Channel g_ch[NUM_CH];
static uint8_t g_mode = MODE_THREE_POINT;
static bool g_calibrated = false;
static uint8_t g_calWarn = 0;  // OR of the per channel WARN_* bits

static bool g_shotActive = false;
static uint32_t g_shotStartMs = 0;   // first open edge
static uint32_t g_firstCloseMs = 0;  // first close edge
static bool g_haveClose = false;
static uint32_t g_lastEdgeMs = 0;
static uint32_t g_glitches = 0;

static QueueHandle_t g_edgeQueue = nullptr;
static mcpwm_cap_timer_handle_t g_capTimer = nullptr;
static mcpwm_cap_channel_handle_t g_capChan[NUM_CH] = { nullptr, nullptr, nullptr };
static uint32_t g_capResHz = 0;  // actual capture timer resolution
static bool g_captureOk = false;

// Reference point for extending the 32-bit capture counter.
static bool g_haveTickBase = false;
static uint32_t g_baseTicks = 0;
static int64_t g_baseCoarseUs = 0;

static WhiteLed g_white = WHITE_OFF;
static uint32_t g_whiteFlashUntilMs = 0;
static uint32_t g_redUntilMs = 0;
static bool g_fault = false;  // hardware setup failed, red LED stays on

static bool g_wasConnected = false;
static bool g_buttonWasDown = false;
static bool g_buttonLongFired = false;
static uint32_t g_buttonDownMs = 0;

static BLEServer *g_server = nullptr;
static BLECharacteristic *g_sensorChar = nullptr;
static BLECharacteristic *g_modeChar = nullptr;
static BLECharacteristic *g_calChar = nullptr;
static Preferences g_prefs;
static char g_msg[200];

// ----------------------------------------------------------------------------
// Small helpers
// ----------------------------------------------------------------------------

static inline uint32_t dutyToMv(uint32_t duty) {
  return duty * SUPPLY_MV / PWM_MAX;
}

static inline uint16_t mvToDuty(uint32_t mv) {
  return (uint16_t)(mv * PWM_MAX / SUPPLY_MV);
}

static inline bool comparatorHigh(uint8_t ch) {
  return gpio_get_level((gpio_num_t)PIN_CMP[ch]) != 0;
}

// Majority vote over a few ms, so a single noisy sample cannot steer the search.
static bool comparatorHighFiltered(uint8_t ch) {
  int highs = 0;
  for (int i = 0; i < 15; i++) {
    if (comparatorHigh(ch)) highs++;
    delayMicroseconds(500);
  }
  return highs >= 8;
}

static uint16_t readSignalMv(uint8_t ch) {
  uint32_t sum = 0;
  for (int i = 0; i < 8; i++) sum += analogReadMilliVolts(PIN_SIG[ch]);
  return (uint16_t)(sum / 8);
}

static void setThreshold(uint8_t ch, uint16_t duty) {
  if (duty > PWM_MAX) duty = PWM_MAX;
  g_ch[ch].thrDuty = duty;
  ledcWrite(PIN_PWM[ch], duty);
}

static bool isRequired(uint8_t ch) {
  return g_mode == MODE_THREE_POINT || ch == CENTRE;
}

static const char *modeName() {
  return g_mode == MODE_THREE_POINT ? "three_point" : "single_point";
}

// ----------------------------------------------------------------------------
// LEDs (both active low)
// ----------------------------------------------------------------------------

static void serviceLeds() {
  uint32_t now = millis();

  // Red: error or light problem (uncalibrated, saturated, too dim), or a
  // recent error such as a shot timeout.
  bool red = g_fault || !g_calibrated || g_calWarn != 0 || (int32_t)(g_redUntilMs - now) > 0;
  digitalWrite(PIN_LED_RED, red ? LOW : HIGH);

  bool white = false;
  switch (g_white) {
    case WHITE_ON: white = true; break;
    case WHITE_BLINK_SLOW: white = (now % 1000) < 500; break;
    case WHITE_BLINK_FAST: white = (now % 200) < 100; break;
    case WHITE_OFF: white = (int32_t)(g_whiteFlashUntilMs - now) > 0; break;
  }
  digitalWrite(PIN_LED_WHITE, white ? LOW : HIGH);
}

static void flagError() {
  g_redUntilMs = millis() + ERROR_LED_MS;
}

// ----------------------------------------------------------------------------
// Capture (MCPWM)
// ----------------------------------------------------------------------------

// Runs in interrupt context. The timestamp was latched by hardware at the edge,
// so interrupt latency (BLE, flash writes) does not affect it. Known limit: if
// two edges on one channel arrive before this handler has run, the first
// capture value is overwritten and that edge is lost.
static bool IRAM_ATTR onCapture(mcpwm_cap_channel_handle_t, const mcpwm_capture_event_data_t *edata, void *user) {
  EdgeEvent ev;
  ev.ticks = edata->cap_value;
  ev.coarseUs = esp_timer_get_time();
  ev.ch = (uint8_t)(uintptr_t)user;
  ev.high = (edata->cap_edge == MCPWM_CAP_EDGE_POS);

  BaseType_t woken = pdFALSE;
  if (xQueueSendFromISR(g_edgeQueue, &ev, &woken) != pdTRUE) {
    g_droppedEvents.fetch_add(1, std::memory_order_relaxed);
  }
  return woken == pdTRUE;
}

static bool setupCapture() {
  g_edgeQueue = xQueueCreate(64, sizeof(EdgeEvent));
  if (!g_edgeQueue) return false;

  mcpwm_capture_timer_config_t timerCfg = {};
  timerCfg.group_id = 0;
  timerCfg.clk_src = MCPWM_CAPTURE_CLK_SRC_DEFAULT;
  timerCfg.resolution_hz = CAPTURE_RES_REQUEST_HZ;
  if (mcpwm_new_capture_timer(&timerCfg, &g_capTimer) != ESP_OK) {
    // UNTESTED fallback: let the driver pick its default resolution.
    timerCfg.resolution_hz = 0;
    if (mcpwm_new_capture_timer(&timerCfg, &g_capTimer) != ESP_OK) return false;
  }

  for (uint8_t i = 0; i < NUM_CH; i++) {
    mcpwm_capture_channel_config_t chanCfg = {};
    chanCfg.gpio_num = PIN_CMP[i];
    chanCfg.prescale = 1;
    chanCfg.flags.pos_edge = true;
    chanCfg.flags.neg_edge = true;
    // No internal pull: the board has a 3.3 V pull-up on each LM339 output.
    if (mcpwm_new_capture_channel(g_capTimer, &chanCfg, &g_capChan[i]) != ESP_OK) return false;

    mcpwm_capture_event_callbacks_t cbs = {};
    cbs.on_cap = onCapture;
    if (mcpwm_capture_channel_register_event_callbacks(g_capChan[i], &cbs, (void *)(uintptr_t)i) != ESP_OK) return false;
    if (mcpwm_capture_channel_enable(g_capChan[i]) != ESP_OK) return false;
  }

  if (mcpwm_capture_timer_enable(g_capTimer) != ESP_OK) return false;
  if (mcpwm_capture_timer_start(g_capTimer) != ESP_OK) return false;
  if (mcpwm_capture_timer_get_resolution(g_capTimer, &g_capResHz) != ESP_OK || g_capResHz == 0) return false;
  return true;
}

// The capture counter is 32 bits and wraps (about every 7 minutes at 10 MHz).
// Extend it to 64 bits: the low 32 bits come from the hardware timestamp, the
// number of whole wraps comes from the coarse esp_timer time taken in the ISR.
// The coarse time only has to be right to within half a wrap period, so ISR
// latency does not matter. Both clocks derive from the same crystal.
static uint64_t extendTicks(uint32_t ticks, int64_t coarseUs) {
  if (!g_haveTickBase) {
    g_haveTickBase = true;
    g_baseTicks = ticks;
    g_baseCoarseUs = coarseUs;
  }
  int64_t us = coarseUs - g_baseCoarseUs;
  if (us < 0) us = 0;
  int64_t expected = (us / 1000000) * (int64_t)g_capResHz + (us % 1000000) * (int64_t)g_capResHz / 1000000;
  uint32_t low = ticks - g_baseTicks;  // modulo 2^32 on purpose
  int64_t wraps = (expected - (int64_t)low + (1LL << 31)) >> 32;
  if (wraps < 0) wraps = 0;
  return ((uint64_t)wraps << 32) + low;
}

// Tick difference to microseconds, rounded, using the real timer resolution.
static long ticksToUs(uint64_t later, uint64_t earlier) {
  int64_t d = (int64_t)(later - earlier);
  int64_t half = g_capResHz / 2;
  int64_t us = (d >= 0) ? (d * 1000000 + half) / g_capResHz : -((-d * 1000000 + half) / g_capResHz);
  return (long)us;
}

static void flushEdges() {
  if (g_edgeQueue) xQueueReset(g_edgeQueue);
}

// ----------------------------------------------------------------------------
// BLE
// ----------------------------------------------------------------------------

class ServerCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer *) override {
    g_connected = true;
  }
  void onDisconnect(BLEServer *) override {
    g_connected = false;
  }
};

class SensorCallbacks : public BLECharacteristicCallbacks {
#if defined(CONFIG_NIMBLE_ENABLED)
  // Upstream sends the metadata message the moment a central connects, which
  // is normally before the UI has subscribed, so the UI never sees it. Sending
  // it again on subscribe fixes that without changing the message.
  void onSubscribe(BLECharacteristic *, ble_gap_conn_desc *, uint16_t subValue) override {
    if (subValue != 0) g_sendMetadata = true;
  }
#endif
};

class ModeCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic *c) override {
    String value = c->getValue();
    if (value.length() > 0) {
      uint8_t m = (uint8_t)value[0];
      if (m == MODE_SINGLE_POINT || m == MODE_THREE_POINT) g_modeRequest = m;
    }
  }
};

// Calibration commands: short JSON such as {"cmd":"start"}. Only the command
// word is looked for, so a bare "start" works too.
class CalCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic *c) override {
    String v = c->getValue();
    if (v.indexOf("start") >= 0) g_bleCommand = CMD_CAL_START;
    else if (v.indexOf("next") >= 0) g_bleCommand = CMD_CAL_NEXT;
    else if (v.indexOf("cancel") >= 0) g_bleCommand = CMD_CAL_CANCEL;
    else if (v.indexOf("status") >= 0) g_bleCommand = CMD_STATUS;
  }
};

static void setupBluetooth() {
  BLEDevice::init(DEVICE_NAME);

  g_server = BLEDevice::createServer();
  g_server->setCallbacks(new ServerCallbacks());

  BLEService *service = g_server->createService(SERVICE_UUID);

  g_sensorChar = service->createCharacteristic(
    SENSOR_CHARACTERISTIC_UUID,
    BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_NOTIFY | BLECharacteristic::PROPERTY_INDICATE);
  g_sensorChar->setCallbacks(new SensorCallbacks());

  g_modeChar = service->createCharacteristic(MODE_CHARACTERISTIC_UUID, BLECharacteristic::PROPERTY_WRITE);
  g_modeChar->setCallbacks(new ModeCallbacks());

  g_calChar = service->createCharacteristic(
    CAL_CHARACTERISTIC_UUID,
    BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_NOTIFY);
  g_calChar->setCallbacks(new CalCallbacks());

  // Upstream added BLE2902 descriptors by hand. The C6 uses the NimBLE stack,
  // which creates the client configuration descriptor itself for notify and
  // indicate characteristics, so nothing is added here.

  service->start();

  BLEAdvertising *adv = BLEDevice::getAdvertising();
  adv->addServiceUUID(SERVICE_UUID);
  // The 128-bit service UUID fills most of the advertising packet, so the
  // device name goes in the scan response (upstream had this switched off).
  adv->setScanResponse(true);
  adv->setMinPreferred(0x0);
  BLEDevice::startAdvertising();
}

// Every measurement and metadata message goes out as one JSON line on USB
// serial as well as over BLE, so the web UI can connect over USB (Web Serial).
static void notifySensor(const char *json) {
  Serial.println(json);
  if (!g_connected) return;
  g_sensorChar->setValue(json);
  g_sensorChar->notify();
}

static void sendMetadata() {
  snprintf(g_msg, sizeof(g_msg), "{\"type\":\"metadata\",\"mode\":\"%s\",\"version\":\"%s\"}", modeName(), VERSION);
  notifySensor(g_msg);
}

// Calibration status: one short JSON object, printed on serial as well. Kept
// short because a notification carries at most MTU - 3 bytes.
static void calStatus(const char *state, const char *text) {
  Serial.printf("[cal] %s: %s\r\n", state, text);
  char buf[160];
  snprintf(buf, sizeof(buf), "{\"type\":\"cal\",\"state\":\"%s\",\"msg\":\"%s\"}", state, text);
  g_calChar->setValue(buf);
  if (g_connected) g_calChar->notify();
}

static void calIdleStatus() {
  char buf[160];
  snprintf(buf, sizeof(buf), "{\"type\":\"cal\",\"state\":\"idle\",\"calibrated\":%s,\"warn\":%u}",
           g_calibrated ? "true" : "false", (unsigned)g_calWarn);
  g_calChar->setValue(buf);
  if (g_connected) g_calChar->notify();
}

// ----------------------------------------------------------------------------
// Commands: serial, BLE and the BOOT button
// ----------------------------------------------------------------------------

static void printHelp() {
  Serial.println("Commands: c = calibrate, n = next calibration step, x = cancel calibration,");
  Serial.println("          s = status, e = erase calibration, 1 = single point, 3 = three point, ? = help");
}

// Returns at most one command per call. During calibration a short press of
// BOOT means "next"; otherwise a long press (about 1 s) starts calibration.
static Command readCommand(bool inCalibration) {
  uint8_t ble = g_bleCommand.exchange(CMD_NONE);
  if (ble != CMD_NONE) return (Command)ble;

  while (Serial.available() > 0) {
    int c = Serial.read();
    switch (c) {
      case 'c': return inCalibration ? CMD_CAL_NEXT : CMD_CAL_START;
      case 'n': return CMD_CAL_NEXT;
      case 'x': return CMD_CAL_CANCEL;
      case 's': return CMD_STATUS;
      case 'e': return CMD_ERASE;
      case '1': return CMD_MODE_SINGLE;
      case '3': return CMD_MODE_THREE;
      case '?': return CMD_HELP;
      default: break;  // ignore line endings and anything else
    }
  }

  bool down = digitalRead(PIN_BOOT) == LOW;
  uint32_t now = millis();
  Command cmd = CMD_NONE;
  if (down && !g_buttonWasDown) {
    g_buttonDownMs = now;
    g_buttonLongFired = false;
  } else if (down && !g_buttonLongFired && now - g_buttonDownMs >= LONG_PRESS_MS) {
    g_buttonLongFired = true;
    if (!inCalibration) cmd = CMD_CAL_START;
  } else if (!down && g_buttonWasDown && !g_buttonLongFired && now - g_buttonDownMs >= 30) {
    if (inCalibration) cmd = CMD_CAL_NEXT;
  }
  g_buttonWasDown = down;
  return cmd;
}

// ----------------------------------------------------------------------------
// Threshold search
// ----------------------------------------------------------------------------

// Wait while keeping the LEDs alive. If cancellable, returns false as soon as
// a cancel command arrives.
static bool waitMs(uint32_t ms, bool cancellable) {
  uint32_t start = millis();
  while (millis() - start < ms) {
    serviceLeds();
    if (cancellable && readCommand(true) == CMD_CAL_CANCEL) return false;
    delay(5);
  }
  return true;
}

// Find, for all three channels at once, the PWM duty at which the comparator
// flips, i.e. the present amp output level expressed in PWM counts. Only the
// comparator is used, so ADC accuracy does not come into it. The light must
// not change during the search (about 7 s). Accuracy is limited by the
// comparator hysteresis (about +/- 8 mV).
// Returns false if cancelled.
static bool searchLevels(uint16_t result[NUM_CH], bool cancellable) {
  uint16_t lo[NUM_CH], hi[NUM_CH];
  for (uint8_t i = 0; i < NUM_CH; i++) {
    lo[i] = 0;
    hi[i] = PWM_MAX;
  }
  for (uint8_t step = 0; step < PWM_BITS; step++) {
    for (uint8_t i = 0; i < NUM_CH; i++) ledcWrite(PIN_PWM[i], (lo[i] + hi[i] + 1) / 2);
    if (!waitMs(step == 0 ? SETTLE_FIRST_MS : SETTLE_MS, cancellable)) return false;
    for (uint8_t i = 0; i < NUM_CH; i++) {
      uint16_t mid = (lo[i] + hi[i] + 1) / 2;
      if (comparatorHighFiltered(i)) lo[i] = mid;  // signal is above this threshold
      else hi[i] = mid;
    }
  }
  for (uint8_t i = 0; i < NUM_CH; i++) result[i] = (lo[i] + hi[i]) / 2;
  return true;
}

// Put the working thresholds back on the pins and ignore edges until the RC
// filters have settled.
static void applyThresholds(bool sampleIdle) {
  uint32_t until = millis() + SETTLE_FIRST_MS;
  for (uint8_t i = 0; i < NUM_CH; i++) {
    setThreshold(i, g_ch[i].thrDuty);
    g_ch[i].muteUntilMs = until;
    g_ch[i].sampleIdleAfterMute = sampleIdle;
  }
}

// ----------------------------------------------------------------------------
// Shot state
// ----------------------------------------------------------------------------

static void resetShot() {
  for (uint8_t i = 0; i < NUM_CH; i++) {
    g_ch[i].state = CH_IDLE;
    g_ch[i].haveEdge = false;
  }
  g_shotActive = false;
  g_haveClose = false;
}

// ----------------------------------------------------------------------------
// Calibration storage
// ----------------------------------------------------------------------------

static bool loadCalibration() {
  CalBlob blob;
  g_prefs.begin("shutter", true);
  size_t n = g_prefs.getBytes("cal", &blob, sizeof(blob));
  g_prefs.end();
  if (n != sizeof(blob) || blob.magic != CAL_MAGIC || blob.layout != CAL_LAYOUT || blob.pwmBits != PWM_BITS) return false;

  g_calWarn = 0;
  for (uint8_t i = 0; i < NUM_CH; i++) {
    g_ch[i].thrDuty = blob.ch[i].thrDuty;
    g_ch[i].openIsHigh = blob.ch[i].litAbove != 0;
    g_calWarn |= blob.ch[i].warn;
  }
  return true;
}

static void saveCalibration(const CalBlob &blob) {
  g_prefs.begin("shutter", false);
  g_prefs.putBytes("cal", &blob, sizeof(blob));
  g_prefs.end();
}

static void eraseCalibration() {
  g_prefs.begin("shutter", false);
  g_prefs.remove("cal");
  g_prefs.end();
}

// ----------------------------------------------------------------------------
// Uncalibrated default
// ----------------------------------------------------------------------------

// Used until a calibration exists. Assumes the sensors are dark at power-up:
// measure the dark level, then put the threshold a fixed offset above it. The
// idle comparator level is taken as "closed", so the first transition away from
// it counts as "open". If the light turns out to pull the signal DOWN, the
// comparator never trips; watchDefaultPolarity() spots that on the ADC and
// moves the threshold to the other side of dark.
// UNTESTED. If the unit is powered up with light on the sensors the default is
// wrong until a calibration is run. The red LED stays on while uncalibrated.
static void setupDefaultThresholds() {
  Serial.println("No calibration stored. Measuring dark level (keep the sensors dark, about 8 s)...");
  g_white = WHITE_BLINK_FAST;
  uint16_t dark[NUM_CH];
  searchLevels(dark, false);
  g_white = WHITE_OFF;

  uint16_t offset = mvToDuty(DEFAULT_OFFSET_MV);
  for (uint8_t i = 0; i < NUM_CH; i++) {
    g_ch[i].darkDuty = dark[i];
    g_ch[i].darkAdcMv = readSignalMv(i);
    g_ch[i].thrAboveDark = true;
    g_ch[i].thrDuty = (uint16_t)min<uint32_t>((uint32_t)dark[i] + offset, PWM_MAX);
    g_ch[i].openIsHigh = true;  // replaced by the idle sample after settling
    Serial.printf("Sensor %u: dark %lu mV (ADC %u mV), default threshold %lu mV\r\n",
                  i + 1, (unsigned long)dutyToMv(dark[i]), g_ch[i].darkAdcMv, (unsigned long)dutyToMv(g_ch[i].thrDuty));
  }
  applyThresholds(true);
}

// Uncalibrated mode only: if the ADC shows a channel's signal well on the far
// side of dark from its threshold, the light polarity is the other way round,
// so move the threshold across. The shot that revealed it is lost.
// UNTESTED, and limited by how fast loop() polls: very short exposures will
// not be noticed, so calibrate instead of relying on this.
static void watchDefaultPolarity() {
  uint32_t now = millis();
  uint16_t offset = mvToDuty(DEFAULT_OFFSET_MV);
  for (uint8_t i = 0; i < NUM_CH; i++) {
    Channel &c = g_ch[i];
    if ((int32_t)(c.muteUntilMs - now) > 0) continue;
    int32_t delta = (int32_t)analogReadMilliVolts(PIN_SIG[i]) - (int32_t)c.darkAdcMv;
    bool wrongSide = c.thrAboveDark ? (delta < -2 * (int32_t)DEFAULT_OFFSET_MV) : (delta > 2 * (int32_t)DEFAULT_OFFSET_MV);
    if (!wrongSide) continue;

    c.thrAboveDark = !c.thrAboveDark;
    uint16_t thr = c.thrAboveDark ? (uint16_t)min<uint32_t>((uint32_t)c.darkDuty + offset, PWM_MAX)
                                  : (uint16_t)(c.darkDuty > offset ? c.darkDuty - offset : 0);
    setThreshold(i, thr);
    c.muteUntilMs = now + SETTLE_FIRST_MS;
    // Light is on the sensor right now, so the idle level cannot be sampled.
    // Below dark means the comparator goes LOW when lit.
    c.sampleIdleAfterMute = false;
    c.openIsHigh = c.thrAboveDark;
    resetShot();
    Serial.printf("Sensor %u: light moves the signal %s. Default threshold moved, fire again.\r\n",
                  i + 1, c.thrAboveDark ? "up" : "down");
  }
}

// ----------------------------------------------------------------------------
// Calibration
// ----------------------------------------------------------------------------

enum Confirm : uint8_t { CONFIRMED,
                         CANCELLED,
                         TIMED_OUT };

static Confirm waitForConfirm() {
  uint32_t start = millis();
  while (millis() - start < CAL_CONFIRM_TIMEOUT_MS) {
    serviceLeds();
    Command cmd = readCommand(true);
    if (cmd == CMD_CAL_NEXT) return CONFIRMED;
    if (cmd == CMD_CAL_CANCEL) return CANCELLED;
    delay(5);
  }
  return TIMED_OUT;
}

// Leave calibration without changing anything: put the previous thresholds back.
static void abortCalibration(const uint16_t savedThr[NUM_CH], const char *state, const char *text) {
  for (uint8_t i = 0; i < NUM_CH; i++) g_ch[i].thrDuty = savedThr[i];
  applyThresholds(!g_calibrated);
  g_white = WHITE_OFF;
  flagError();
  calStatus(state, text);
  flushEdges();
  resetShot();
  calIdleStatus();
}

// Two steps, because lit and dark cannot both be present at once:
//   1. shutter open on Bulb with the light on  -> confirm -> lit level measured
//   2. shutter closed (or sensors covered)     -> confirm -> dark level measured
// "Confirm" is a short press of BOOT, serial 'n', or BLE {"cmd":"next"}.
// White LED: slow blink = waiting for the user, fast blink = measuring (keep
// still), solid = step done. Cancel or a 60 s timeout leaves the old
// calibration in place.
// UNTESTED on hardware.
static void runCalibration() {
  uint16_t savedThr[NUM_CH];
  for (uint8_t i = 0; i < NUM_CH; i++) savedThr[i] = g_ch[i].thrDuty;

  // If started by a long press, wait for the button to be let go so the
  // release is not taken as a confirmation.
  while (digitalRead(PIN_BOOT) == LOW) {
    serviceLeds();
    delay(5);
  }
  g_buttonWasDown = false;

  uint16_t lit[NUM_CH], dark[NUM_CH], litAdc[NUM_CH], darkAdc[NUM_CH];

  // Step 1: lit
  g_white = WHITE_BLINK_SLOW;
  calStatus("wait_light", "Open shutter on Bulb, light on, then confirm");
  Confirm r = waitForConfirm();
  if (r != CONFIRMED) {
    abortCalibration(savedThr, r == CANCELLED ? "cancelled" : "timeout", "Calibration aborted, nothing changed");
    return;
  }
  g_white = WHITE_BLINK_FAST;
  calStatus("measuring_light", "Measuring lit level, keep still");
  if (!searchLevels(lit, true)) {
    abortCalibration(savedThr, "cancelled", "Calibration aborted, nothing changed");
    return;
  }
  for (uint8_t i = 0; i < NUM_CH; i++) litAdc[i] = readSignalMv(i);
  g_white = WHITE_ON;
  waitMs(1000, false);

  // Step 2: dark
  g_white = WHITE_BLINK_SLOW;
  calStatus("wait_dark", "Close shutter or cover sensors, then confirm");
  r = waitForConfirm();
  if (r != CONFIRMED) {
    abortCalibration(savedThr, r == CANCELLED ? "cancelled" : "timeout", "Calibration aborted, nothing changed");
    return;
  }
  g_white = WHITE_BLINK_FAST;
  calStatus("measuring_dark", "Measuring dark level, keep still");
  if (!searchLevels(dark, true)) {
    abortCalibration(savedThr, "cancelled", "Calibration aborted, nothing changed");
    return;
  }
  for (uint8_t i = 0; i < NUM_CH; i++) darkAdc[i] = readSignalMv(i);

  // Work out threshold, polarity and warnings.
  CalBlob blob = {};
  blob.magic = CAL_MAGIC;
  blob.layout = CAL_LAYOUT;
  blob.pwmBits = PWM_BITS;
  uint8_t warnAll = 0;
  for (uint8_t i = 0; i < NUM_CH; i++) {
    ChannelCal &cc = blob.ch[i];
    cc.darkDuty = dark[i];
    cc.litDuty = lit[i];
    cc.thrDuty = (uint16_t)(((uint32_t)dark[i] + lit[i]) / 2);  // 50 percent of the light level
    cc.litAbove = lit[i] > dark[i];
    cc.litAdcMv = litAdc[i];
    cc.darkAdcMv = darkAdc[i];

    uint32_t diffMv = dutyToMv(lit[i] > dark[i] ? lit[i] - dark[i] : dark[i] - lit[i]);
    if (litAdc[i] < RAIL_MARGIN_MV || litAdc[i] + RAIL_MARGIN_MV > SUPPLY_MV) cc.warn |= WARN_SATURATED;
    if (diffMv < DIM_WARN_MV) cc.warn |= WARN_DIM;
    warnAll |= cc.warn;

    char buf[160];
    snprintf(buf, sizeof(buf), "{\"type\":\"cal\",\"state\":\"result\",\"ch\":%u,\"dark\":%lu,\"lit\":%lu,\"thr\":%lu,\"adc\":%u,\"pol\":%d,\"warn\":%u}",
             i + 1, (unsigned long)dutyToMv(dark[i]), (unsigned long)dutyToMv(lit[i]), (unsigned long)dutyToMv(cc.thrDuty),
             litAdc[i], cc.litAbove ? 1 : -1, (unsigned)cc.warn);
    g_calChar->setValue(buf);
    if (g_connected) g_calChar->notify();
    Serial.printf("[cal] Sensor %u: dark %lu mV, lit %lu mV, threshold %lu mV, lit ADC %u mV, light moves signal %s\r\n",
                  i + 1, (unsigned long)dutyToMv(dark[i]), (unsigned long)dutyToMv(lit[i]), (unsigned long)dutyToMv(cc.thrDuty),
                  litAdc[i], cc.litAbove ? "up" : "down");
    if (cc.warn & WARN_SATURATED) Serial.printf("[cal] Sensor %u WARNING: saturated, move the light further away\r\n", i + 1);
    if (cc.warn & WARN_DIM) Serial.printf("[cal] Sensor %u WARNING: too dim, lit and dark differ by only %lu mV\r\n", i + 1, (unsigned long)diffMv);

    if (diffMv < DIM_FAIL_MV) {
      snprintf(buf, sizeof(buf), "Sensor %u: no usable light (%lu mV), nothing changed", i + 1, (unsigned long)diffMv);
      abortCalibration(savedThr, "failed", buf);
      return;
    }
  }

  // Apply, let the filters settle, then check that every comparator reads
  // "closed" (the sensors are still dark at this point).
  for (uint8_t i = 0; i < NUM_CH; i++) setThreshold(i, blob.ch[i].thrDuty);
  waitMs(SETTLE_FIRST_MS, false);
  for (uint8_t i = 0; i < NUM_CH; i++) {
    bool openLevel = blob.ch[i].litAbove != 0;
    if (comparatorHighFiltered(i) == openLevel) {
      char buf[96];
      snprintf(buf, sizeof(buf), "Sensor %u: check failed, did the light change? Nothing changed", i + 1);
      abortCalibration(savedThr, "failed", buf);
      return;
    }
  }

  saveCalibration(blob);
  for (uint8_t i = 0; i < NUM_CH; i++) {
    g_ch[i].openIsHigh = blob.ch[i].litAbove != 0;
    g_ch[i].muteUntilMs = millis();
    g_ch[i].sampleIdleAfterMute = false;
  }
  g_calibrated = true;
  g_calWarn = warnAll;

  g_white = WHITE_ON;
  calStatus("done", warnAll ? "Calibration stored, with warnings" : "Calibration stored");
  waitMs(1500, false);
  g_white = WHITE_OFF;
  flushEdges();
  resetShot();
  calIdleStatus();
}

// ----------------------------------------------------------------------------
// Output (serial text in the upstream style, BLE JSON identical to upstream)
// ----------------------------------------------------------------------------

static void print_shutter_timing(long t1, long t2, long t3) {
  Serial.print(" 1-2: ");
  Serial.print(t2 - t1);
  Serial.print(" uS || 2-3: ");
  Serial.print(t3 - t2);
  Serial.println(" uS");
}

static void print_exposure_timing(long open_time, long close_time) {
  long interval = close_time - open_time;
  Serial.print(interval);
  Serial.print(" uS  ||  ");
  Serial.print(interval / 1000.0f);
  Serial.print(" mS  ||  ");
  Serial.print("1 / ");
  Serial.print(interval > 0 ? (long)(1000000.0 / interval) : 0L);
  Serial.println(" S");
}

// Times are microseconds relative to the earlier of sensor 1 and sensor 3
// opening, exactly as upstream reports them over BLE.
static void threePointOutput(const long open[NUM_CH], const long close[NUM_CH]) {
  // List the sensors in the order the curtain reached them.
  uint8_t first = (open[0] < open[2]) ? 0 : 2;
  uint8_t last = 2 - first;

  Serial.println("\r\nShutter timing");
  Serial.print("Shutter 1: ");
  print_shutter_timing(open[first], open[CENTRE], open[last]);
  Serial.print("Shutter 2: ");
  print_shutter_timing(close[first], close[CENTRE], close[last]);

  Serial.println("\r\nExposure timing");
  Serial.print("Sensor 1: ");
  print_exposure_timing(open[first], close[first]);
  Serial.print("Sensor 2: ");
  print_exposure_timing(open[CENTRE], close[CENTRE]);
  Serial.print("Sensor 3: ");
  print_exposure_timing(open[last], close[last]);

  snprintf(g_msg, sizeof(g_msg),
           "{\"type\":\"three_point\",\"sensor1\":{\"open\":%ld,\"close\":%ld},\"sensor2\":{\"open\":%ld,\"close\":%ld},\"sensor3\":{\"open\":%ld,\"close\":%ld}}",
           open[0], close[0], open[1], close[1], open[2], close[2]);
  notifySensor(g_msg);
}

static void singlePointOutput(long openUs, long closeUs) {
  Serial.println("\r\nExposure timing");
  print_exposure_timing(openUs, closeUs);

  snprintf(g_msg, sizeof(g_msg), "{\"type\":\"single_point\",\"sensor2\":%ld}", closeUs - openUs);
  notifySensor(g_msg);
}

// ----------------------------------------------------------------------------
// Measurement
// ----------------------------------------------------------------------------

static void handleEdge(const EdgeEvent &ev) {
  Channel &c = g_ch[ev.ch];
  uint32_t evMs = (uint32_t)(ev.coarseUs / 1000);  // same clock as millis()

  // Always feed the tick extension so the wrap count stays anchored.
  uint64_t t = extendTicks(ev.ticks, ev.coarseUs);

  if (!isRequired(ev.ch)) return;
  if ((int32_t)(c.muteUntilMs - evMs) > 0 || c.sampleIdleAfterMute) return;  // threshold still settling

  // Debounce: ignore anything within 30 us of the last accepted edge on this channel.
  uint64_t debounceTicks = (uint64_t)DEBOUNCE_US * g_capResHz / 1000000;
  if (c.haveEdge && t - c.lastEdgeTick < debounceTicks) {
    g_glitches++;
    return;
  }

  bool isOpen = (ev.high == c.openIsHigh);
  if (isOpen) {
    if (c.state == CH_IDLE) {
      c.openTick = t;  // first open edge of the shot
      c.state = CH_OPEN;
      if (!g_shotActive) {
        g_shotActive = true;
        g_shotStartMs = evMs;
      }
    } else if (c.state == CH_CLOSED) {
      c.state = CH_OPEN;  // reopened (bounce): keep the first open time, wait for the last close
    } else {
      return;  // already open
    }
  } else {
    if (c.state == CH_IDLE) return;  // close with no open, e.g. the light being switched off
    c.closeTick = t;                 // the last close edge wins
    c.state = CH_CLOSED;
    if (!g_haveClose) {
      g_haveClose = true;
      g_firstCloseMs = evMs;
    }
  }
  c.haveEdge = true;
  c.lastEdgeTick = t;
  c.lastEdgeMs = evMs;
  g_lastEdgeMs = evMs;
}

// Build a list such as "sensor 1, sensor 3" from a channel bit mask.
static void channelList(uint8_t mask, char *out, size_t len) {
  out[0] = '\0';
  for (uint8_t i = 0; i < NUM_CH; i++) {
    if (!(mask & (1u << i))) continue;
    size_t used = strlen(out);
    snprintf(out + used, len - used, "%ssensor %u", used ? ", " : "", i + 1);
  }
}

static void abortShot(const char *what, uint8_t mask) {
  char list[48];
  channelList(mask, list, sizeof(list));
  Serial.printf("\r\nShot timed out after %lu ms, %s: %s\r\n", (unsigned long)SHOT_TIMEOUT_MS, what, list);
  Serial.println("Check the light covers every sensor, then recalibrate if it keeps happening.");
  flagError();
  resetShot();
}

static void serviceShot() {
  if (!g_shotActive) return;
  uint32_t now = millis();

  uint8_t notTriggered = 0, notClosed = 0, stuckOpen = 0;
  bool allClosed = true;
  for (uint8_t i = 0; i < NUM_CH; i++) {
    if (!isRequired(i)) continue;
    const Channel &c = g_ch[i];
    bool levelOpen = (comparatorHigh(i) == c.openIsHigh);
    if (c.state == CH_IDLE) notTriggered |= 1u << i;
    if (c.state != CH_CLOSED || levelOpen) {
      allClosed = false;
      if (c.state != CH_IDLE) notClosed |= 1u << i;
    }
    // Recorded as open but the pin has read closed for a long time: the close
    // edge was lost (swallowed by the debounce or overwritten in the capture
    // register). There is no timestamp for it, so the shot cannot be used.
    if (c.state == CH_OPEN && !levelOpen && now - c.lastEdgeMs > SHOT_TIMEOUT_MS) stuckOpen |= 1u << i;
  }

  if (allClosed && now - g_lastEdgeMs >= SHOT_QUIET_MS) {
    // Complete. Reference time is the earlier of sensor 1 and 3 opening.
    if (g_mode == MODE_THREE_POINT) {
      uint64_t ref = min(g_ch[0].openTick, g_ch[2].openTick);
      long open[NUM_CH], close[NUM_CH];
      for (uint8_t i = 0; i < NUM_CH; i++) {
        open[i] = ticksToUs(g_ch[i].openTick, ref);
        close[i] = ticksToUs(g_ch[i].closeTick, ref);
      }
      threePointOutput(open, close);
    } else {
      singlePointOutput(0, ticksToUs(g_ch[CENTRE].closeTick, g_ch[CENTRE].openTick));
    }
    g_whiteFlashUntilMs = now + SHOT_FLASH_MS;
    resetShot();
    return;
  }

  // Timeouts. A channel that is simply still open does not time out, so long
  // exposures can be measured. What times out is: a sensor that never saw the
  // shutter open 2.5 s after the first one did, or one that has not closed
  // 2.5 s after the first one did.
  if (notTriggered && now - g_shotStartMs > SHOT_TIMEOUT_MS) {
    abortShot("did not trigger", notTriggered);
  } else if (g_haveClose && now - g_firstCloseMs > SHOT_TIMEOUT_MS) {
    abortShot("did not close", notClosed);
  } else if (stuckOpen) {
    abortShot("missed the close edge", stuckOpen);
  }
}

// After a threshold change in uncalibrated mode, take the idle comparator
// level as "closed" once the filter has settled.
static void serviceIdleSampling() {
  uint32_t now = millis();
  for (uint8_t i = 0; i < NUM_CH; i++) {
    Channel &c = g_ch[i];
    if (c.sampleIdleAfterMute && (int32_t)(c.muteUntilMs - now) <= 0) {
      c.openIsHigh = !comparatorHighFiltered(i);
      c.sampleIdleAfterMute = false;
    }
  }
}

static void printStatus() {
  Serial.printf("Version %s, mode %s, %s, capture %lu Hz, BLE %s\r\n", VERSION, modeName(),
                g_calibrated ? "calibrated" : "NOT calibrated (default thresholds)",
                (unsigned long)g_capResHz, g_connected ? "connected" : "not connected");
  for (uint8_t i = 0; i < NUM_CH; i++) {
    Serial.printf("Sensor %u: threshold %lu mV, signal now %u mV (ADC), comparator %s, open = %s\r\n",
                  i + 1, (unsigned long)dutyToMv(g_ch[i].thrDuty), readSignalMv(i),
                  comparatorHigh(i) ? "HIGH" : "LOW", g_ch[i].openIsHigh ? "HIGH" : "LOW");
  }
  if (g_calWarn & WARN_SATURATED) Serial.println("Warning: a sensor was saturated at calibration. Move the light away and recalibrate.");
  if (g_calWarn & WARN_DIM) Serial.println("Warning: a sensor was too dim at calibration. Move the light closer and recalibrate.");
  Serial.printf("Glitches ignored: %lu, capture events dropped: %lu\r\n",
                (unsigned long)g_glitches, (unsigned long)g_droppedEvents.load());
  calIdleStatus();
}

static void setMode(uint8_t mode) {
  if (mode == g_mode) return;
  g_mode = mode;
  resetShot();
  Serial.println(mode == MODE_SINGLE_POINT ? "Setting mode to single point" : "Setting mode to three point");
}

// ----------------------------------------------------------------------------
// setup / loop
// ----------------------------------------------------------------------------

void setup() {
  // LEDs off (active low) before the pins become outputs.
  digitalWrite(PIN_LED_RED, HIGH);
  digitalWrite(PIN_LED_WHITE, HIGH);
  pinMode(PIN_LED_RED, OUTPUT);
  pinMode(PIN_LED_WHITE, OUTPUT);
  digitalWrite(PIN_LED_RED, HIGH);
  digitalWrite(PIN_LED_WHITE, HIGH);
  pinMode(PIN_BOOT, INPUT_PULLUP);

  Serial.begin(115200);  // native USB CDC: the number is ignored, 115200 is the documented value
  Serial.println("");
  Serial.println("Shutter tester rev B, firmware " VERSION);

  analogSetAttenuation(ADC_11db);  // full 0 to 3.3 V range on the SIG pins

  for (uint8_t i = 0; i < NUM_CH; i++) {
    if (!ledcAttach(PIN_PWM[i], PWM_FREQ_HZ, PWM_BITS)) {
      Serial.printf("ERROR: threshold PWM setup failed on GPIO%u\r\n", PIN_PWM[i]);
      g_fault = true;
    }
    ledcWrite(PIN_PWM[i], PWM_MAX / 2);
  }

  g_captureOk = setupCapture();
  if (!g_captureOk) {
    Serial.println("ERROR: MCPWM capture setup failed, no measurements possible");
    g_fault = true;
  } else {
    Serial.printf("Capture timer resolution: %lu Hz\r\n", (unsigned long)g_capResHz);
  }

  setupBluetooth();

  g_calibrated = loadCalibration();
  if (g_calibrated) {
    Serial.println("Calibration loaded.");
    applyThresholds(false);
  } else {
    setupDefaultThresholds();
  }

  flushEdges();
  resetShot();
  calIdleStatus();
  printHelp();
  Serial.println("Ready to measure");
}

void loop() {
  // Edges. Waiting on the queue for up to 1 ms also paces the loop.
  EdgeEvent ev;
  if (g_captureOk && xQueueReceive(g_edgeQueue, &ev, pdMS_TO_TICKS(1)) == pdTRUE) {
    do {
      handleEdge(ev);
    } while (xQueueReceive(g_edgeQueue, &ev, 0) == pdTRUE);
  } else if (!g_captureOk) {
    delay(1);
  }

  serviceIdleSampling();
  serviceShot();
  if (!g_calibrated) watchDefaultPolarity();
  serviceLeds();

  // Mode changes from the UI.
  uint8_t modeRequest = g_modeRequest.exchange(0);
  if (modeRequest) setMode(modeRequest);

  switch (readCommand(false)) {
    case CMD_CAL_START: runCalibration(); break;
    case CMD_STATUS: printStatus(); break;
    case CMD_MODE_SINGLE: setMode(MODE_SINGLE_POINT); break;
    case CMD_MODE_THREE: setMode(MODE_THREE_POINT); break;
    case CMD_HELP: printHelp(); break;
    case CMD_ERASE:
      eraseCalibration();
      Serial.println("Calibration erased. Restarting.");
      delay(200);
      ESP.restart();
      break;
    default: break;
  }

  // Connection changes.
  bool connected = g_connected;
  if (!connected && g_wasConnected) {
    Serial.println("Device disconnected.");
    g_server->startAdvertising();
    Serial.println("Start advertising");
  }
  if (connected && !g_wasConnected) {
    Serial.println("Device Connected");
    g_sendMetadata = true;  // same moment as upstream; repeated when the UI subscribes
  }
  g_wasConnected = connected;

  if (g_sendMetadata.exchange(false)) sendMetadata();
}
