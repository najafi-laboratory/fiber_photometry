# Online Recording Project

## Responsibilities

The recording application combines five responsibilities:

- represent user channel settings and build an FDM plan,
- program external excitation electronics,
- stream LabJack or mock samples,
- calculate live lock-in amplitudes,
- plot and save raw plus demodulated data.

The important design principle is separation between **configuration**, **hardware I/O**, **continuous numerical work**, and **user-interface coordination**. Hardware and demodulation cannot be allowed to freeze the Qt event loop, and file writing should not delay acquisition. That is why the project uses a plan object, device abstraction, Qt workers, and a separate writer thread.

## Project layout

| File | Responsibility |
| --- | --- |
| `run_fp.py` | Qt application entry point. |
| `core.py` | Configuration containers and coherent plan generation. |
| `devices.py` | Hardware protocols, real T7 streaming, synthetic data, and CSV playback. |
| `workers.py` | Acquisition, online processing, and LUT workers that run outside the GUI thread. |
| `gui.py` | GUI construction, plotting, session control, and threaded HDF5 writing. |

## Plan model

`ChannelSpec` is the editable source configuration. Excitation channels may carry an `AD9833Cfg`, `MCP4728Cfg`, and `ShiftVppCfg`. `build_plan()` converts enabled channel specs into a `DevicePlan` containing:

- `mode`: currently `FDM`,
- `Fs`: effective samples per channel per second,
- `L`: samples per acquisition block,
- `excitations`: enabled `ExcitationPlan` objects,
- `excitations_disabled`: disabled excitations forced to zero Vbias and Vpp,
- `scan_ain` and `scan_dio`: ordered physical input names,
- `k_array`: coherent Fourier-bin indices,
- `adjusted_freqs`: actual frequencies represented by those bins.

The generated waveform is `vbias + 0.5 * vpp * sin(2*pi*k*n/L)`. It is retained in the plan for mock synthesis or the dormant onboard-DAC path; the active real-hardware path programs external ICs.

## Hardware communication

### AD9833 frequency generation

The code converts frequency to a 28-bit tuning word using `round(f * 2^28 / MCLK)`, splits it into two 14-bit writes, and transmits reset, frequency, and run control words over LabJack SPI. Each excitation has an independent chip-select line.

### MCP4728 bias control

The code clamps requested voltage to 0 through 5.032 V, converts it to a 12-bit DAC code, and sends a three-byte channel-specific command over LabJack I2C. Writes to one I2C address are locked and separated by at least 60 ms.

### Shift-register Vpp control

The code shifts one six-bit value per enabled excitation in parallel. `IE`, `E1`, and `E2` use separate serial data lines and shared shift/latch clocks. The default bit order is least-significant bit first.

### LabJack stream

The T7 opens over USB and applies these global settings:

- single-ended AIN negative channel `199`,
- 10 V AIN range,
- stream resolution index 0,
- zero stream settling delay,
- internal stream clock.

`eStreamStart()` requests `L` scans per read. `read_chunk()` reshapes the flat returned values into `channels x L` float32 blocks and assigns a monotonically increasing starting sample index.

## Vpp LUT lifecycle

The LUT maps every code from 0 to 63 to measured peak-to-peak voltage for each excitation.

1. Set the same candidate code on every excitation shift register.
2. Wait 15 ms by default.
3. capture a short monitor waveform for plotting,
4. measure Vbias as the median and Vpp as the 0.5% to 99.5% quantile range,
5. store the measured Vpp in the channel LUT,
6. save all 64 values to `vpp_lut.json`.

Loading validates that every enabled Vpp-controlled excitation exists and has exactly 64 values. Extra channels in the file are discarded. Missing or malformed channels force a rebuild.

## Online demodulation

Online demodulation is a digital lock-in amplifier. A lock-in amplifier extracts the part of a noisy signal that oscillates at a known reference frequency. Signals at other frequencies multiply into alternating positive and negative values that mostly cancel after low-pass filtering. See [Concepts and Background](concepts.md) for an interactive illustration.

`ProcessWorker` identifies photodiode rows as scanned analog inputs that are not excitation monitors. For each sample and each excitation it:

1. selects the coherent sine/cosine reference value using the absolute sample index modulo `L`,
2. optionally subtracts the mean across photodiodes,
3. optionally removes a slow DC estimate,
4. multiplies the signal by cosine and sine references,
5. low-pass filters I and Q with the first exponential stage,
6. optionally low-pass filters them again,
7. calculates amplitude as `2 * sqrt(I^2 + Q^2)`,
8. emits one amplitude sample every `round(Fs / decim_hz)` raw samples.

The worker calculates a slow amplitude baseline internally, but the baseline is not subtracted and is not saved. The saved `/demod/amplitude` dataset contains absolute online lock-in amplitudes.

## Threading and queues

- `AcquireWorker` lives in the acquisition `QThread` and blocks in `read_chunk()`.
- `ProcessWorker` lives in a second `QThread`; Qt signals transfer each block to it.
- `H5Writer` is a Python daemon thread using a bounded queue of 16 blocks.
- Plot updates are throttled to 40 Hz, while every processed block is offered to the writer.

If the writer queue remains full for 0.5 s, the GUI drops that block and increments an in-memory queue-drop counter. This counter is printed but not written to HDF5. Separately, `H5Writer` detects discontinuities among blocks that do reach it and records them in `/events/drop_log`.

## Mock mode

`MockT7` supports two sources:

- **Synthetic:** excitation monitor rows reproduce the planned waveforms with noise; photodiode rows contain mixtures of all excitation frequencies; digital rows contain occasional synthetic pulses.
- **CSV playback:** the first column must be `timestamp`; remaining columns are read as measurement rows. Optional comment lines beginning with `# PLAN`, `# CHANNELS`, and `# CHANNEL_KEY` carry JSON metadata.

Playback timing follows the active plan's `L/Fs`, loops at end of file, and truncates rows when CSV and plan channel counts differ. The CSV timestamps are loaded but are not used by playback timing.

## GUI behavior

### Config tab

Edits channel values, selects mock versus real T7, loads mock CSV data, builds the plan, builds/aborts/verifies LUTs, and displays short excitation monitor traces.

### Calibration tab

Runs acquisition, online demodulation, and plotting without an HDF5 writer.

### Session tab

Runs the same pipeline with HDF5 logging. Plots include individual excitation monitors, all digital inputs, combined raw photodiodes, and one demodulation panel per photodiode.

Plot data is limited to the selected trailing window. Raw traces above 2,000 points are reduced to a 2,000-point min/max envelope to preserve spikes while keeping the UI responsive.

## Session shutdown

`on_stop()` requests device and processor stop, quits and waits up to two seconds for each Qt thread, then stops and joins the HDF5 writer for up to two seconds. Closing the main window invokes the same sequence.
