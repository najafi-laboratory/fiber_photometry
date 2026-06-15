# Concepts and Background

This page explains the ideas behind the modules. It is intended for readers who need to operate or modify the code but do not yet know why fiber-photometry software is organized this way.

## What the system measures

A photodiode reports one voltage containing the sum of all optical signals reaching it. In this system, multiple LEDs or excitation paths are modulated at different frequencies. The tissue response to each excitation is therefore tagged with that excitation's frequency. Frequency-division multiplexing lets one photodiode carry several separable fluorescence signals at the same time.

<div class="pipeline-strip">
  <div class="pipeline-step">Modulate light</div>
  <div class="pipeline-step">Record mixed voltage</div>
  <div class="pipeline-step">Demodulate each frequency</div>
  <div class="pipeline-step">Remove reference</div>
  <div class="pipeline-step">Calculate dF/F</div>
</div>

## Try a tiny lock-in amplifier

The gray trace is a noisy recorded signal. The green trace is a running average after multiplying that signal by a sine reference. Set the two frequencies equal and the response grows; move them apart and positive and negative products cancel. This is the central idea used by `ProcessWorker`.

<div class="signal-lab">
  <div class="signal-controls">
    <label>Signal frequency <span id="signal-freq-value">8 Hz</span>
      <input id="signal-freq" type="range" min="2" max="20" step="1" value="8" />
    </label>
    <label>Reference frequency <span id="reference-freq-value">8 Hz</span>
      <input id="reference-freq" type="range" min="2" max="20" step="1" value="8" />
    </label>
    <label>Noise <span id="signal-noise-value">0.35</span>
      <input id="signal-noise" type="range" min="0" max="1" step="0.05" value="0.35" />
    </label>
  </div>
  <canvas id="signal-canvas" aria-label="Interactive lock-in demodulation demonstration"></canvas>
  <div id="signal-readout" class="signal-readout"></div>
</div>

## Why coherent blocks matter

The recorder does not use arbitrary excitation frequencies exactly as entered. `choose_L_and_k()` picks a block length and adjusts each frequency to `k * Fs / L`, where `k` is an integer. Every acquisition block then contains a whole number of cycles.

When a block ends midway through a cycle, a Fourier or lock-in view treats the sharp boundary as energy spread across nearby frequencies. This is spectral leakage. Coherent blocks reduce leakage and make neighboring excitation channels easier to separate.

## Why I and Q are both used

The phase delay between generated light, tissue, detector, and electronics is not guaranteed to be zero. Correlating only with a sine reference would underestimate a signal shifted toward cosine phase. The online processor therefore calculates two components:

- **I:** correlation with cosine,
- **Q:** correlation with sine.

Amplitude is calculated as `2 * sqrt(I^2 + Q^2)`. This magnitude is largely insensitive to phase rotation.

## Why low-pass filtering reveals amplitude

Multiplying two equal-frequency sine waves produces a constant term plus a component at twice the original frequency. A low-pass filter removes the fast doubled-frequency term and retains the slowly varying amplitude. The recorder uses exponential filters because they can update one sample at a time without storing a long convolution window.

Two cascaded stages produce stronger rejection of fast components than one stage. The time constants trade responsiveness for smoothness: shorter values react quickly but pass more noise; longer values are smoother but blur fast biological changes.

## Reference excitation and motion correction

The `IE` excitation is treated as a reference channel by the offline pipeline. The intended assumption is that the reference captures non-calcium variation such as motion, fiber bending, coupling changes, or bleaching, while the signal excitation captures those effects plus calcium-dependent fluorescence.

`regress_out_ref()` fits the reference contribution with robust Huber regression. Huber loss behaves like squared error for ordinary samples and reduces the influence of large outliers. This is useful when real transients should not dominate the motion-reference fit.

The correction is statistical, not magical. If IE contains biology of interest, or if artifacts affect IE and E1 differently, regression can remove useful signal or leave residual artifact. Always inspect the raw signal, reference, fitted slope, and residual.

## Baseline and dF/F

Fluorescence changes are easier to compare after normalization to a local baseline:

```text
dF/F = (F - F0) / F0
```

The pipeline estimates `F0` with a rolling low percentile. A low percentile follows slow bleaching and drift while avoiding most upward transients. The 60-second window defines how slowly the baseline may move; the 10th percentile assumes the trace spends enough time near baseline within each window.

## Why use a robust z-score

The median and median absolute deviation are less sensitive to large events than the mean and standard deviation. The saved normalized trace uses:

```text
z = (x - median(x)) / (1.4826 * median(abs(x - median(x))))
```

The factor `1.4826` makes MAD comparable to standard deviation for normally distributed data. This normalization is useful for plotting and comparing event responses, but its units are robust standard deviations, not fractional fluorescence.

## Why the code is split into modules

### `core.py`: describe before touching hardware

This module converts human configuration into a deterministic plan. Keeping planning separate makes it possible to validate channel count, sample rate, and coherent frequencies before opening a stream.

### `devices.py`: isolate hardware side effects

SPI, I2C, DIO timing, LabJack streaming, and mock playback live behind device objects. The rest of the application can consume the same `(sample_index, data, names)` interface regardless of whether data is real or synthetic.

### `workers.py`: protect the GUI event loop

Hardware reads and per-sample demodulation can block for too long to run in the main Qt thread. Workers keep controls and plots responsive while signals safely carry data between threads.

### `gui.py`: coordinate people, threads, plots, and files

The GUI owns user intent: which mode to run, where to save, when the plan is ready, and how errors are displayed. `H5Writer` is placed here because its lifecycle is tied directly to a recording session.

### `DataIO.py`: enforce the recorder/processor contract

Centralizing file validation prevents every analysis function from making slightly different assumptions about paths, channel names, and array shapes.

### `DffTraces.py`: keep numerical transformations inspectable

Reference removal, baseline fitting, dF/F, and normalization are separate functions so each intermediate can be plotted, tested, or replaced independently.

### `SaveStructureMatch.py`: adapt to downstream conventions

The scientific processing result is not always shaped like the files expected by legacy analysis code. This module creates that compatibility layer without mixing it into the signal-processing functions.

## What to inspect before trusting a result

1. Raw excitation monitors should be stable and close to configured frequency, Vpp, and Vbias.
2. Raw photodiode voltages should not clip or sit at a rail.
3. Demodulated amplitudes should be finite and should not jump at chunk boundaries.
4. IE and E1 should have a plausible relationship before regression.
5. The fitted baseline should follow drift without following individual events.
6. Digital pulses should align with the behavioral record.
7. The final z-scored trace should always be interpreted alongside raw and intermediate traces.

