#!/usr/bin/env python3

import numpy as np
import matplotlib.pyplot as plt
from modules.DataIO import export_npy, get_digital, load_h5, qc_output_dir, read_demod, read_raw_time, resample_data, resolve_h5_path
from modules.DffTraces import compute_dff_traces
from modules.SaveStructureMatch import save_dff, create_dummy_ops, create_dummy_masks, move_bpod_mat, process_vol

# find abnormal pulse widths in digital signal.
def pulse_anomalies(time, signal, expected_s=0.068, tolerance_s=0.010):
    # find digital edges.
    high = signal >= 0.5
    change = np.diff(high.astype(np.int8))
    rises = time[np.flatnonzero(change == 1) + 1]
    falls = time[np.flatnonzero(change == -1) + 1]
    # pair rises and falls.
    out = []
    fall_i = 0
    for rise in rises:
        while fall_i < falls.size and falls[fall_i] <= rise:
            out.append({"type": "fall_without_rise", "time": float(falls[fall_i])})
            fall_i += 1
        if fall_i == falls.size:
            out.append({"type": "rise_without_fall", "time": float(rise)})
            continue
        width = float(falls[fall_i] - rise)
        if np.abs(width - expected_s) > tolerance_s:
            out.append({"type": "bad_width", "rise": float(rise), "fall": float(falls[fall_i]), "width": width})
        fall_i += 1
    out.extend({"type": "fall_without_rise", "time": float(fall)} for fall in falls[fall_i:])
    return out

# plot demodulated amplitudes for each photodiode.
def plot_raw_overview(result):
    t = result["t"]
    for pi, pd_name in enumerate(result["pd_names"]):
        fig, ax = plt.subplots(1, 1, figsize=(24, 6), layout="tight")
        for ei, exc_name in enumerate(result["exc_names"]):
            ax.plot(t, result["A"][:, pi, ei], label=f"A_{pd_name}_{exc_name}")
        ax.grid(False)
        ax.set_xlabel("Time (s)")
        ax.legend()
        ax.set_title(f"{pd_name} demodulated amplitudes")

# plot downsampled digital channels.
def plot_digitals(t, digitals):
    if not digitals:
        return
    fig, ax = plt.subplots(1, 1, figsize=(12, 3), layout="tight")
    for name, trace in digitals.items():
        ax.plot(t, trace, label=name)
    ax.set_title("Digital signals")
    ax.set_xlabel("Time (s)")
    ax.legend()
    ax.grid(False)

# plot each step of dF/F processing.
def plot_steps(t, r, title=""):
    fig, ax = plt.subplots(4, 1, sharex=True, figsize=(12, 9), layout="tight")
    # raw and reference.
    ax[0].plot(t, r["raw"], label="raw")
    if r["ref"] is not None:
        ax[0].plot(t, r["ref"], label="ref")
        ax[0].set_title(f"{title} | Huber slope={r['slope']:.4g}")
    else:
        ax[0].set_title(title)
    ax[0].legend()
    # reference-removed residual.
    ax[1].plot(t, r["resid"], label="residual")
    ax[1].legend()
    # baseline fit.
    ax[2].plot(t, r["resid"], label="residual")
    ax[2].plot(t, r["base"], label="baseline")
    ax[2].legend()
    # final normalized traces.
    ax[3].plot(t, r["dff"], label="dF/F")
    ax[3].plot(t, r["zn"], label="z (MAD)")
    ax[3].legend()
    ax[3].set_xlabel("Time (s)")
    for axis in ax:
        axis.grid(False)

# plot all computed dF/F traces.
def plot_dff_results(t, dff_results):
    for (pd_name, exc_name), trace in dff_results.items():
        plot_steps(t, trace, title=f"{pd_name}: {exc_name} with IE reference removed")

# run full postprocess pipeline.
def run_postprocess(sess_path, plot=False):
    raw_trim_start_s=0.25
    raw_trim_stop_s=0.25
    downsample_hz=500.0
    digital_names=None
    dff_window_s=60
    dff_pct=10
    dff_huber_eps=1.5
    # load raw h5 data.
    digital_names = ["TrialStart", "Opto"]
    h5_path = resolve_h5_path(sess_path)
    h5, channel_key, digital_h5_names = load_h5(h5_path)
    raw_time = read_raw_time(h5)
    # trim raw power-spike edges.
    start = 0 if raw_trim_start_s <= 0 else np.searchsorted(raw_time, raw_trim_start_s, side="left")
    stop_time = raw_time[-1] - np.maximum(0, raw_trim_stop_s)
    stop = raw_time.size if raw_trim_stop_s <= 0 else np.searchsorted(raw_time, stop_time, side="right")
    print(f"Opened {h5_path}")
    print(f"Trimmed raw samples to [{start}, {stop})")
    # load demodulated signals saved during acquisition.
    result = read_demod(h5)
    # check digital pulse quality.
    for name in digital_names:
        trace = get_digital(h5, channel_key, digital_h5_names, name, start, stop)
        if trace is None:
            continue
        found = pulse_anomalies(raw_time[start:stop], trace)
        if found:
            print(f"{name} anomalies: {found}")
    # trim stored demodulated signals to the selected raw interval.
    keep = (result["t"] >= raw_time[start]) & (result["t"] <= raw_time[stop - 1])
    if not np.any(keep):
        h5.close()
        raise ValueError("No demodulated samples remain after trimming")
    result["t"] = result["t"][keep]
    result["A"] = result["A"][keep]
    # downsample and export traces.
    result, digitals = resample_data(result, h5, channel_key, digital_h5_names, digital_names, start, stop, downsample_hz)
    written = export_npy(qc_output_dir(sess_path), result, digitals)
    for path in written:
        print(f"Exported: {path}")
    # compute dF/F traces.
    print('Computing dff')
    dff_results = compute_dff_traces(
        result,
        reference_exc="IE",
        window_s=dff_window_s,
        pct=dff_pct,
        huber_eps=dff_huber_eps,
    )
    h5.close()
    # save results.
    save_dff(sess_path, dff_results['F1', 'E1']['zn'])
    create_dummy_ops(sess_path)
    create_dummy_masks(sess_path)
    process_vol(sess_path, result, digitals)
    move_bpod_mat(sess_path)
    # show figures.
    if plot:
        plot_raw_overview(result)
        plot_digitals(result["t"], digitals)
        plot_dff_results(result["t"], dff_results)
        plt.show()
    return written, dff_results

if __name__ == "__main__":
    
    list_sess_path = [
        'C:/Users/yhuang887/Projects/joystick_double_motor_timing_202601/2p/results/YH33/20260614', 'C:/Users/yhuang887/Projects/joystick_double_motor_timing_202601/2p/results/YH33/20260612'
        ]
    for sess_path in list_sess_path:
        run_postprocess(sess_path)
        
