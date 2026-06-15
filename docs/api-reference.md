# API Reference

## Recording: `run_fp.py`

`main()`
: Reuses or creates the Qt application, constructs `MainWindow(use_hw=True)`, displays it, starts the Qt event loop, and exits with the event-loop status. Real hardware is still conditional on a successful LJM import.

## Recording: `core.py`

### Data containers

`ChannelSpec(name, role, enabled=True, target_freq=None, vpp=0.4, vbias=3.0, von=None, ain=None, dac=None, dio=None, ad9833=None, mcp4728=None, vpp_shift=None)`
: Stores one user-facing channel definition. Attributes are copied directly from constructor arguments.

`AD9833Cfg(cs_dio, clk_dio='DIO3', miso_dio='DIO18', mosi_dio='DIO4', spi_mode=2, speed_throttle=0, mclk_hz=25000000.0)`
: Stores AD9833 wiring and SPI settings.

`MCP4728Cfg(chan, sda=2, scl=1, addr7=96, speed_throttle=65516)`
: Stores MCP4728 channel and LabJack I2C settings.

`ShiftVppCfg(ser_dio, srclk_dio='DIO11', rclk_dio='DIO12', nbits=6, msb_first=False)`
: Stores one Vpp shift-register data line and shared clock settings.

`ExcitationPlan(**kw)` / `DevicePlan(**kw)`
: Generic attribute containers for calculated excitation and device plans.

### Functions

`choose_L_and_k(Fs, targets, max_L=4096)`
: Tests block lengths `[512, 768, 1000, 1024, 1600, 2000, 2048, 3072, 4096]`, rounds targets to integer bins, and returns `(L, k, adjusted_frequencies)` for the lowest frequency-error plus block-size score.

`build_plan(specs, Fs_requested, max_L=4096)`
: Validates enabled excitations, zeros disabled excitations, selects scan channels, limits sample rate, calculates coherent frequencies, creates excitation waveforms, and returns a `DevicePlan`.

## Recording: `devices.py`

### Protocol helpers

`_mcp4728_build_simple(chan_letter, volts)`
: Clamps voltage, converts it to a 12-bit code, and returns a three-byte MCP4728 write command.

`_ad9833_words_for_freq(f_hz, mclk)`
: Returns the low and high 14-bit AD9833 frequency-register words.

`_spi_tx_words(handle, cs, clk, miso, mosi, words, mode, speed_throttle)`
: Configures LabJack SPI registers and transmits 16-bit words in big-endian byte order.

`_i2c_tx_bytes_rate_limited(...)`
: Serializes writes by I2C address, enforces a minimum interval, configures LabJack I2C registers, and transmits a payload.

`_dio_write(h, pin, v)`
: Writes one boolean DIO value.

`_shift_vpp_parallel(h, ser_pins, srclk, rclk, codes, nbits, msb_first)`
: Bit-bangs multiple serial data lines in parallel and latches the resulting Vpp codes.

### `MockT7`

`__init__()`
: Initializes deterministic synthetic random state, playback state, sample counters, and optional metadata holders.

`load_csv(path)`
: Reads legacy timestamped CSV playback data and optional JSON metadata comments.

`get_loaded_metadata()`
: Returns loaded plan, channel, and channel-key metadata or `None`.

`apply_plan(plan)`
: Stores the plan, establishes measurement order, and reconciles playback row count.

`start()` / `stop()`
: Reset playback state and control the mock running flag.

`_synth_chunk(L, Fs)`
: Generates noisy excitation monitors, mixed-frequency photodiode signals, and random digital pulses.

`_playback_chunk(L)`
: Returns the next playback block and wraps at the file end.

`read_chunk()`
: Waits until the next real-time block slot and returns `(starting_sample, data, names)`.

### `T7Device`

`__init__()`
: Initializes the LabJack handle, plan, stream state, sample counter, and measurement names without opening hardware yet.

`_open()` / `_close()` / `dispose()`
: Open, configure, stop, and close the LabJack handle.

`apply_plan(plan)`
: Programs external excitation devices and resolves stream scan addresses.

`apply_plan_settings(lut_path='vpp_lut.json', force_rebuild_lut=False, retrim_bias=True)`
: Ensures LUT availability, applies target Vpp/Vbias, and prints a final monitor snapshot.

`start()` / `stop()` / `read_chunk()`
: Manage the LabJack stream and return channel-major float32 blocks.

`__del__()`
: Best-effort call to `dispose()` during object destruction.

`_exc_monitors()`
: Returns `(excitation_name, monitor_ain)` pairs.

`measure_monitors_pp(duration_s=0.1, rate=None, settling_us=10, qtrim=0.005)`
: Temporarily streams monitor AINs and returns median Vbias plus quantile-based Vpp.

`_parallel_shift_codes(codes_by_name)`
: Merges requested and current codes, validates shared shift settings, writes them, and caches current codes.

`calibrate_vpp_luts(codes=range(64), settle_s=0.015, measure_s=0.08, save_json=None)`
: Sweeps codes, measures Vpp, optionally saves JSON, and returns channel LUTs.

