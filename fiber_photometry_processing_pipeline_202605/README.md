# Fiber Photometry Processing Pipeline

Post-process fiber photometry recordings, check digital pulses, downsample and export signals, compute reference-corrected dF/F, and create files compatible with the downstream analysis workflow.

## Environment Setup

Install [Miniconda](https://docs.conda.io/projects/miniconda/en/latest/) or Anaconda, then run:

```powershell
conda create -n fiber-photometry-process python=3.11 -y
conda activate fiber-photometry-process
pip install numpy matplotlib h5py scipy scikit-learn tifffile tqdm cellpose
```

## Input

Each session folder must contain exactly one recording named:

```text
*_fiber_photometry.h5
```

The HDF5 file must include raw timestamps/digital channels, channel metadata, and saved demodulated signals under `/demod`.

## Main Workflow

1. Open `run_process.py` and set `list_sess_path` to the session folders to process.
2. Run the pipeline from this repository:

```powershell
python run_process.py
```

For each session, the pipeline trims recording edges, checks `TrialStart` and `Opto` pulses, downsamples signals to 500 Hz, exports QC arrays, removes the `IE` reference, computes dF/F and robust z-scores, and writes downstream-compatible output files.

Main outputs are saved inside each session folder:

- `qc_results/*.npy`: time, digital, and demodulated traces
- `dff.h5`: processed z-scored dF/F trace
- `raw_voltages.h5`: converted digital signals
- `masks.h5` and `suite2p/plane0/ops.npy`: compatibility files
- `bpod_session_data.mat`: renamed behavioral session file when one `.mat` file is present

Set `plot=True` in `run_postprocess(...)` to display processing and QC plots.
