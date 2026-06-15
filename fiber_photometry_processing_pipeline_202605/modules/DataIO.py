#!/usr/bin/env python3

import os
import json
import h5py
import numpy as np
from pathlib import Path

# read json dataset saved in h5.
def read_json(dset):
    value = dset[()]
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    return json.loads(value)

# load h5 metadata needed by the processing pipeline.
def load_h5(h5_path):
    h5 = h5py.File(h5_path, "r")
    channel_key = read_json(h5["meta/channel_key_json"])
    digital_names = h5["raw/names_digital"].asstr()[...].tolist()
    return h5, channel_key, digital_names

# read demodulated traces saved during acquisition.
def read_demod(h5):
    if "demod" not in h5:
        raise ValueError("H5 file does not contain a /demod group")
    demod = h5["demod"]
    required = {"time", "amplitude", "names_pd", "names_exc"}
    missing = sorted(required.difference(demod.keys()))
    if missing:
        raise ValueError(f"Missing /demod datasets: {', '.join(missing)}")
    result = {
        "t": demod["time"][:],
        "A": demod["amplitude"][:].astype(np.float32, copy=False),
        "pd_names": demod["names_pd"].asstr()[...].tolist(),
        "exc_names": demod["names_exc"].asstr()[...].tolist(),
    }
    if result["A"].shape != (result["t"].size, len(result["pd_names"]), len(result["exc_names"])):
        raise ValueError("/demod/amplitude shape does not match time and channel names")
    if result["t"].size == 0:
        raise ValueError("/demod datasets are empty")
    return result

# resolve the fiber photometry recording inside a session folder.
def resolve_h5_path(sess_path):
    sess_path = Path(sess_path)
    if not sess_path.is_dir():
        raise NotADirectoryError(f"Session folder not found: {sess_path}")
    h5_paths = sorted(sess_path.glob("*_fiber_photometry.h5"))
    if len(h5_paths) != 1:
        found = ", ".join(path.name for path in h5_paths) or "none"
        raise ValueError(f"Expected exactly one *_fiber_photometry.h5 file in {sess_path}; found {found}")
    return str(h5_paths[0])

# read raw acquisition time from h5.
def read_raw_time(h5):
    return h5["raw/time"][:]

# get digital signal from raw h5.
def get_digital(h5, channel_key, digital_names, name, start, stop):
    # resolve friendly name to measured channel.
    meas = channel_key.get(name, {}).get("meas_name", name)
    if meas not in digital_names:
        return None
    row = digital_names.index(meas)
    return h5["raw/digital"][row, start:stop].astype(np.float32)

# downsample stored traces and digital signals.
def resample_data(result, h5, channel_key, digital_h5_names, digital_names, start, stop, target_hz):
    # match FP_PostProcess_5.py: trim first, then keep every Nth sample.
    time = result["t"]
    fs = 1 / np.median(np.diff(time))
    if target_hz <= 0 or target_hz >= fs:
        idx = np.arange(time.size)
    else:
        step = max(1, int(np.rint(fs / target_hz)))
        idx = np.arange(0, time.size, step)
    # apply the same sample index to time and amplitude.
    result = dict(result)
    result["t"] = time[idx]
    result["A"] = result["A"][idx].astype(np.float32)
    # sample digital traces at nearest raw timestamp.
    raw_time = h5["raw/time"][start:stop]
    digitals = {}
    for name in digital_names:
        trace = get_digital(h5, channel_key, digital_h5_names, name, start, stop)
        if trace is not None:
            raw_idx = np.clip(np.searchsorted(raw_time, result["t"], side="left"), 0, raw_time.size - 1)
            digitals[name] = trace[raw_idx]
    return result, digitals

# get output folder for qc npy files.
def qc_output_dir(sess_path):
    return os.path.join(sess_path, "qc_results")

# load npy files exported from the processing pipeline.
def load_export(sess_path, out_dir=None, mmap_mode="r"):
    folder = Path(out_dir) if out_dir is not None else Path(qc_output_dir(sess_path))
    data = {}
    for path in sorted(folder.glob("*.npy")):
        key = path.stem
        data["t" if key == "time" else key] = np.load(path, mmap_mode=mmap_mode)
    return data

# save demodulated and digital traces as npy files.
def export_npy(out_dir, result, digitals):
    # create output folder.
    os.makedirs(out_dir, exist_ok=True)
    written = []
    # save time and digital signals.
    for name, array in {"time": result["t"], **digitals}.items():
        path = os.path.join(out_dir, f"{name}.npy")
        np.save(path, array)
        written.append(path)
    # save demodulated signals by photodiode and excitation.
    for pi, pd_name in enumerate(result["pd_names"]):
        for ei, exc_name in enumerate(result["exc_names"]):
            path = os.path.join(out_dir, f"A_{pd_name}_{exc_name}.npy")
            np.save(path, result["A"][:, pi, ei])
            written.append(path)
    return written
