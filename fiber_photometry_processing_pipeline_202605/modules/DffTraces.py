#!/usr/bin/env python3

import os
import h5py
import numpy as np
from scipy.ndimage import percentile_filter
from sklearn.linear_model import HuberRegressor

# estimate a rolling low percentile baseline.
def roll_pct_baseline(y, t, window_s=60, pct=10):
    y = np.asarray(y, float)
    if np.isnan(y).any():
        y = np.where(np.isnan(y), np.nanmedian(y), y)
    # convert time window to an odd sample window.
    dt = float(np.median(np.diff(t)))
    win = max(3, int(round(window_s / dt)))
    win = min(win, len(y) - (len(y) % 2 == 0))
    win = max(3, win | 1)
    return percentile_filter(y, percentile=pct, size=win, mode="reflect")

# robust z-score using median absolute deviation.
def mad_z(x, eps=1e-9):
    x = np.asarray(x, float)
    c = np.nanmedian(x)
    s = 1.4826 * np.nanmedian(np.abs(x - c))
    return (x - c) / (s if s > eps else eps)

# regress reference channel from signal channel.
def regress_out_ref(y, x, epsilon=1.5, preserve_dc=True):
    y = np.asarray(y, float)
    if x is None:
        return y, 0.0
    x = np.asarray(x, float)
    ym, xm = np.nanmedian(y), np.nanmedian(x)
    yy, xx = y - ym, (x - xm).reshape(-1, 1)
    m = HuberRegressor(fit_intercept=False, epsilon=epsilon, max_iter=200).fit(xx, yy)
    slope = float(m.coef_[0])
    resid = y - slope * (x - xm)
    if preserve_dc:
        resid += (ym - np.nanmedian(resid))
    return resid, slope

# compute residual, baseline, dF/F, and robust z-score.
def postprocess(t, y, ref=None, window_s=60, pct=10, min_base=1e-6, huber_eps=1.5):
    resid, slope = regress_out_ref(y, ref, epsilon=huber_eps)
    base = roll_pct_baseline(resid, t, window_s=window_s, pct=pct)
    dff = (resid - base) / np.maximum(base, min_base)
    zn = mad_z(dff)
    return dict(raw=y, ref=ref, resid=resid, slope=slope, base=base, dff=dff, zn=zn)

# compute dF/F traces for each photodiode and signal excitation.
def compute_dff_traces(result, reference_exc="IE", signal_excs=None,
                       window_s=60, pct=10, min_base=1e-6, huber_eps=1.5):
    traces = {}
    t = result["t"]
    pd_names = result["pd_names"]
    exc_names = result["exc_names"]
    ref_idx = exc_names.index(reference_exc) if reference_exc in exc_names else None
    # use the same reference excitation for each photodiode.
    for pi, pd_name in enumerate(pd_names):
        ref = result["A"][:, pi, ref_idx] if ref_idx is not None else None
        for ei, exc_name in enumerate(exc_names):
            if exc_name == reference_exc:
                continue
            if signal_excs is not None and exc_name not in signal_excs:
                continue
            traces[(pd_name, exc_name)] = postprocess(
                t,
                result["A"][:, pi, ei],
                ref=ref,
                window_s=window_s,
                pct=pct,
                min_base=min_base,
                huber_eps=huber_eps)
    return traces