`load_vpp_luts(path)` / `ensure_vpp_luts(...)`
: Validate and load a LUT or rebuild it when needed.

`set_vbias_target(name, target_vbias=1.5, tol=0.01, max_iter=3, ...)`
: Iteratively adjusts the MCP4728 command using measured bias error.

`set_vpp_target(name, target_vpp, clamp=True, retrim_bias=True, ...)`
: Selects the nearest measured LUT code, writes it, and optionally retrims Vbias.

`sample_monitors_raw(window_s=0.003, settling_us=10)`
: Captures one short raw monitor waveform for plotting and frequency estimation.

## Recording: `workers.py`

`VppLutWorker.__init__(dev, codes=None, plot_win_s=0.003, settle_s=0.015, measure_s=0.08)`
: Stores the device and sweep timing. The default code list is all integers from 0 through 63.

`VppLutWorker.run()` / `stop()`
: Background 64-code LUT sweep with waveform, status, result, and error signals.

`AcquireWorker.__init__(device)`
: Stores the selected real or mock device and initializes the running flag.

`AcquireWorker.start_loop()` / `stop()`
: Starts a device, repeatedly reads chunks, emits plan metadata with each block, and stops safely.

`ProcessWorker.__init__(plan, ui_hz=40.0, max_window_s=10.0, tau1_ms=10.0, tau2_ms=10.0, decim_hz=500.0, baseline_sec=5.0, dc_tau_s=2.0, common_mode=True)`
: Stores the plan and processing parameters and initializes all streaming filter/buffer state.

`ProcessWorker.stop()`
: Clears the processing running flag.

`ProcessWorker.process_chunk(chunk)`
: Converts one acquisition block into a raw writer item and optional demodulated block.

`ProcessWorker._ingest_raw(...)`
: Builds timestamps and retains a trailing raw-data window.

`ProcessWorker._init_lockin_state(names)`
: Resolves photodiodes, filter constants, reference tables, and state arrays.

`ProcessWorker._lockin_stream(...)`
: Performs sample-wise quadrature mixing, filtering, amplitude calculation, and decimation.

`ProcessWorker._maybe_emit_ui(Fs)`
: Concatenates retained blocks and emits a throttled plot payload.

## Recording: `gui.py`

### `H5Writer`

`__init__(q, path, specs=None, plan=None, override_plan=None, override_channels=None, override_channel_key=None, chunk_len=8192)`
: Configures the daemon writer, metadata sources, dataset state, chunk length, counters, and gap tracking.

`run()`
: Consumes queued blocks until stopped, prepares the file on the first block, appends data, and closes cleanly.

`_open_and_prepare(meas_names)`
: Creates metadata, raw, demod, and event groups and resolves channel row indices.

`_append_block(item)`
: Detects sample gaps and extends raw/demod datasets.

`stop()`
: Sets the stop flag and queues a sentinel when possible.

### `MultiPlotAdapter`

`__init__(plan, specs, plots, window_sec, demod_ylim)`
: Builds logical-to-physical channel maps, curve registries, colors, and initial axis limits.

`reset_items()` / `set_window()` / `_fix_xranges()`
: Reset curve registries and control the plot time window.

`update(payload)`
: Dispatches raw and demodulated payloads.

`_subset_and_rescale_time(t, window)`
: Selects the trailing window and maps its start to zero.

`_downsample(y)`
: Reduces long arrays to a 2,000-point min/max envelope.

`_update_raw(...)` / `_update_demod(...)`
: Create or update curves for excitation, digital, photodiode, and amplitude panels.

### `ChannelTable`

`__init__()`
: Creates the nine-column channel table and configures headers and row appearance.

`populate(specs)`
: Creates editable rows from channel specs.

`_ro(...)`, `_num(...)`, `_str(...)`
: Create read-only, numeric-text, and text cells.

`pull(specs)`
: Writes current table values back into the existing specs.

### `MainWindow`

`__init__(use_hw=False)`
: Creates default channel specs and application state, selects `T7Device` only when requested and available, loads subjects, builds the UI, and synchronizes hardware controls.

`_build_ui()` / `_build_plot_grid(...)`
: Construct the three tabs, controls, LUT panels, and live plots.

`_load_subjects()` / `_save_subjects()`
: Read and write `subjects.json`.

`_on_set_base_dir()`, `_on_add_subject()`, `_on_del_subject()`, `_update_storage_mode()`, `_safe_name()`
: Manage session path and subject controls.

`_maybe_auto_verify()`, `_color_span()`, `_format_verify_html()`, `_estimate_freq()`
: Measure excitation performance and format pass/fail metrics.

`_lut_in_progress()` / `_set_plan_ready()`
: Track whether sessions may start.

`_auto_range_session()`
: Calculate and lock current plot Y ranges.

`_on_hw_mode_changed()` / `_on_load_session_file()`
: Enable mock playback controls and load a CSV.

`_on_verify_lut()`
: Capture excitation monitor data and compare it with configured targets.

`on_build_plan()`
: Read the table, attach default hardware bindings, build/apply a plan, and load or start a LUT.

