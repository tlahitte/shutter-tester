# Shutter tester rev B hardware

Derived from Ben Pirt's three-point shutter tester
(https://github.com/bjpirt/shutter-tester), hardware under CERN-OHL-P v2. The upstream
KiCad 8 board is still here as `shutter-tester.kicad_*` for reference.

Rev B is two 2-layer boards that arrive fully assembled: a 77 x 34 mm sensor board and a
50 x 50 mm light board. Nothing to solder.

| Board | Folder | What it does |
|-------|--------|--------------|
| Sensor | `sensor-board/` | Sits at the film plane. Three photodiodes on the frame diagonal, ESP32-C6, USB-C |
| Light | `light-board/` | Nine white LEDs, DC driven from any USB-C charger, goes in front of the lens |

## Sensor board

- 3 x Vishay VBPW34SR photodiodes mounted on the component side, each looking through a
  1.2 mm plated hole. The camera-facing side is flat with no parts on it.
- Each photodiode feeds a transimpedance amp (MCP6004, 1M, about 23 kHz bandwidth) and
  an LM339 comparator. The firmware sets each comparator threshold with a filtered PWM
  output, so calibration is automatic and there are no trimmers.
- ESP32-C6-WROOM-1-N8 timestamps comparator edges in hardware (MCPWM capture), so timing
  does not depend on interrupt latency.
- USB-C for power, flashing and serial. BOOT button for manual download mode if needed.
- The board is 34 mm tall so it drops into the film channel and lies on the film rails.
  The frame centre is 25 mm from the USB end, so the plug lies in the channel on one
  side of the gate. The electronics reach 52 mm the other way and sit over the cassette
  chamber. That fits the film cavity of a normal 35 mm SLR or rangefinder; it is too long
  for a compact, where it has to be held against the back instead.
- Sensor positions on the board: (9,7), (25,17), (41,27) mm. Silk on the camera side
  shows the 36 x 24 mm frame outline.

## Light board

9 LEDs at about 21 mA each, straight from USB 5 V through 100R. No PWM anywhere, so the
light has no flicker that could be read as a shutter edge. Four 3.2 mm mounting holes.
If the sensor board reports saturation, move the light further away.

## Light levels

Estimates only, nothing measured. A photodiode behind a 1.2 mm aperture gives roughly
1.3 mV per lux of white LED light at the amplifier output, and the output clips at about
1.6 V. With the light board held against the front of the lens that works out to:

| Lens setting | Centre sensor | Corner sensors |
|--------------|--------------:|---------------:|
| f/2          | clips, move the light back | about 1 V |
| f/2.8        | about 1.2 V   | about 0.5 V |
| f/5.6        | about 0.3 V   | about 0.12 V |
| f/8          | about 0.15 V  | about 0.06 V |

So test with the lens between f/2.8 and f/8. Calibration needs at least 30 mV between dark
and lit and warns below 60 mV. With the lens off, start with the light 20 cm away. If the
firmware reports saturation, move the light back; if it reports too dim, open the lens up.

## Regenerating

The KiCad files are generated. Change the Python, not the KiCad files.

    . scripts/_env.sh sensor-board
    (cd hardware/gen && "$KICAD_PY" sensor_board.py)    # or light_board.py
    scripts/check.sh sensor-board                       # ERC + DRC, must be clean
    scripts/render.sh sensor-board front                # PNG in fab/sensor-board/
    scripts/fab.sh sensor-board                         # fab/sensor-board/jlcpcb/

`fab.sh` ends by running `scripts/cplcheck.py`. It downloads JLCPCB's own footprint for
every LCSC part on the BOM, lays it on the board at the position and rotation written in
`cpl.csv`, and fails if a pad misses or a diode or IC ends up the wrong way round. The
rotation and origin corrections it checks live in the `FIX` table in `fab.sh`.

## Estimated cost

Estimate from 16 Nov 2026, in USD because that is what JLCPCB quotes in. Part prices are
live JLCPCB prices on that date. The fees are JLCPCB's usual economic assembly fees and
are not taken from a real quote, so treat the totals as a guide and check the quote page.

| Per order | Sensor board | Light board |
|-----------|-------------:|------------:|
| 5 bare PCBs (minimum) | $2 | $2 |
| Assembly setup + stencil | $9.50 | $9.50 |
| Extended parts fee ($3 per part type) | $15 (5 types) | $6 (2 types) |
| Parts, per assembled board | $7.30 | $0.34 |
| **Total, 2 boards assembled** | **about $42** | **about $19** |
| **Total, 5 boards assembled** | **about $64** | **about $20** |

One working set (2 of each, the minimum JLCPCB will assemble) comes to about $60 before
shipping. Add roughly $15 to $25 shipping to the UK and 20% VAT at checkout, so expect
somewhere around £70 to £85 delivered. Most of the cost is fixed fees, which is why a
single set is expensive per board and five sets are only about £100.

## Ordering at JLCPCB

Two separate orders, one per board. For each one:

1. jlcpcb.com, "Order now", upload `fab/<board>/jlcpcb/gerbers.zip`.
2. PCB options: 2 layers, 1.6 mm, qty 5, HASL lead-free. Black solder mask for the sensor
   board (less stray light), any colour for the light board. Leave the rest at default.
3. Turn on "PCB Assembly": Economic, Top side, qty 2 or 5, "Confirm parts placement" on.
4. Upload `bom.csv` and `cpl.csv`. Every part should match by LCSC number. If one is out
   of stock, stop and pick a substitute before going further.
5. In the placement preview, check before paying. `cplcheck.py` has already compared
   every placement with JLCPCB's footprints, so anything that looks wrong here is worth
   stopping for:
   - U2, U3 (SOIC-14): pin 1 dot matches the silk dot
   - U4 (SOT-223): tab on the wide pad
   - U1 (ESP32 module): antenna end at the board edge, over the bare area
   - J1 (USB-C): opening faces off the board and the two pegs sit in their holes
   - Sensor board LED1, LED2: cathode mark matches the silk
   - Light board D1 to D9: the large pad of the LED sits on the large board pad (the
     cathode, on the left)
   - D1 to D3 on the sensor board: body centred over the hole (either way round is fine,
     the circuit does not care about photodiode polarity)
6. Ship to the UK. Under £135, VAT is collected at checkout and there is nothing to pay
   on delivery.

## Not verified yet

- No board has been built. The front end gain is an estimate and the firmware has only
  been compiled, never run on hardware.
- The corrections written into `cpl.csv` (SOT-223 +180, SOIC +270, 2835 LED +180, USB-C
  origin moved 1.394 mm towards its pads) were worked out from JLCPCB's EasyEDA footprints
  for the exact parts and are checked by `cplcheck.py`. That assumes JLCPCB places parts the
  way its footprint is drawn, which is how it normally works but has not been confirmed
  with a real order, hence step 5.
- The photodiodes are clear packages on the open side of the board, so room light reaches
  them from behind. Expect to need a strip of black tape over D1 to D3, or a dim room.
- VBPW34SR stock at JLCPCB was 925 on 16 Nov 2026. It dropped to 10 in June 2026 and was
  restocked within a month, so recheck before ordering. There is no drop-in alternative.
- Bluetooth range with the board against a metal camera body is unknown. The antenna is
  at the far end over the cassette chamber. If Bluetooth is weak, use the USB connection
  in the web UI.
- Black solder mask at 1.6 mm with HASL is listed by JLCPCB as an economic assembly
  option. If the order page refuses it, pick green and plan on the black tape below.
- No ESD protection or fuse on USB. Fine for a bench prototype.
