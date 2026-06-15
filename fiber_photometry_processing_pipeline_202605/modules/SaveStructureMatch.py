#!/usr/bin/env python3

import os
import h5py
import numpy as np

# save dff traces results.
def save_dff(sess_path, dff):
    print('Saving dff')
    dff = np.asarray(dff).reshape(1, -1)
    f = h5py.File(os.path.join(sess_path, 'dff.h5'), 'w')
    f['dff'] = dff
    f.close()

# create suite2p ops file.
def create_dummy_ops(sess_path):
    print('Saving dummy ops.npy')
    save_dir = sess_path
    ops = {'save_path0': save_dir,
           'nchannels': 1}
    if not os.path.exists(os.path.join(save_dir, 'suite2p', 'plane0')):
        os.makedirs(os.path.join(save_dir, 'suite2p', 'plane0'))
    np.save(os.path.join(save_dir, 'suite2p', 'plane0', 'ops.npy'), ops)
    return ops

# create dummy mask labels.
def create_dummy_masks(sess_path):
    print('Saving dummy masks')
    save_dir = sess_path
    masks_func = np.array([5, 1, 2, 3, 2])
    mean_func = np.array([5, 1, 2, 3, 2])
    max_func = np.array([5, 1, 2, 3, 2])
    labels = np.array([-1])
    f = h5py.File(os.path.join(save_dir, 'masks.h5'), 'w')
    f['labels'] = labels
    f['masks_func'] = masks_func
    f['mean_func'] = mean_func
    f['max_func'] = max_func
    f.close()
    return masks_func, labels

# move bpod session data.
def move_bpod_mat(sess_path):
    print('Moving bpod session data')
    target = os.path.join(sess_path, 'bpod_session_data.mat')
    bpod_mat = [
        f for f in os.listdir(sess_path)
        if f.lower().endswith('.mat') and f != 'bpod_session_data.mat'
    ]
    if len(bpod_mat) == 1:
        os.rename(os.path.join(sess_path, bpod_mat[0]),
                  target)
    elif os.path.exists(target):
        return
    else:
        print('Valid bpod session data mat file not found')
        
# save voltage recordings.
def process_vol(sess_path, result, digitals, upsample_factor=5):
    print('Processing voltage signals')
    # make a dense timebase from dff timestamps.
    t = np.asarray(result['t'], dtype=float)
    if t.ndim != 1 or t.size == 0:
        raise ValueError('result[\'t\'] must be a non-empty 1D array')
    if upsample_factor < 1:
        raise ValueError('upsample_factor must be at least 1')
    factor = int(upsample_factor)
    if t.size == 1:
        vol_time = t.copy()
    else:
        pieces = [
            np.linspace(t[i], t[i + 1], factor, endpoint=False)
            for i in range(t.size - 1)
        ]
        vol_time = np.concatenate([*pieces, t[-1:]])
    # expand sampled digital traces onto the dense timebase.
    def binary_trace(name):
        trace = digitals.get(name)
        if trace is None:
            return np.zeros(vol_time.shape, dtype=np.uint8)
        trace = (np.asarray(trace) >= 0.5).astype(np.uint8)
        if trace.shape != t.shape:
            raise ValueError(f'digitals["{name}"] must match result["t"] length')
        if trace.size == 1:
            return trace.copy()
        return np.concatenate([np.repeat(trace[:-1], factor), trace[-1:]]).astype(np.uint8)
    # mark every dff timestamp as an imaging trigger.
    vol_img = np.zeros(vol_time.shape, dtype=np.uint8)
    vol_img[np.arange(t.size) * factor] = 1
    # convert time from seconds to milliseconds.
    vol_time = vol_time * 1000
    # keep unused voltage channels as binary zeros.
    vol = {
        'vol_time': vol_time,
        'vol_start': binary_trace('TrialStart'),
        'vol_stim_vis': np.zeros(vol_time.shape, dtype=np.uint8),
        'vol_hifi': np.zeros(vol_time.shape, dtype=np.uint8),
        'vol_img': vol_img,
        'vol_stim_aud': np.zeros(vol_time.shape, dtype=np.uint8),
        'vol_flir': np.zeros(vol_time.shape, dtype=np.uint8),
        'vol_pmt': np.zeros(vol_time.shape, dtype=np.uint8),
        'vol_led': binary_trace('Opto'),
        'vol_2p_stim': np.zeros(vol_time.shape, dtype=np.uint8)}
    # save in the suite2p-compatible h5 structure.
    save_dir = sess_path
    os.makedirs(save_dir, exist_ok=True)
    with h5py.File(os.path.join(save_dir, 'raw_voltages.h5'), 'w') as f:
        grp = f.create_group('raw')
        for key, value in vol.items():
            grp[key] = value
    return vol
