# Operations and Troubleshooting

## Pre-recording checklist

- Confirm LabJack LJM is installed and the T7 is visible over USB.
- Confirm AIN, DIO, SPI, I2C, and shift-register wiring matches the Config table and default bindings.
- Select the intended real/mock mode before building the plan.
- Confirm enabled excitation frequencies are positive.
- Confirm no two semantic channels unintentionally point to the same physical input.
- Build the plan and review effective `Fs`, `L`, and adjusted frequencies.
- Build or load the LUT and verify frequency, Vpp, and Vbias.
- Run Calibration mode and inspect all raw and demodulated panels.
- Set the base directory and subject before starting the session.

## Post-recording checklist

- Stop the session through the GUI.
- Confirm the HDF5 file exists at the displayed path.
- Confirm `/raw/time` and `/demod/time` are non-empty and monotonic.
- Confirm amplitude shape matches photodiode and excitation name counts.
- Check `/meta` `dropped_chunks_detected`.
- Inspect `/events/drop_log` for gaps.
- Confirm `TrialStart` and `Opto` are present in `channel_key_json` and `/raw/names_digital` when required.
- Preserve the original recording HDF5 as the source of truth.

## Processing checklist

- Place exactly one `*_fiber_photometry.h5` in each session directory.
- Place the behavioral `.mat` file in the same directory when downstream analysis requires it.
- Review `list_sess_path` before running `run_process.py`.
- Read terminal pulse-anomaly messages.
- Confirm `qc_results/time.npy` and all expected `A_<PD>_<EXC>.npy` files exist.
- Plot or inspect F1/IE and F1/E1 before trusting normalized output.
- Confirm `dff.h5`, `raw_voltages.h5`, `masks.h5`, and `suite2p/plane0/ops.npy` were created.
- Remember that `dff.h5` currently contains F1/E1 robust z-score only.

## Common recording problems

### The app uses mock mode unexpectedly

`run_fp.py` requests real hardware, but `MainWindow` falls back to `MockT7` when importing `labjack.ljm` fails. Install both the Python package and the system LJM driver, then restart the application.

### Build Plan reports zero channels

At least one enabled AIN or DIO must be selected. Check the Enable boxes and physical input fields.

### Frequency differs from the requested value

This is expected within a small error. Frequencies are adjusted to coherent bins `k * Fs / L`. Review the plan status and verify that the error is acceptable.

### LUT build never enables session start

Session start remains disabled while a LUT thread is running or when `_plan_ready` is false. Wait for completion, inspect the LUT error message, or abort and rebuild the plan. An aborted or failed build removes or invalidates `vpp_lut.json` and clears the plan.

### Monitor measurements fail during streaming

LUT verification and monitor measurements start their own temporary stream. Stop Calibration or Session mode before invoking them.

### HDF5 has missing blocks

Inspect both queue-drop terminal messages and `/events/drop_log`. Potential causes include slow storage, an undersized writer queue, long filesystem stalls, or acquisition/processing errors. Queue-dropped blocks may appear as a later sample-index gap when the next block is written.

### Reusing the same subject on the same day

The filename contains subject and date but no run number or time. Opening a new session at the same path uses HDF5 mode `w` and overwrites the previous file. Move/rename the earlier file or use a different destination before another same-day session.

## Common processing problems

### Expected exactly one HDF5 file

The session has zero or multiple matching recordings. Keep one intended `*_fiber_photometry.h5` in the directory or process recordings in separate session folders.

### Missing `/demod` datasets

The input was not created by a compatible recorder or recording ended before the writer created/appended demod data. Inspect the source HDF5 and repeat acquisition if necessary.

### No demodulated samples remain after trimming

The recording is too short, timestamps do not overlap, or the first/last raw timestamps are inconsistent. The fixed trim removes 0.25 s from each end.

### `KeyError: ('F1', 'E1')`

The current save step assumes both names exist and E1 is processed as a non-reference signal. Rename/configure channels consistently or change the selected key in `run_process.py`.

### Baseline errors on very short recordings

The rolling percentile code requires enough samples to form a minimum window. Very short traces may produce invalid window sizing. Use a longer session or reduce the configured baseline window in code.

### Pulse anomaly messages

Check wiring, voltage thresholds, synchronization, and task pulse generation. The expected pulse is 68 +/- 10 ms. The messages are warnings; decide whether the session remains valid before analysis.

### Behavioral MAT was not renamed

The function only renames when exactly one non-target `.mat` file is present. Remove ambiguity or rename the correct file manually to `bpod_session_data.mat`.

## Current implementation cautions

- Same-subject, same-day recordings can overwrite each other.
- The Session Log name control does not affect the active HDF5 filename.
- Online amplitude baseline state is calculated but neither applied nor saved.
- Offline downsampling uses stride selection without a new anti-alias filter.
- Pulse anomalies are printed but not saved.
- Only F1/E1 robust z-score is persisted in `dff.h5`.
- Dummy Suite2p/mask outputs are compatibility placeholders, not biological segmentation results.
- `LabelExcInh.py` is optional legacy/broader-pipeline code and is not invoked by the fiber-photometry entry point.

## Minimal validation commands

Syntax-check both projects:

```powershell
python -m compileall -q fiber_photometry_recording_202606 fiber_photometry_processing_pipeline_202605
```

Build the documentation:

```powershell
mkdocs build --strict
```

