# Processing Pipeline

## Responsibilities

The processing project converts one recorder HDF5 file into QC arrays, normalized fluorescence, digital-event voltage files, and a small set of compatibility artifacts expected by an existing analysis workflow.

The processing stage deliberately leaves the original recording untouched. The HDF5 file is the source record; generated NPY and HDF5 outputs are reproducible derivatives. This distinction matters when parameters change or a questionable result needs to be traced back through trimming, reference removal, and baseline estimation.

## Project layout

| File | Responsibility |
| --- | --- |
| `run_process.py` | Session orchestration, pulse QC, plots, and fixed processing parameters. |
| `modules/DataIO.py` | Input validation, HDF5 reading, resampling, and NPY export. |
| `modules/DffTraces.py` | Reference regression, baseline estimation, dF/F, and robust z-scoring. |
| `modules/SaveStructureMatch.py` | Downstream-compatible file creation and behavioral MAT rename. |
| `modules/LabelExcInh.py` | Optional imaging ROI/anatomical labeling utilities; not called by `run_process.py`. |

## Input contract

`resolve_h5_path()` requires a session directory containing exactly one `*_fiber_photometry.h5`. The file must provide:

- `/meta/channel_key_json`,
- `/raw/time`, `/raw/digital`, and `/raw/names_digital`,
- `/demod/time`, `/demod/amplitude`, `/demod/names_pd`, and `/demod/names_exc`.

The demodulated amplitude shape must be `(time, photodiode, excitation)`. Empty or mismatched datasets raise an error before processing.

## Fixed processing parameters

`run_postprocess()` currently defines its parameters inside the function:

| Parameter | Value | Meaning |
| --- | --- | --- |
| Raw start trim | 0.25 s | Removes the beginning power transient. |
| Raw stop trim | 0.25 s | Removes the ending transient. |
| Downsample target | 500 Hz | Approximate demod/QC output rate. |
| Digital names | `TrialStart`, `Opto` | Channels checked and exported. |
| Baseline window | 60 s | Rolling percentile-filter duration. |
| Baseline percentile | 10 | Low fluorescence baseline percentile. |
| Huber epsilon | 1.5 | Robust reference-regression sensitivity. |

## Pulse quality control

`pulse_anomalies()` thresholds a digital trace at 0.5, detects rising and falling edges, pairs them in time order, and reports:

- `fall_without_rise`,
- `rise_without_fall`,
- `bad_width` when pulse width differs from 68 ms by more than 10 ms.

Anomalies are printed to the terminal. They do not stop processing and are not saved to a report file.

## Trimming and resampling

Raw indices are chosen using `numpy.searchsorted()` on `/raw/time`. Demodulated rows are retained when their timestamps lie between the first and last retained raw timestamps.

The demodulated sampling rate is estimated from the median timestamp difference. Downsampling is simple stride selection:

```text
step = round(original_rate / target_rate)
indices = 0, step, 2*step, ...
```

This is decimation without an additional offline anti-alias filter. The online lock-in output has already passed low-pass stages, but users should still verify that the resulting bandwidth is appropriate.

Each digital output is sampled from the nearest raw timestamp at or after the demodulated timestamp, with indices clipped to the available range.

## dF/F calculation

The numerical stages answer different questions. Reference regression asks, "how much of this signal follows the artifact reference?" Baseline estimation asks, "what is the local resting fluorescence?" dF/F expresses change relative to that baseline, and robust z-scoring expresses how unusual each value is relative to the session's typical variation.

For every photodiode and every non-`IE` excitation, `compute_dff_traces()` performs:

### 1. Reference regression

The signal and IE reference are median-centered. A no-intercept `HuberRegressor` fits the centered reference to the centered signal. The fitted reference contribution is removed:

```text
residual = signal - slope * (reference - median(reference))
```

The residual median is shifted back to the original signal median by default.

### 2. Rolling baseline

A rolling percentile filter estimates the 10th percentile in a nominal 60-second odd-sized window. NaNs are replaced with the signal median before filtering. Reflective edge handling is used.

### 3. Fractional fluorescence

```text
dF/F = (residual - baseline) / max(baseline, 1e-6)
```

### 4. Robust normalization

```text
z = (dF/F - median(dF/F)) / (1.4826 * median(abs(dF/F - median(dF/F))))
```

The denominator is limited to at least `1e-9`.

Each result dictionary retains `raw`, `ref`, `resid`, `slope`, `base`, `dff`, and `zn`, so callers can inspect every stage.

## Saved trace selection

Although dF/F is calculated for every non-reference excitation and photodiode, the current orchestration code writes only:

```python
dff_results['F1', 'E1']['zn']
```

to `dff.h5`. Sessions without both `F1` and `E1` will fail at this lookup. Other calculated traces remain available in the returned `dff_results` object and in `qc_results/A_<PD>_<EXC>.npy`, but are not persisted as processed dF/F files.

## Compatibility exports

### `dff.h5`

Contains one dataset, `/dff`, shaped `(1, samples)`. Despite the dataset name, the stored values are the robust z-score `zn` for F1/E1, not the unnormalized fractional dF/F array.

### `raw_voltages.h5`

The pipeline creates a timebase five times denser than the processed fluorescence timebase. It repeats each sampled digital value five times, marks every original fluorescence timestamp in `vol_img`, converts time to milliseconds, and saves binary channels under `/raw`.

Mapping:

| Output | Source |
| --- | --- |
| `vol_start` | `TrialStart` |
| `vol_led` | `Opto` |
| `vol_img` | One-sample marker at each fluorescence time |
| Other voltage channels | Zeros for compatibility |

### `ops.npy` and `masks.h5`

These are placeholders, not image-derived results. `ops.npy` contains only `save_path0` and `nchannels=1`. `masks.h5` contains small dummy arrays and a single label of `-1`.

### Behavioral MAT file

If exactly one `.mat` file other than `bpod_session_data.mat` exists, it is renamed. If the target already exists, no action is taken. Otherwise, ambiguous or missing MAT files produce a terminal message.

## Optional ROI-labeling module

`LabelExcInh.py` belongs to a broader imaging pipeline and is not part of normal fiber-photometry processing. It can load Suite2p masks and traces, run Cellpose on an anatomical channel, correct anatomical bleedthrough, calculate functional/anatomical ROI overlap, assign labels, and write a richer `masks.h5`.

This module expects files such as `qc_results/masks.npy`, Suite2p `F.npy`, `F_chan2.npy`, and image fields in `ops`. Those inputs are not produced by the present fiber-photometry entry point.

## Optional plots

Call `run_postprocess(session, plot=True)` to show:

- demodulated amplitudes by photodiode and excitation,
- downsampled digital channels,
- raw/reference traces,
- reference-removed residual,
- rolling baseline,
- dF/F and robust z-score.
