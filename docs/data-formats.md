# Data Formats

## Recorder HDF5

Filename:

```text
SUBJECT_YYYYMMDD_fiber_photometry.h5
```

All extendable numeric datasets use LZF compression. Raw numeric blocks are float32 except timestamps, while metadata strings use UTF-8 HDF5 string types.

### `/meta`

| Item | Type | Description |
| --- | --- | --- |
| attribute `META_VERSION` | string | Currently `2`. |
| attribute `START_TIME` | ISO UTC string | File-open time with `Z` suffix. |
| attribute `dropped_chunks_detected` | integer | Number of sample-index discontinuities observed by the writer. Written at close. |
| `plan_json` | scalar JSON string | Mode, sample rate, block length, excitation names, scan lists, and unused TDM fields. |
| `channels_json` | scalar JSON string | Serialized channel configuration. |
| `channel_key_json` | scalar JSON string | Friendly-name to physical measurement-channel mapping. |
| `excitation_names` | string vector | Enabled excitation names in plan order. |
| `excitation_freqs_hz` | float64 vector | Adjusted coherent excitation frequencies. |

Example channel-key entry:

```json
{
  "TrialStart": {
    "meas_name": "MIO0",
    "type": "DIO",
    "role": "DIGITAL_IN"
  }
}
```

### `/raw`

| Dataset | Shape | Type | Description |
| --- | --- | --- | --- |
| `time` | `(samples,)` | float64 | Seconds from stream sample zero. |
| `analog` | `(analog_channels, samples)` | float32 | Analog values in sorted physical-name order. |
| `digital` | `(digital_channels, samples)` | float32 | Digital stream values in sorted physical-name order. |
| `names_analog` | `(analog_channels,)` | strings | Physical names corresponding to `analog` rows. |
| `names_digital` | `(digital_channels,)` | strings | Physical names corresponding to `digital` rows. |

### `/demod`

| Dataset | Shape | Type | Description |
| --- | --- | --- | --- |
| `time` | `(demod_samples,)` | float64 | Time of each decimated lock-in sample. |
| `amplitude` | `(demod_samples, photodiodes, excitations)` | float32 | Absolute online lock-in amplitudes. |
| `names_pd` | `(photodiodes,)` | strings | Friendly photodiode names when available. |
| `names_exc` | `(excitations,)` | strings | Excitation names in plan order. |

`time` and `amplitude` are only created when the plan has at least one photodiode and one excitation. Name datasets are created even when empty.

### `/events`

| Dataset | Shape | Description |
| --- | --- | --- |
| `drop_log` | `(events, 3)` int64 | Rows are `(observed_si0, expected_si0, gap_samples)`. A positive gap indicates missing samples; a negative gap indicates overlap or non-monotonic input. |

## Vpp LUT JSON

`vpp_lut.json` contains a mapping from excitation name to 64 measured Vpp values:

```json
{
  "luts": {
    "IE": [0.0, 0.05, 0.10],
    "E1": [0.0, 0.05, 0.10],
    "E2": [0.0, 0.05, 0.10]
  }
}
```

Actual arrays must contain exactly 64 values. Files written by the device-level calibration path may also include a Unix `timestamp`; files written by the GUI worker contain only `luts`.

## Subject list JSON

`subjects.json` is a JSON list of subject names. `FakeSubject` is inserted in memory if absent and cannot be deleted through the GUI.

## Processing QC NPY files

Directory: `SESSION/qc_results/`

| Pattern | Shape | Description |
| --- | --- | --- |
| `time.npy` | `(samples,)` | Downsampled demodulation timestamps in seconds. |
| `TrialStart.npy` | `(samples,)` | Sampled digital trace, when present. |
| `Opto.npy` | `(samples,)` | Sampled digital trace, when present. |
| `A_<PD>_<EXC>.npy` | `(samples,)` | Downsampled online demodulated amplitude. |

`load_export()` loads all NPY files into a dictionary and renames key `time` to `t`.

## `dff.h5`

| Dataset | Shape | Description |
| --- | --- | --- |
| `/dff` | `(1, samples)` | Current pipeline: F1/E1 robust MAD z-score. |

## `raw_voltages.h5`

All datasets are under `/raw` and share the dense voltage timebase length.

| Dataset | Type | Description |
| --- | --- | --- |
| `vol_time` | float64 | Milliseconds. |
| `vol_start` | uint8 | Binarized `TrialStart`. |
| `vol_led` | uint8 | Binarized `Opto`. |
| `vol_img` | uint8 | One at each original processed-fluorescence sample. |
| `vol_stim_vis` | uint8 | Zeros. |
| `vol_hifi` | uint8 | Zeros. |
| `vol_stim_aud` | uint8 | Zeros. |
| `vol_flir` | uint8 | Zeros. |
| `vol_pmt` | uint8 | Zeros. |
| `vol_2p_stim` | uint8 | Zeros. |

## `masks.h5`

The default compatibility writer stores:

| Dataset | Current contents |
| --- | --- |
| `labels` | `[-1]` |
| `masks_func` | Dummy one-dimensional array `[5, 1, 2, 3, 2]` |
| `mean_func` | Same dummy array |
| `max_func` | Same dummy array |

The optional ROI-labeling module can instead add `mean_anat` and `masks_anat` with real image-derived content.

## `suite2p/plane0/ops.npy`

Saved Python dictionary:

```python
{
    "save_path0": "<session path>",
    "nchannels": 1
}
```

Load with `numpy.load(path, allow_pickle=True).item()`.

