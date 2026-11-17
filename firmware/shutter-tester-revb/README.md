# Shutter tester firmware, rev B

Firmware for the rev B sensor board (ESP32-C6-WROOM-1-N8, three photodiode channels with
comparators and PWM thresholds). It speaks the same BLE protocol as the upstream sketch in
`../shutter-tester`, so the existing web UI works without changes.

**Status: compiled only. It has never run on hardware.** Expect to tune timing constants
and the calibration flow once a board exists. The places most likely to need work are
marked `UNTESTED` in the sketch.

## What you need

- Arduino core "esp32 by Espressif Systems", version 3.x (built against 3.3.7)
- Board: ESP32C6 Dev Module (`esp32:esp32:esp32c6`)
- USB CDC On Boot: Enabled. Serial then goes over the module's native USB port.
- No extra libraries. BLE and Preferences ship with the core.

## Flashing with the Arduino IDE

1. Open Boards Manager, search for "esp32" and install "esp32 by Espressif Systems" 3.x.
2. Open `shutter-tester-revb.ino`.
3. Tools > Board > esp32 > ESP32C6 Dev Module.
4. Tools > USB CDC On Boot > Enabled.
5. Optional: Tools > Flash Size > 8MB (the N8 module has 8 MB; the 4 MB default also works).
6. Plug the board in over USB and pick its port under Tools > Port.
7. Click Upload.
8. Open the Serial Monitor at 115200.

If the upload cannot connect, put the chip in download mode by hand. The board has no
reset button, so unplug the USB cable, hold BOOT, plug the cable back in, then release
BOOT. Upload again, then unplug and replug the cable to start the sketch. A brand new
board may need this for its first upload.

## Flashing with arduino-cli

```sh
arduino-cli core update-index
arduino-cli core install esp32:esp32

# from the repository root
arduino-cli compile --fqbn esp32:esp32:esp32c6:CDCOnBoot=cdc firmware/shutter-tester-revb

arduino-cli board list                      # find the port, e.g. /dev/cu.usbmodem1101
arduino-cli upload -p /dev/cu.usbmodem1101 --fqbn esp32:esp32:esp32c6:CDCOnBoot=cdc firmware/shutter-tester-revb
arduino-cli monitor -p /dev/cu.usbmodem1101 -c baudrate=115200
```

To see every board option: `arduino-cli board details -b esp32:esp32:esp32c6`.

Apple Silicon note: arduino-cli's bundled `ctags` is an Intel binary. Without Rosetta the
compile stops with "bad CPU type in executable". Either install Rosetta
(`softwareupdate --install-rosetta`) or skip the ctags step, which this sketch does not
need because every function is defined before it is used:

```sh
arduino-cli compile --fqbn esp32:esp32:esp32c6:CDCOnBoot=cdc \
  --build-property "tools.ctags.cmd.path=/usr/bin/true" firmware/shutter-tester-revb
```

## Using it

Every measurement is also printed on USB serial as one JSON line (the same message
that goes out over Bluetooth), so the web UI can connect with its USB button (Web
Serial, Chrome or Edge) instead of Bluetooth. Idea taken from 9vvin's fork of the
original project.

Serial commands (single characters, 115200):

| Key | Action |
|-----|--------|
| `c` | Start calibration |
| `n` | Next calibration step (confirm) |
| `x` | Cancel calibration |
| `s` | Print status (thresholds, live ADC readings, comparator levels) |
| `e` | Erase the stored calibration and restart |
| `1` / `3` | Single point / three point mode |
| `?` | Help |

BOOT button: hold for about 1 second to start calibration. During calibration a short
press confirms the current step. Do not hold BOOT while resetting or plugging in, that
puts the chip in download mode.

LEDs:

- Red on: not calibrated, or the last calibration found a sensor saturated or too dim, or
  a recent error (shot timed out, calibration failed).
- White short flash: a shot was measured.
- White slow blink: calibration is waiting for you. Fast blink: measuring, keep everything
  still. Solid: step done.

### Calibration

1. Start it (`c`, long press of BOOT, or the BLE command below).
2. Open the shutter on Bulb with the light on, so all three sensors are lit. Confirm.
   The lit level is measured (about 7 seconds).
3. When prompted, close the shutter or cover the sensors. Confirm. The dark level is measured.
4. The threshold for each channel is set halfway between dark and lit and saved to flash.

If a step is not confirmed within 60 seconds, or you cancel, the previous calibration
stays in place. Warnings: "saturated" means the lit signal is within 150 mV of a supply
rail, so move the light further away. "Too dim" means lit and dark differ by less than
60 mV, so move the light closer. Below 30 mV the calibration is refused.

Before any calibration the firmware measures the dark level at power-up (keep the sensors
dark for about 8 seconds) and uses a threshold 100 mV away from it. That is a rough
default. Calibrate before trusting any numbers.

## BLE protocol

Unchanged from upstream:

- Device name `ShutterTester`, service `42de79f1-7248-4c9b-9279-96509b8a9f5c`
- Sensor characteristic `...9f5d` (notify): `three_point`, `single_point` and `metadata`
  JSON messages, times in microseconds
- Mode characteristic `...9f5e` (write): one byte, `1` = single point, `2` = three point

One deliberate fix: the metadata message now sends `"version":"3.0.0"` with quotes.
Upstream sent `"version":2.2.0`, which is not valid JSON and does not match the UI schema.
The metadata message is also sent again when the UI subscribes, because the copy sent on
connect arrives before the UI is listening.

### New: calibration characteristic

UUID `42de79f1-7248-4c9b-9279-96509b8a9f5f`, properties read, write, notify. The web UI
does not use it yet.

Write one of these (UTF-8 text):

```json
{"cmd":"start"}
{"cmd":"next"}
{"cmd":"cancel"}
{"cmd":"status"}
```

Notifications and reads return one short JSON object:

```json
{"type":"cal","state":"wait_light","msg":"Open shutter on Bulb, light on, then confirm"}
{"type":"cal","state":"measuring_light","msg":"Measuring lit level, keep still"}
{"type":"cal","state":"wait_dark","msg":"Close shutter or cover sensors, then confirm"}
{"type":"cal","state":"measuring_dark","msg":"Measuring dark level, keep still"}
{"type":"cal","state":"result","ch":1,"dark":1648,"lit":2210,"thr":1929,"adc":2204,"pol":1,"warn":0}
{"type":"cal","state":"done","msg":"Calibration stored"}
{"type":"cal","state":"idle","calibrated":true,"warn":0}
```

- `state` is one of `idle`, `wait_light`, `measuring_light`, `wait_dark`, `measuring_dark`,
  `result`, `done`, `failed`, `cancelled`, `timeout`.
- `result` is sent once per channel. `dark`, `lit` and `thr` are millivolts worked out from
  the PWM duty (nominal 3.3 V supply), `adc` is the lit ADC reading in millivolts, `pol` is
  `1` if light raises the signal and `-1` if it lowers it.
- `warn` is a bit field: `1` = saturated, `2` = too dim. In the `idle` message it is the
  OR of all channels.
- After `failed`, `cancelled` or `timeout` an `idle` message follows.

## Not tested yet

Everything that needs a board: capture timing accuracy, PWM threshold settling, the
calibration search, the uncalibrated default, LED polarity and behaviour, the BOOT button,
NVS storage, and the BLE exchange with the web UI.
