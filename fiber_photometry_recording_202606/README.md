# Fiber Photometry Recording

PyQt6 application for LabJack T7 based fiber photometry acquisition. The app builds an FDM acquisition plan, programs external excitation hardware, streams raw analog/digital samples, demodulates photodiode signals online, plots live traces, and saves raw plus demodulated data to HDF5.

## Environment

Create a fresh conda environment:

```powershell
conda create -n fiber-photometry python=3.11 -y
conda activate fiber-photometry
```

Install Python packages:

```powershell
pip install numpy h5py PyQt6 pyqtgraph labjack-ljm
```

Notes:
- `labjack-ljm` also requires the LabJack LJM driver installed on the computer.
- Without LabJack/LJM, the app can still run in mock mode for UI testing and playback.

Run:

```powershell
python run_fp.py
```

## Files

- `run_fp.py`: Entry point. Starts the Qt application and opens `MainWindow`.
- `core.py`: Small configuration objects and `build_plan()`.
  - `ChannelSpec`: User-facing channel settings.
  - `AD9833Cfg`, `MCP4728Cfg`, `ShiftVppCfg`: External IC wiring/settings.
  - `ExcitationPlan`, `DevicePlan`: Computed acquisition plan.
  - `choose_L_and_k()`: Picks coherent block length and frequency bins.
  - `build_plan()`: Converts GUI channel settings into a `DevicePlan`.
- `devices.py`: Hardware and mock device logic.
  - `MockT7`: Synthetic/playback data source.
  - `T7Device`: Real LabJack T7 interface.
  - AD9833 SPI, MCP4728 I2C, and Vpp shift-register helpers.
  - Monitor measurement, Vpp LUT calibration/loading, Vbias/Vpp target setting.
- `workers.py`: Background Qt workers.
  - `AcquireWorker`: Reads chunks from the selected device.
  - `ProcessWorker`: Performs online lock-in demodulation and sends raw/demod data onward.
  - `VppLutWorker`: Sweeps Vpp codes and measures LUT values.
- `gui.py`: GUI, plotting, and HDF5 writer.
  - `MainWindow`: Config, calibration, and session tabs.
  - `ChannelTable`: Editable channel table.
  - `MultiPlotAdapter`: Live raw/demod plot updates with legends.
  - `H5Writer`: Saves raw and demodulated results.

## Main Workflow

1. Start the app with `python run_fp.py`.
2. In the Config tab, edit channel enable states, frequencies, Vpp/Vbias, AINs, and DIOs.
3. Click `Build Plan`.
   - The app attaches default IC bindings for IE/E1/E2.
   - `build_plan()` computes scan channels, stream rate, block length `L`, and adjusted coherent frequencies.
   - Real hardware mode programs AD9833 frequency, MCP4728 bias, and Vpp shift-register codes.
4. If real hardware is used, build or load the Vpp LUT.
5. Use Calibration mode for live viewing without recording.
6. Use Session mode to record.
   - `AcquireWorker` reads blocks from `T7Device` or `MockT7`.
   - `ProcessWorker` demodulates photodiode signals against excitation references.
   - `MultiPlotAdapter` updates raw and demodulated plots.
   - `H5Writer` saves data.

## Hardware Summary

The supported real-hardware path uses external SinGen-style excitation hardware:

- AD9833: excitation frequency over LabJack SPI.
- MCP4728: excitation bias voltage over LabJack I2C.
- Shift registers: Vpp setting over LabJack DIO lines.
- LabJack T7 stream: scans excitation monitor AINs, photodiode AINs, and digital inputs.

Default excitation bindings:

| Channel | AD9833 CS | MCP4728 | Vpp SER |
| --- | --- | --- | --- |
| IE | DIO5 | A | DIO8 |
| E1 | DIO6 | B | DIO9 |
| E2 | DIO7 | C | DIO10 |

Shared defaults:

- AD9833 CLK: `DIO3`
- AD9833 MOSI: `DIO4`
- AD9833 MISO: `DIO18`
- Vpp SRCLK: `DIO11`
- Vpp RCLK: `DIO12`
- MCP4728 SDA/SCL: `FIO2/FIO1`

## HDF5 Output

Session files are saved as:

```text
SUBJECT_YYYYMMDD_fiber_photometry.h5
```

The subject comes from the GUI subject dropdown. The date uses 8 digits: year/month/day.

Main HDF5 groups:

- `/meta`
  - `plan_json`
  - `channels_json`
  - `channel_key_json`
  - `excitation_names`
  - `excitation_freqs_hz`
- `/raw`
  - `time`
  - `analog`
  - `digital`
  - `names_analog`
  - `names_digital`
- `/demod`
  - `time`
  - `amplitude`
  - `names_pd`
  - `names_exc`
- `/events`
  - `drop_log`

`/demod/amplitude` shape is:

```text
samples x photodiodes x excitations
```

## Mock Mode

If `Use Real T7` is unchecked, or LabJack LJM is unavailable, the app uses `MockT7`.

Mock mode can:

- Generate synthetic photometry-like signals.
- Load a legacy CSV playback file from the Config tab.
- Exercise plotting and HDF5 logging without hardware.