`_cleanup_after_lut_abort()`, `_on_abort_lut()`, `_on_build_lut()`, `_on_lut_thread_finished()`, `_on_lut_wave()`, `_on_lut_status()`, `_on_lut_finished()`, `_on_lut_error()`
: Manage the complete asynchronous LUT-build lifecycle.

`_clear_plot_widgets(plots)`
: Clear curves and restore legends/X ranges before a new run.

`_start_pipeline(mode)`
: Validate readiness, select output path, start the writer and Qt workers, and update button states.

`on_stop()`
: Stop workers, threads, and writer and restore idle controls.

`_on_ui_update(payload)` / `_on_raw_batch(payload)` / `_on_error(msg)` / `_update_window(v)` / `closeEvent(e)`
: Route plot data, enqueue recording blocks, show errors, update windows, and shut down.

## Processing: `DataIO.py`

`read_json(dset)`
: Decode a scalar byte/string HDF5 dataset and parse JSON.

`load_h5(h5_path)`
: Open read-only HDF5 and return the file, channel-key mapping, and digital physical names.

`read_demod(h5)`
: Validate required demod datasets, load arrays/names, and verify shape and non-empty time.

`resolve_h5_path(sess_path)`
: Require exactly one session recording matching the expected suffix.

`read_raw_time(h5)`
: Return `/raw/time`.

`get_digital(h5, channel_key, digital_names, name, start, stop)`
: Resolve a friendly digital name and return a float32 raw slice or `None`.

`resample_data(...)`
: Stride-downsample demod data and align digital traces to the resulting timestamps.

`qc_output_dir(sess_path)`
: Return `SESSION/qc_results`.

`load_export(sess_path, out_dir=None, mmap_mode='r')`
: Load all QC NPY files into a dictionary.

`export_npy(out_dir, result, digitals)`
: Save time, digital traces, and every PD/excitation amplitude separately.

## Processing: `DffTraces.py`

`roll_pct_baseline(y, t, window_s=60, pct=10)`
: Replace NaNs and calculate an odd-window rolling percentile baseline.

`mad_z(x, eps=1e-9)`
: Return median/MAD robust z-scores.

`regress_out_ref(y, x, epsilon=1.5, preserve_dc=True)`
: Robustly regress a reference trace from a signal and return residual plus slope.

`postprocess(t, y, ref=None, ...)`
: Return all reference-removal, baseline, dF/F, and normalization stages.

`compute_dff_traces(result, reference_exc='IE', signal_excs=None, ...)`
: Process every requested non-reference excitation for every photodiode.

## Processing: `SaveStructureMatch.py`

`save_dff(sess_path, dff)`
: Reshape one trace to `(1, samples)` and overwrite `dff.h5`.

`create_dummy_ops(sess_path)`
: Create minimal `suite2p/plane0/ops.npy`.

`create_dummy_masks(sess_path)`
: Create placeholder `masks.h5` arrays.

`move_bpod_mat(sess_path)`
: Rename one unambiguous MAT file to the expected behavioral filename.

`process_vol(sess_path, result, digitals, upsample_factor=5)`
: Build the dense millisecond voltage timebase, map digital inputs, create compatibility channels, and save `raw_voltages.h5`.

Nested `binary_trace(name)`
: Fetches and binarizes a named digital trace, validates its length, and repeats samples onto the dense voltage timebase; missing channels become zeros.

## Processing: `run_process.py`

`pulse_anomalies(time, signal, expected_s=0.068, tolerance_s=0.010)`
: Return unpaired-edge and width anomaly dictionaries.

`plot_raw_overview(result)`, `plot_digitals(t, digitals)`, `plot_steps(t, r, title='')`, `plot_dff_results(t, dff_results)`
: Display raw, digital, and processing-stage QC figures.

`run_postprocess(sess_path, plot=False)`
: Execute the complete session processing workflow and return `(written_npy_paths, dff_results)`.

## Optional processing: `LabelExcInh.py`

`normz(data)`
: Mean/std z-score with a small denominator offset.

`run_cellpose(ops, mean_anat, diameter, flow_threshold=0.5)`
: Save the anatomical mean image, run Cellpose `cyto3`, save segmentation artifacts, and return masks.

`get_mask(ops)`
: Crop functional masks/images to Suite2p ranges and return optional anatomical mean image.

`get_ch_traces(ops)`
: Load Suite2p `F.npy` and `F_chan2.npy`.

`anat_bleedthrough_correction(...)`
: Optimize a linear anatomical-channel correction by minimizing absolute trace correlation.

Nested `objective(...)`, `optimize()`, and `correct_anat(...)`
: Score corrected trace correlation, solve for linear correction parameters with L-BFGS-B, and apply those parameters to the anatomical mean image.

`get_label(masks_func, masks_anat, thres1=0.2, thres2=0.9)`
: Calculate maximum relative overlaps; assign `-1` below 0.2, `1` above 0.9, and `0` otherwise.

`save_masks(...)`
: Write functional images, labels, and optional anatomical data to `masks.h5`.

`run(ops, diameter)`
: Orchestrate single-channel default labeling or two-channel Cellpose labeling.
