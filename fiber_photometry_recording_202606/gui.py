import json
import os
import queue
import threading
import re
import time
import h5py
import numpy as np
from PyQt6 import QtCore, QtWidgets
import pyqtgraph as pg
from devices import HAVE_LJM, MockT7, T7Device
from core import AD9833Cfg, ChannelSpec, MCP4728Cfg, ShiftVppCfg, build_plan
from workers import AcquireWorker, ProcessWorker, VppLutWorker

class H5Writer(threading.Thread):

    def __init__(self, q, path, specs=None, plan=None, override_plan=None, override_channels=None, override_channel_key=None, chunk_len=8192):
        super().__init__(daemon=True)
        self.q = q
        self.path = path
        self._h5 = None
        self._prepared = False
        self._stop_flag = False
        self._specs = list(specs) if specs else []
        self._plan = plan
        self._override_plan = override_plan
        self._override_channels = override_channels
        self._override_channel_key = override_channel_key
        self._chunk_len = max(1024, int(chunk_len))
        self._analog_cols = []
        self._digital_cols = []
        self._analog_idx = []
        self._digital_idx = []
        self._ds_t = None
        self._ds_a = None
        self._ds_d = None
        self._ds_demod_t = None
        self._ds_demod_A = None
        self._ds_drop = None
        self._N_written = 0
        self._N_demod_written = 0
        self._last_si_end = None
        self._drops_detected = 0

    def run(self):
        try:
            while not self._stop_flag:
                try:
                    item = self.q.get(timeout=0.5)
                except queue.Empty:
                    continue
                if item is None:
                    break
                if not self._prepared:
                    self._open_and_prepare(item['names'])
                self._append_block(item)
        finally:
            if self._h5:
                try:
                    meta = self._h5.require_group('meta')
                    meta.attrs['dropped_chunks_detected'] = int(self._drops_detected)
                    self._h5.flush()
                except Exception:
                    pass
                self._h5.close()
                self._h5 = None

    def _open_and_prepare(self, meas_names):
        import datetime, json, pathlib
        pathlib.Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        analog = [n for n in meas_names if n.startswith('AIN')]
        digital = [n for n in meas_names if n.startswith(('FIO', 'EIO', 'CIO', 'MIO', 'DIO'))]
        analog.sort()
        digital.sort()
        self._analog_cols = analog
        self._digital_cols = digital
        plan_dict = self._override_plan if self._override_plan is not None else (
            {} if self._plan is None else {'mode': self._plan.mode, 'Fs': self._plan.Fs, 'L': self._plan.L, 'excitation': [ex.name for ex in self._plan.excitations], 'scan_ain': list(self._plan.scan_ain), 'scan_dio': list(self._plan.scan_dio), 'tdm_offsets': None, 'tdm_on_off': None}
        )
        channels_list = self._override_channels if self._override_channels is not None else [
            {'name': c.name, 'role': c.role, 'enabled': c.enabled, 'target_freq': c.target_freq, 'vpp': c.vpp, 'vbias': c.vbias, 'von': c.von, 'ain': c.ain, 'dac': c.dac, 'dio': c.dio, 'pulse_on_ms': 1 if c.role != 'PHOTODIODE' else 1.0, 'pulse_off_ms': 1 if c.role != 'PHOTODIODE' else 1.0}
            for c in self._specs
        ]
        channel_key = self._override_channel_key if self._override_channel_key is not None else {
            c.name: {'meas_name': c.ain if c.role in ('EXCITATION', 'PHOTODIODE') else c.dio, 'type': 'AIN' if c.role in ('EXCITATION', 'PHOTODIODE') else 'DIO', 'role': c.role}
            for c in self._specs
            if c.enabled and ((c.role in ('EXCITATION', 'PHOTODIODE') and c.ain) or (c.role == 'DIGITAL_IN' and c.dio))
        }
        exc_names = []
        exc_freqs = []
        if self._plan is not None and self._plan.excitations:
            exc_names = [ex.name for ex in self._plan.excitations]
            exc_freqs = [float(ex.f_adj) for ex in self._plan.excitations]
        self._h5 = h5py.File(self.path, 'w')
        gmeta = self._h5.create_group('meta')
        gmeta.attrs['META_VERSION'] = '2'
        gmeta.attrs['START_TIME'] = datetime.datetime.utcnow().isoformat() + 'Z'
        str_dt = h5py.string_dtype(encoding='utf-8')
        gmeta.create_dataset('plan_json', data=json.dumps(plan_dict), dtype=str_dt)
        gmeta.create_dataset('channels_json', data=json.dumps(channels_list), dtype=str_dt)
        gmeta.create_dataset('channel_key_json', data=json.dumps(channel_key), dtype=str_dt)
        gmeta.create_dataset('excitation_names', data=np.array(exc_names, dtype=object), dtype=str_dt)
        gmeta.create_dataset('excitation_freqs_hz', data=np.array(exc_freqs, dtype=np.float64))
        graw = self._h5.create_group('raw')
        graw.create_dataset('names_analog', data=np.array(self._analog_cols, dtype=object), dtype=str_dt)
        graw.create_dataset('names_digital', data=np.array(self._digital_cols, dtype=object), dtype=str_dt)
        self._ds_t = graw.create_dataset('time', shape=(0,), maxshape=(None,), chunks=(self._chunk_len,), dtype=np.float64, compression='lzf')
        n_a = len(self._analog_cols)
        n_d = len(self._digital_cols)
        self._ds_a = graw.create_dataset('analog', shape=(n_a, 0), maxshape=(n_a, None), chunks=(max(1, n_a), self._chunk_len), dtype=np.float32, compression='lzf')
        self._ds_d = graw.create_dataset('digital', shape=(n_d, 0), maxshape=(n_d, None), chunks=(max(1, n_d), self._chunk_len), dtype=np.float32, compression='lzf')
        gdemod = self._h5.create_group('demod')
        pd_names = [c.name for c in self._specs if c.enabled and c.role == 'PHOTODIODE' and c.ain in self._analog_cols]
        if not pd_names and self._plan is not None:
            mons = {ex.ain_monitor for ex in self._plan.excitations if ex.ain_monitor}
            pd_names = [n for n in self._plan.scan_ain if n not in mons]
        n_pd = len(pd_names)
        n_exc = len(exc_names)
        gdemod.create_dataset('names_pd', data=np.array(pd_names, dtype=object), dtype=str_dt)
        gdemod.create_dataset('names_exc', data=np.array(exc_names, dtype=object), dtype=str_dt)
        if n_pd and n_exc:
            self._ds_demod_t = gdemod.create_dataset('time', shape=(0,), maxshape=(None,), chunks=(self._chunk_len,), dtype=np.float64, compression='lzf')
            self._ds_demod_A = gdemod.create_dataset('amplitude', shape=(0, n_pd, n_exc), maxshape=(None, n_pd, n_exc), chunks=(max(1, min(self._chunk_len, 1024)), n_pd, n_exc), dtype=np.float32, compression='lzf')
        gevt = self._h5.create_group('events')
        self._ds_drop = gevt.create_dataset('drop_log', shape=(0, 3), maxshape=(None, 3), chunks=(1024, 3), dtype=np.int64, compression='lzf')
        name_to_idx = {n: i for i, n in enumerate(meas_names)}
        self._analog_idx = [name_to_idx[a] for a in self._analog_cols if a in name_to_idx]
        self._digital_idx = [name_to_idx[d] for d in self._digital_cols if d in name_to_idx]
        self._prepared = True
        print(f'[H5Writer] Opened {self.path}')
        print(f'[H5Writer] analog={self._analog_cols}  digital={self._digital_cols}')

    def _append_block(self, item):
        t = item['t']
        raw = item['raw']
        si0 = item.get('si0')
        L = item.get('L')
        if si0 is not None and L is not None:
            if self._last_si_end is None:
                self._last_si_end = si0 + L
            else:
                expected = self._last_si_end
                if si0 != expected:
                    gap = int(si0 - expected)
                    self._drops_detected += 1
                    m = self._ds_drop.shape[0]
                    self._ds_drop.resize(m + 1, axis=0)
                    self._ds_drop[m, :] = (int(si0), int(expected), gap)
                    print(f'[H5Writer] GAP: si0={si0} expected={expected} gap={gap}')
                    self._last_si_end = si0 + L
                else:
                    self._last_si_end = si0 + L
        N = t.size
        if N == 0:
            return
        start = self._N_written
        end = start + N
        self._ds_t.resize(end, axis=0)
        self._ds_t[start:end] = t.astype(np.float64, copy=False)
        if self._analog_idx:
            a_block = raw[self._analog_idx, :].astype(np.float32, copy=False)
            self._ds_a.resize((self._ds_a.shape[0], end))
            self._ds_a[:, start:end] = a_block
        if self._digital_idx:
            d_block = raw[self._digital_idx, :].astype(np.float32, copy=False)
            self._ds_d.resize((self._ds_d.shape[0], end))
            self._ds_d[:, start:end] = d_block
        self._N_written = end
        if self._ds_demod_A is not None and 'demod_t' in item and item['demod_t'].size:
            td = item['demod_t'].astype(np.float64, copy=False)
            A = item['demod_A'].astype(np.float32, copy=False)
            start = self._N_demod_written
            end = start + td.size
            self._ds_demod_t.resize(end, axis=0)
            self._ds_demod_A.resize(end, axis=0)
            self._ds_demod_t[start:end] = td
            self._ds_demod_A[start:end, :, :] = A
            self._N_demod_written = end

    def stop(self):
        self._stop_flag = True
        try:
            self.q.put_nowait(None)
        except queue.Full:
            pass

class MultiPlotAdapter:
    COLOR_EXC = {'IE': '#1f77b4', 'E1': '#2ca02c', 'E2': '#d62728'}
    COLOR_PD = {'F1': '#2ca02c', 'F2': '#d62728'}

    def __init__(self, plan, specs, plots, window_sec, demod_ylim):
        self.plan = plan
        self.specs = specs
        self.plots = plots
        self.window_sec = window_sec
        self.demod_ylim = demod_ylim
        self.pd_name_to_ain = {c.name: c.ain for c in specs if c.role == 'PHOTODIODE' and c.ain}
        self.exc_name_to_ain = {ex.name: ex.ain_monitor for ex in plan.excitations if ex.ain_monitor}
        self.items_exc = {}
        self.items_dig = {}
        self.items_pd_combined = {}
        self.items_demod_f1 = {}
        self.items_demod_f2 = {}
        for key in ('ie', 'e1', 'e2', 'pd_combined'):
            if key in self.plots:
                self.plots[key].setYRange(-0.1, 5.1)
        if 'dins' in self.plots:
            self.plots['dins'].setYRange(-0.1, 1.1)
        if 'f1_demod' in self.plots:
            self.plots['f1_demod'].setYRange(0, self.demod_ylim)
        if 'f2_demod' in self.plots:
            self.plots['f2_demod'].setYRange(0, self.demod_ylim)

    def reset_items(self):
        self.items_exc.clear()
        self.items_dig.clear()
        self.items_pd_combined.clear()
        self.items_demod_f1.clear()
        self.items_demod_f2.clear()

    def set_window(self, window_sec):
        self.window_sec = window_sec
        self._fix_xranges()

    def _fix_xranges(self):
        for p in self.plots.values():
            p.setXRange(0, self.window_sec, padding=0)

    def update(self, payload):
        t_raw = payload['t_raw']
        raw = payload['raw']
        t_amp = payload['t_amp']
        A = payload['A']
        if raw.size:
            self._update_raw(t_raw, raw)
        if A.size:
            self._update_demod(t_amp, A)

    def _subset_and_rescale_time(self, t, window):
        t_end = t[-1]
        t_start = t_end - window
        mask = t >= t_start
        t_sel = t[mask]
        if not t_sel.size:
            return (np.empty(0), mask)
        t_plot = t_sel - t_start
        return (t_plot, mask)

    def _downsample(self, y):
        if y.size <= 2000:
            return (None, y)
        edges = np.linspace(0, y.size, 1001, dtype=int)
        x_idx = np.empty(2000, dtype=int)
        env = np.empty(2000, dtype=y.dtype)
        for i, (s, e) in enumerate(zip(edges[:-1], edges[1:])):
            seg = y[s:max(e, s + 1)]
            x_idx[2 * i:2 * i + 2] = (s, max(e - 1, s))
            env[2 * i:2 * i + 2] = (seg.min(), seg.max())
        return (x_idx, env)

    def _update_raw(self, t_raw, raw):
        meas_names = list(self.plan.scan_ain) + list(self.plan.scan_dio)
        t_plot, mask = self._subset_and_rescale_time(t_raw, self.window_sec)
        if not t_plot.size:
            return
        raw_window = raw[:, mask]
        ain_rows = {nm: i for i, nm in enumerate(self.plan.scan_ain)}
        dig_rows = {nm: i + len(self.plan.scan_ain) for i, nm in enumerate(self.plan.scan_dio)}

        def map_time(x_idx, y_len, t_start, t_end):
            if x_idx is None:
                return t_plot
            if y_len <= 1:
                return np.full_like(x_idx, t_start, dtype=float)
            return t_start + x_idx / (y_len - 1) * (t_end - t_start)
        for exc_name, ain in self.exc_name_to_ain.items():
            plot_key = exc_name.lower()
            if plot_key not in self.plots or ain not in ain_rows:
                continue
            row = ain_rows[ain]
            y = raw_window[row]
            x_idx, yds = self._downsample(y)
            if plot_key not in self.items_exc:
                pen = pg.mkPen(self.COLOR_EXC.get(exc_name, '#cccccc'), width=1.2)
                self.items_exc[plot_key] = self.plots[plot_key].plot(pen=pen, name=exc_name)
            x_final = map_time(x_idx, len(y), 0.0, self.window_sec)
            self.items_exc[plot_key].setData(x_final, yds)
            self.plots[plot_key].setXRange(0, self.window_sec, padding=0)
        if 'dins' in self.plots and dig_rows:
            for dname, row in dig_rows.items():
                dig = raw_window[row]
                x_idx, yds = self._downsample(dig)
                if dname not in self.items_dig:
                    pen = pg.mkPen('#ff00aa', width=1.0)
                    self.items_dig[dname] = self.plots['dins'].plot(pen=pen, name=dname)
                x_final = map_time(x_idx, len(dig), 0.0, self.window_sec)
                self.items_dig[dname].setData(x_final, yds)
            self.plots['dins'].setXRange(0, self.window_sec, padding=0)
        if 'pd_combined' in self.plots:
            for pd_name, ain in self.pd_name_to_ain.items():
                if ain not in ain_rows:
                    continue
                row = ain_rows[ain]
                y = raw_window[row]
                x_idx, yds = self._downsample(y)
                if pd_name not in self.items_pd_combined:
                    pen = pg.mkPen(self.COLOR_PD.get(pd_name, '#888888'), width=1.2)
                    self.items_pd_combined[pd_name] = self.plots['pd_combined'].plot(pen=pen, name=pd_name)
                x_final = map_time(x_idx, len(y), 0.0, self.window_sec)
                self.items_pd_combined[pd_name].setData(x_final, yds)
            self.plots['pd_combined'].setXRange(0, self.window_sec, padding=0)

    def _update_demod(self, t_amp, A):
        t_plot, mask = self._subset_and_rescale_time(t_amp, self.window_sec)
        if not t_plot.size:
            return
        A_win = A[mask]
        n_pd = A_win.shape[1]
        exc_names = [ex.name for ex in self.plan.excitations]
        t_final = t_plot

        def plot_panel(panel_key, pd_index, store):
            if panel_key not in self.plots:
                return
            for ei, exc_name in enumerate(exc_names):
                key = f'{panel_key}_{exc_name}'
                if key not in store:
                    pen = pg.mkPen(self.COLOR_EXC.get(exc_name, '#cccccc'), width=1.3)
                    store[key] = self.plots[panel_key].plot(pen=pen, name=exc_name)
                store[key].setData(t_final, A_win[:, pd_index, ei])
            self.plots[panel_key].setXRange(0, self.window_sec, padding=0)
        if n_pd > 0:
            plot_panel('f1_demod', 0, self.items_demod_f1)
        if n_pd > 1:
            plot_panel('f2_demod', 1, self.items_demod_f2)

class ChannelTable(QtWidgets.QTableWidget):
    COLS = ['Enable', 'Name', 'Role', 'TargetHz', 'Vpp', 'Vbias', 'AIN', 'DAC', 'DIO']

    def __init__(self):
        super().__init__(0, len(self.COLS))
        self.setHorizontalHeaderLabels(self.COLS)
        self.verticalHeader().setVisible(False)
        self.setAlternatingRowColors(True)

    def populate(self, specs):
        self.setRowCount(len(specs))
        for r, c in enumerate(specs):
            chk = QtWidgets.QTableWidgetItem()
            chk.setFlags(QtCore.Qt.ItemFlag.ItemIsUserCheckable | QtCore.Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(QtCore.Qt.CheckState.Checked if c.enabled else QtCore.Qt.CheckState.Unchecked)
            self.setItem(r, 0, chk)
            self._ro(r, 1, c.name)
            self._ro(r, 2, c.role)
            self._num(r, 3, c.target_freq if c.target_freq else '')
            self._num(r, 4, c.vpp)
            self._num(r, 5, c.vbias)
            self._str(r, 6, c.ain or '')
            self._str(r, 7, c.dac or '')
            self._str(r, 8, c.dio or '')

    def _ro(self, row, col, val):
        it = QtWidgets.QTableWidgetItem(str(val))
        it.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled | QtCore.Qt.ItemFlag.ItemIsSelectable)
        self.setItem(row, col, it)

    def _num(self, row, col, val):
        it = QtWidgets.QTableWidgetItem('' if val == '' else f'{val}')
        self.setItem(row, col, it)

    def _str(self, row, col, val):
        it = QtWidgets.QTableWidgetItem(val)
        self.setItem(row, col, it)

    def pull(self, specs):

        def to_float(txt, default=None):
            try:
                if txt.strip() == '':
                    return default
                return float(txt)
            except:
                return default
        for r, c in enumerate(specs):
            c.enabled = self.item(r, 0).checkState() == QtCore.Qt.CheckState.Checked
            c.target_freq = to_float(self.item(r, 3).text(), c.target_freq)
            c.vpp = to_float(self.item(r, 4).text(), c.vpp)
            c.vbias = to_float(self.item(r, 5).text(), c.vbias)
            c.ain = self.item(r, 6).text() or None
            c.dac = self.item(r, 7).text() or None
            c.dio = self.item(r, 8).text() or None

class MainWindow(QtWidgets.QMainWindow):

    def __init__(self, use_hw=False):
        super().__init__()
        self.setWindowTitle('NFSPhotometry')
        self.resize(1400, 900)
        self.specs = [ChannelSpec('IE', 'EXCITATION', True, target_freq=811.0, vpp=1.8, vbias=4.0, ain='AIN0', dac='DAC0'), ChannelSpec('E1', 'EXCITATION', True, target_freq=1307.0, vpp=1.8, vbias=4.0, ain='AIN1', dac='DAC1'), ChannelSpec('E2', 'EXCITATION', True, target_freq=2111.0, vpp=1.8, vbias=4.0, ain='AIN2', dac='NONE'), ChannelSpec('F1', 'PHOTODIODE', True, ain='AIN3'), ChannelSpec('F2', 'PHOTODIODE', True, ain='AIN4'), ChannelSpec('TrialStart', 'DIGITAL_IN', True, dio='MIO0'), ChannelSpec('Opto', 'DIGITAL_IN', True, dio='MIO1')]
        self.plan = None
        self.device = T7Device() if use_hw and HAVE_LJM else MockT7()
        self.initial_use_hw = use_hw and HAVE_LJM
        self.acquire_thread = None
        self.process_thread = None
        self.acq_worker = None
        self.proc_worker = None
        self.csv_queue = None
        self.csv_writer = None
        self.mplot_session = None
        self.mplot_calib = None
        self._recording_enabled = False
        self._current_mode = 'session'
        self._window_sec = 1.0
        self._demod_ylim = 1.0
        self._loaded_session_file = 'logs/t7_stream_fdm_165451_20251003.csv'
        self._subjects_file = 'subjects.json'
        self._base_dir = 'C:\\behavior\\session_data'
        self._subjects = self._load_subjects()
        if 'FakeSubject' not in self._subjects:
            self._subjects.insert(0, 'FakeSubject')
        self._lut_abort_pending = False
        self._plan_ready = False
        self._tol_freq_pct = 0.5
        self._tol_vpp_pct = 5.0
        self._tol_vbias_mV = 25.0
        self._build_ui()
        self.chk_hw.blockSignals(True)
        self.chk_hw.setChecked(self.initial_use_hw)
        self.chk_hw.blockSignals(False)
        self._on_hw_mode_changed()

    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)
        self.tabs = QtWidgets.QTabWidget()
        layout.addWidget(self.tabs)
        self.tab_config = QtWidgets.QWidget()
        cfg_layout = QtWidgets.QVBoxLayout(self.tab_config)
        top_cfg = QtWidgets.QHBoxLayout()
        self.fs_spin = QtWidgets.QDoubleSpinBox()
        self.fs_spin.setRange(1000, 200000)
        self.fs_spin.setValue(12500.0)
        self.chk_hw = QtWidgets.QCheckBox('Use Real T7')
        self.chk_hw.stateChanged.connect(self._on_hw_mode_changed)
        self.btn_build = QtWidgets.QPushButton('Build Plan')
        self.btn_build.clicked.connect(self.on_build_plan)
        self.btn_build_lut = QtWidgets.QPushButton('Build LUT')
        self.btn_abort_lut = QtWidgets.QPushButton('Abort')
        self.btn_abort_lut.setEnabled(False)
        self.lbl_lut_status = QtWidgets.QLabel('LUT idle.')
        self.btn_build_lut.clicked.connect(self._on_build_lut)
        self.btn_abort_lut.clicked.connect(self._on_abort_lut)
        self.btn_verify_lut = QtWidgets.QPushButton('Verify LUT')
        self.btn_verify_lut.setToolTip('Capture short AIN windows and report vbias/vpp/freq for enabled excitations.')
        self.btn_verify_lut.clicked.connect(self._on_verify_lut)
        top_cfg.addWidget(QtWidgets.QLabel('Fs Req'))
        top_cfg.addWidget(self.fs_spin)
        top_cfg.addWidget(self.chk_hw)
        top_cfg.addWidget(self.btn_build)
        top_cfg.addWidget(self.btn_verify_lut)
        top_cfg.addWidget(self.btn_build_lut)
        top_cfg.addWidget(self.btn_abort_lut)
        top_cfg.addStretch(1)
        cfg_layout.addLayout(top_cfg)
        file_row = QtWidgets.QHBoxLayout()
        self.le_session_file = QtWidgets.QLineEdit(self._loaded_session_file or '')
        self.btn_load_file = QtWidgets.QPushButton('Load Session Data (Mock)')
        self.btn_load_file.clicked.connect(self._on_load_session_file)
        file_row.addWidget(self.le_session_file, 1)
        file_row.addWidget(self.btn_load_file)
        cfg_layout.addLayout(file_row)
        self.table = ChannelTable()
        self.table.populate(self.specs)
        cfg_layout.addWidget(self.table)
        self.lbl_plan_status = QtWidgets.QLabel('No plan.')
        cfg_layout.addWidget(self.lbl_plan_status)
        cfg_layout.addWidget(self.lbl_lut_status)
        self.tabs.addTab(self.tab_config, 'Config')
        self.lut_ain_row = QtWidgets.QHBoxLayout()
        self.lut_ain_widgets = {}
        for tag in ('IE', 'E1', 'E2'):
            w = QtWidgets.QWidget()
            v = QtWidgets.QVBoxLayout(w)
            v.setContentsMargins(4, 2, 4, 2)
            v.setSpacing(2)
            w.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
            w.setFixedHeight(300)
            lbl_metrics = QtWidgets.QLabel('—')
            lbl_metrics.setTextFormat(QtCore.Qt.TextFormat.RichText)
            lbl_metrics.setWordWrap(False)
            lbl_metrics.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter)
            lbl_metrics.setStyleSheet("color:#ffffff; font-family:Consolas,'Courier New',monospace; font-size:10px;")
            lbl_metrics.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
            lbl_metrics.setFixedHeight(40)
            v.addWidget(lbl_metrics)
            title = f'{tag} (AIN?)'
            pw = pg.PlotWidget(title=title)
            pw.setBackground('k')
            pw.showGrid(x=True, y=True, alpha=0.25)
            pw.setYRange(-0.1, 5.1)
            pw.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
            pw.setMinimumHeight(200)
            pw.setMaximumHeight(200)
            pw.addLegend(offset=(8, 8))
            curve = pw.plot(pen=pg.mkPen('#dddddd', width=1.2), name=tag)
            spin = QtWidgets.QDoubleSpinBox()
            spin.setRange(0.001, 0.05)
            spin.setSingleStep(0.001)
            spin.setDecimals(3)
            spin.setValue(0.01)
            spin.setFixedHeight(22)
            v.addWidget(pw)
            hb = QtWidgets.QHBoxLayout()
            hb.setContentsMargins(0, 0, 0, 0)
            hb.setSpacing(4)
            lbl = QtWidgets.QLabel('win (s):')
            lbl.setFixedHeight(20)
            hb.addWidget(lbl)
            hb.addWidget(spin)
            hb.addStretch(1)
            v.addLayout(hb)
            self.lut_ain_row.addWidget(w, 1)
            self.lut_ain_widgets[tag] = {'plot': pw, 'spin': spin, 'label': lbl_metrics, 'curve': curve}
        cfg_layout.addLayout(self.lut_ain_row)
        self.tab_calib = QtWidgets.QWidget()
        calib_layout = QtWidgets.QVBoxLayout(self.tab_calib)
        calib_ctrl = QtWidgets.QHBoxLayout()
        self.spin_window_calib = QtWidgets.QDoubleSpinBox()
        self.spin_window_calib.setRange(0.1, 30.0)
        self.spin_window_calib.setValue(self._window_sec)
        self.spin_window_calib.valueChanged.connect(lambda v: self._update_window(v))
        self.btn_start_calib = QtWidgets.QPushButton('Start Calibration')
        self.btn_stop_calib = QtWidgets.QPushButton('Stop')
        self.btn_stop_calib.setEnabled(False)
        self.btn_start_calib.clicked.connect(lambda: self._start_pipeline(mode='calibration'))
        self.btn_stop_calib.clicked.connect(self.on_stop)
        calib_ctrl.addWidget(QtWidgets.QLabel('Window (s)'))
        calib_ctrl.addWidget(self.spin_window_calib)
        calib_ctrl.addWidget(self.btn_start_calib)
        calib_ctrl.addWidget(self.btn_stop_calib)
        calib_ctrl.addStretch(1)
        calib_layout.addLayout(calib_ctrl)
        self._build_plot_grid(parent_layout=calib_layout, prefix='calib')
        self.lbl_calib_status = QtWidgets.QLabel('Calibration idle.')
        calib_layout.addWidget(self.lbl_calib_status)
        self.tabs.addTab(self.tab_calib, 'Calibration')
        self.tab_session = QtWidgets.QWidget()
        ses_layout = QtWidgets.QVBoxLayout(self.tab_session)
        ses_ctrl = QtWidgets.QHBoxLayout()
        self.spin_window = QtWidgets.QDoubleSpinBox()
        self.spin_window.setRange(0.01, 30.0)
        self.spin_window.setValue(self._window_sec)
        self.spin_window.valueChanged.connect(lambda v: self._update_window(v))
        self.btn_set_base = QtWidgets.QPushButton('Set Base Directory')
        self.btn_set_base.clicked.connect(self._on_set_base_dir)
        self.lbl_base_dir = QtWidgets.QLabel(self._base_dir)
        self.lbl_base_dir.setMinimumWidth(200)
        self.cmb_subjects = QtWidgets.QComboBox()
        self.cmb_subjects.addItems(self._subjects)
        self.btn_add_subj = QtWidgets.QPushButton('+')
        self.btn_add_subj.setFixedWidth(28)
        self.btn_del_subj = QtWidgets.QPushButton('-')
        self.btn_del_subj.setFixedWidth(28)
        self.btn_add_subj.clicked.connect(self._on_add_subject)
        self.btn_del_subj.clicked.connect(self._on_del_subject)
        self.rb_use_logname = QtWidgets.QRadioButton('Use Log Name')
        self.rb_use_subject = QtWidgets.QRadioButton('Use Subject Folder')
        self.rb_use_subject.setChecked(True)
        self.rb_use_logname.toggled.connect(self._update_storage_mode)
        self.rb_use_subject.toggled.connect(self._update_storage_mode)
        self.btn_start_session = QtWidgets.QPushButton('Start Session')
        self.btn_stop_session = QtWidgets.QPushButton('Stop')
        self.btn_stop_session.setEnabled(False)
        self.btn_start_session.setEnabled(False)
        self.btn_autorange = QtWidgets.QPushButton('Auto Y Ranges')
        self.btn_autorange.setToolTip('Compute Y ranges for all session plots (locks them after).')
        self.btn_autorange.clicked.connect(self._auto_range_session)
        self.btn_start_session.clicked.connect(lambda: self._start_pipeline(mode='session'))
        self.btn_stop_session.clicked.connect(self.on_stop)
        ses_ctrl.addWidget(QtWidgets.QLabel('Window (s)'))
        ses_ctrl.addWidget(self.spin_window)
        ses_ctrl.addWidget(self.btn_set_base)
        ses_ctrl.addWidget(self.lbl_base_dir)
        ses_ctrl.addWidget(QtWidgets.QLabel('Subject'))
        ses_ctrl.addWidget(self.cmb_subjects)
        ses_ctrl.addWidget(self.btn_add_subj)
        ses_ctrl.addWidget(self.btn_del_subj)
        ses_ctrl.addWidget(self.rb_use_subject)
        ses_ctrl.addWidget(QtWidgets.QLabel('Log name'))
        self.le_log_name = QtWidgets.QLineEdit()
        self.le_log_name.setPlaceholderText('stream')
        self.le_log_name.setText('stream')
        self.le_log_name.setMaximumWidth(220)
        ses_ctrl.addWidget(self.le_log_name)
        ses_ctrl.addWidget(self.rb_use_logname)
        ses_ctrl.addWidget(self.btn_autorange)
        ses_ctrl.addWidget(self.btn_start_session)
        ses_ctrl.addWidget(self.btn_stop_session)
        ses_ctrl.addStretch(1)
        ses_layout.addLayout(ses_ctrl)
        self._build_plot_grid(parent_layout=ses_layout, prefix='session')
        self.lbl_session_status = QtWidgets.QLabel('Session idle.')
        ses_layout.addWidget(self.lbl_session_status)
        self.tabs.addTab(self.tab_session, 'Session')
        self._update_storage_mode()
        self._on_hw_mode_changed()

    def _load_subjects(self):
        try:
            if os.path.isfile(self._subjects_file):
                with open(self._subjects_file, 'r', encoding='utf-8') as f:
                    obj = json.load(f)
                if isinstance(obj, list):
                    return [str(x) for x in obj if isinstance(x, (str, int, float))]
        except Exception:
            pass
        return ['FakeSubject']

    def _save_subjects(self):
        try:
            with open(self._subjects_file, 'w', encoding='utf-8') as f:
                json.dump(self._subjects, f, indent=2)
        except Exception as e:
            print(f'[subjects] save failed: {e}')

    def _on_set_base_dir(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, 'Select Base Directory', self._base_dir)
        if path:
            self._base_dir = path
            self.lbl_base_dir.setText(self._base_dir)

    def _on_add_subject(self):
        name, ok = QtWidgets.QInputDialog.getText(self, 'Add Subject', 'Subject name:')
        if ok and name.strip():
            nm = name.strip()
            if nm not in self._subjects:
                self._subjects.append(nm)
                self._subjects.sort()
                self.cmb_subjects.clear()
                self.cmb_subjects.addItems(self._subjects)
                self.cmb_subjects.setCurrentText(nm)
                self._save_subjects()

    def _on_del_subject(self):
        nm = self.cmb_subjects.currentText().strip()
        if nm and nm != 'FakeSubject' and (nm in self._subjects):
            self._subjects.remove(nm)
            self.cmb_subjects.clear()
            self.cmb_subjects.addItems(self._subjects)
            self._save_subjects()

    def _update_storage_mode(self):
        use_subject = self.rb_use_subject.isChecked()
        self.cmb_subjects.setEnabled(use_subject)
        self.btn_add_subj.setEnabled(use_subject)
        self.btn_del_subj.setEnabled(use_subject)
        self.le_log_name.setEnabled(not use_subject)

    def _safe_name(self, s):
        s = (s or '').strip()
        s = re.sub('[^\\w\\-]+', '_', s)
        return s or 'stream'

    def _maybe_auto_verify(self):
        if isinstance(self.device, T7Device) and self._plan_ready and (not self._lut_in_progress()):
            if self.acquire_thread and self.acquire_thread.isRunning():
                return
            try:
                self._on_verify_lut()
            except Exception as e:
                print(f'[Verify] auto-verify failed: {e}')

    def _color_span(self, txt, ok):
        col = '#2ecc71' if ok else '#ff5252'
        return f"<span style='color:{col}'>{txt}</span>"

    def _format_verify_html(self, ex, vb, vp, f_est):
        f_set = float(ex.f_adj or 0.0)
        vpp_set = float(ex.vpp_volts or 0.0)
        vb_set = float(ex.vbias_volts or 0.0)

        def pct_err(meas, targ):
            if not np.isfinite(meas) or not np.isfinite(targ) or targ == 0:
                return float('nan')
            return 100.0 * (meas - targ) / targ
        f_err = pct_err(f_est, f_set)
        vpp_err = pct_err(vp, vpp_set)
        vb_err_mv = (vb - vb_set) * 1000.0 if np.isfinite(vb) and np.isfinite(vb_set) else float('nan')
        ok_f = abs(f_err) <= self._tol_freq_pct if np.isfinite(f_err) else False
        ok_vpp = abs(vpp_err) <= self._tol_vpp_pct if np.isfinite(vpp_err) else False
        ok_vb = abs(vb_err_mv) <= self._tol_vbias_mV if np.isfinite(vb_err_mv) else False
        f_str = self._color_span(f'Δ% {f_err:+.2f}', ok_f)
        vpp_str = self._color_span(f'Δ% {vpp_err:+.2f}', ok_vpp)
        vb_str = self._color_span(f'Δ {vb_err_mv:+.0f} mV', ok_vb)
        html = f"""<div style='font-family:Consolas, "Courier New", monospace; font-size:10px; color:black'>f_set {f_set:.1f}Hz, f_meas {f_est:.1f}Hz, {f_str}<br>vb_set {vb_set:.2f}V, vb_meas {vb:.2f}V, {vb_str}<br>vpp_set {vpp_set:.2f}V, vpp_meas {vp:.2f}V, {vpp_str}</div>"""
        return html

    def _lut_in_progress(self):
        return bool(hasattr(self, 'lut_thread') and self.lut_thread and self.lut_thread.isRunning())

    def _set_plan_ready(self, ready):
        self._plan_ready = bool(ready)
        allow = self._plan_ready and (not self._lut_in_progress())
        try:
            self.btn_start_session.setEnabled(allow)
        except Exception:
            pass

    def _auto_range_session(self):
        if not hasattr(self, 'plots_session'):
            return
        for name, pw in self.plots_session.items():
            try:
                pw.enableAutoRange(axis='y', enable=True)
                pw.getViewBox().autoRange()
                yr = pw.viewRange()[1]
                pw.enableAutoRange(axis='y', enable=False)
                pw.setYRange(yr[0], yr[1], padding=0.02)
                pw.setXRange(0, self._window_sec, padding=0)
            except Exception:
                pass

    def _build_plot_grid(self, parent_layout, prefix):
        grid = QtWidgets.QGridLayout()
        grid.setContentsMargins(4, 4, 4, 4)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        container = QtWidgets.QWidget()
        container.setLayout(grid)
        parent_layout.addWidget(container)

        def mkplot(title):
            pw = pg.PlotWidget(title=title)
            pw.setBackground('k')
            pw.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)
            pw.setMinimumHeight(150)
            pw.showGrid(x=True, y=True, alpha=0.25)
            pw.setMenuEnabled(False)
            pw.setLabel('bottom', 'Time', units='s')
            legend = pw.addLegend(offset=(8, 8))
            legend.setBrush(pg.mkBrush(0, 0, 0, 160))
            return pw
        plots = {'ie': mkplot('IE (AIN0)'), 'e1': mkplot('E1 (AIN1)'), 'e2': mkplot('E2 (AIN2)'), 'dins': mkplot('Digital Inputs'), 'pd_combined': mkplot('F1 + F2 Raw'), 'f1_demod': mkplot('F1 Demod Amplitude'), 'f2_demod': mkplot('F2 Demod Amplitude')}
        for key in ('ie', 'e1', 'e2', 'pd_combined'):
            plots[key].setLabel('left', 'Voltage', units='V')
        plots['dins'].setLabel('left', 'State')
        plots['f1_demod'].setLabel('left', 'Amplitude')
        plots['f2_demod'].setLabel('left', 'Amplitude')
        grid.addWidget(plots['ie'], 0, 0)
        grid.addWidget(plots['e1'], 1, 0)
        grid.addWidget(plots['e2'], 2, 0)
        grid.addWidget(plots['dins'], 3, 0)
        grid.addWidget(plots['pd_combined'], 0, 1)
        grid.addWidget(plots['f1_demod'], 1, 1)
        grid.addWidget(plots['f2_demod'], 2, 1)
        for r in range(4):
            grid.setRowStretch(r, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        if prefix == 'session':
            self.plots_session = plots
        else:
            self.plots_calib = plots

    def _on_hw_mode_changed(self):
        mock = not self.chk_hw.isChecked() or not HAVE_LJM
        self.btn_load_file.setEnabled(mock)

    def _on_load_session_file(self):
        path = QtWidgets.QFileDialog.getOpenFileName(self, 'Select Session CSV', 'logs', 'CSV Files (*.csv)')[0]
        if not path:
            return
        self.le_session_file.setText(path)
        self._loaded_session_file = path
        if isinstance(self.device, MockT7):
            try:
                self.device.load_csv(path)
                QtWidgets.QMessageBox.information(self, 'Loaded', f'Loaded playback file:\n{path}')
            except Exception as e:
                QtWidgets.QMessageBox.warning(self, 'Load Error', str(e))

    def _estimate_freq(self, t, y):
        if t.size < 3 or y.size != t.size:
            return float('nan')
        y0 = y - float(np.median(y))
        s0 = y0[:-1]
        s1 = y0[1:]
        mask = (s0 <= 0) & (s1 > 0)
        idx = np.nonzero(mask)[0]
        if idx.size < 2:
            return float('nan')
        dt = t[1:] - t[:-1]
        with np.errstate(divide='ignore', invalid='ignore'):
            frac = -s0[idx] / (s1[idx] - s0[idx])
        tcross = t[idx] + frac * dt[idx]
        if tcross.size < 2:
            return float('nan')
        periods = np.diff(tcross)
        if not periods.size:
            return float('nan')
        return float(1.0 / np.mean(periods))

    def _on_verify_lut(self):
        if not isinstance(self.device, T7Device):
            QtWidgets.QMessageBox.information(self, 'Info', 'Use Real T7 to verify LUT.')
            return
        if not self.plan:
            QtWidgets.QMessageBox.information(self, 'Info', 'Build a plan first.')
            return
        if self.acquire_thread and self.acquire_thread.isRunning():
            QtWidgets.QMessageBox.information(self, 'Busy', 'Stop the session before verifying.')
            return
        name_to_ain = {ex.name: ex.ain_monitor for ex in self.plan.excitations}
        for tag, widgets in self.lut_ain_widgets.items():
            ain = name_to_ain.get(tag, '?')
            widgets['plot'].setTitle(f"{tag} ({ain or 'NA'})")
            widgets['curve'].setData([], [])
            widgets['label'].setText('—')
        plot_win = min((w['spin'].value() for w in self.lut_ain_widgets.values()))
        try:
            t, ain_names, raw = self.device.sample_monitors_raw(window_s=plot_win, settling_us=10)
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, 'Verify Error', str(e))
            return
        ain_to_tag = {ex.ain_monitor: ex.name for ex in self.plan.excitations if ex.ain_monitor}
        freq_est = {}
        for i, ain in enumerate(ain_names):
            tag = ain_to_tag.get(ain, None)
            if tag and tag in self.lut_ain_widgets:
                w = self.lut_ain_widgets[tag]
                win_s = float(w['spin'].value())
                tx = t / t[-1] * win_s if t.size > 1 else np.array([0.0])
                y = raw[i]
                w['curve'].setData(tx, y)
                freq_est[tag] = self._estimate_freq(t, y)
        meas = self.device.measure_monitors_pp(duration_s=0.1)
        parts = []
        ex_by_name = {ex.name: ex for ex in self.plan.excitations}
        for nm, ex in ex_by_name.items():
            if nm not in meas:
                continue
            vb = float(meas[nm]['vbias'])
            vp = float(meas[nm]['vpp'])
            f_est = float(freq_est.get(nm, float('nan')))
            if nm in self.lut_ain_widgets:
                html = self._format_verify_html(ex, vb, vp, f_est)
                self.lut_ain_widgets[nm]['label'].setText(html)
            parts.append(f'{nm}: vbias={vb:.3f}V vpp={vp:.3f}V f≈{f_est:.1f}Hz (target {ex.f_adj:.1f}Hz)')
        msg = '[Verify LUT] ' + ' | '.join(parts) if parts else '[Verify LUT] No monitor AINs.'
        self.lbl_lut_status.setText(msg)
        print(msg)

    def on_build_plan(self):
        self.table.pull(self.specs)
        try:
            Fs_req = float(self.fs_spin.value())
            cs = {'IE': 'DIO5', 'E1': 'DIO6', 'E2': 'DIO7'}
            mcp = {'IE': 'A', 'E1': 'B', 'E2': 'C'}
            ser = {'IE': 'DIO8', 'E1': 'DIO9', 'E2': 'DIO10'}
            for c in self.specs:
                if c.role != 'EXCITATION':
                    continue
                if c.ad9833 is None and c.name in cs:
                    c.ad9833 = AD9833Cfg(cs_dio=cs[c.name])
                if c.mcp4728 is None and c.name in mcp:
                    c.mcp4728 = MCP4728Cfg(chan=mcp[c.name])
                if c.vpp_shift is None and c.name in ser:
                    c.vpp_shift = ShiftVppCfg(ser_dio=ser[c.name])
            self.plan = build_plan(self.specs, Fs_req)
            fadj_txt = ', '.join((f'{ex.name}={ex.f_adj:.2f} Hz' for ex in self.plan.excitations))
            self.lbl_plan_status.setText(f'Plan: IC mode  Fs={self.plan.Fs:.1f} Hz  L={self.plan.L}  |  f_adj: {fadj_txt}')
            print(self.lbl_plan_status.text())
            self._set_plan_ready(False)
            if self.chk_hw.isChecked():
                if not isinstance(self.device, T7Device):
                    try:
                        if isinstance(self.device, T7Device):
                            self.device.dispose()
                    except Exception:
                        pass
                    self.device = T7Device()
            else:
                if not isinstance(self.device, MockT7):
                    try:
                        if isinstance(self.device, T7Device):
                            self.device.dispose()
                    except Exception:
                        pass
                    self.device = MockT7()
                if self._loaded_session_file and os.path.isfile(self._loaded_session_file):
                    try:
                        self.device.load_csv(self._loaded_session_file)
                    except Exception as e:
                        print('Playback load warning:', e)
            self.device.apply_plan(self.plan)
            if isinstance(self.device, T7Device):
                try:
                    lut_path = 'vpp_lut.json'
                    if os.path.isfile(lut_path) and self.device.load_vpp_luts(lut_path):
                        print('[LUT] Using existing LUT file.')
                        self.device.apply_plan_settings(lut_path, force_rebuild_lut=False, retrim_bias=True)
                        self._set_plan_ready(True)
                        self._maybe_auto_verify()
                    else:
                        print('[LUT] No matching LUT file. Launching interactive Build LUT...')
                        self._on_build_lut()
                except Exception as e:
                    print(f'[Apply] Warning: LUT check failed: {e}')
            else:
                self._set_plan_ready(True)
            self.mplot_session = MultiPlotAdapter(self.plan, self.specs, self.plots_session, self._window_sec, self._demod_ylim)
            self.mplot_calib = MultiPlotAdapter(self.plan, self.specs, self.plots_calib, self._window_sec, self._demod_ylim)
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, 'Plan Error', str(e))

    def _cleanup_after_lut_abort(self, reason='aborted'):
        print('cleanup_after_lut_abort:', reason)
        self._lut_abort_pending = False
        if hasattr(self, 'lut_thread') and self.lut_thread:
            try:
                self.lut_thread.quit()
                self.lut_thread.wait(1000)
            except Exception:
                pass
        self.lut_thread = None
        self.lut_worker = None
        lut_path = 'vpp_lut.json'
        if os.path.isfile(lut_path):
            try:
                os.remove(lut_path)
                print(f'[VPP LUT] removed {lut_path} ({reason})')
            except Exception as e:
                bad = f'{lut_path}.invalid'
                try:
                    os.replace(lut_path, bad)
                    print(f'[VPP LUT] could not remove ({e}); renamed to {bad}')
                except Exception as e2:
                    print(f'[VPP LUT] cleanup failed: {e2}')
        if isinstance(self.device, T7Device):
            if hasattr(self.device, '_vpp_lut'):
                try:
                    delattr(self.device, '_vpp_lut')
                except Exception:
                    pass
            setattr(self.device, '_current_vpp_codes', {})
        self.plan = None
        self.lbl_plan_status.setText(f'No plan. (LUT {reason})')
        self.btn_build_lut.setEnabled(True)
        self.btn_abort_lut.setEnabled(False)
        self.lbl_lut_status.setText(f'LUT {reason}.')
        self._set_plan_ready(False)

    def _on_abort_lut(self):
        self._lut_abort_pending = True
        if hasattr(self, 'lut_worker') and self.lut_worker:
            try:
                self.lut_worker.stop()
            except Exception:
                pass
        self.lbl_lut_status.setText('Aborting LUT...')
        self.btn_abort_lut.setEnabled(False)

    def _on_build_lut(self):
        if not isinstance(self.device, T7Device):
            QtWidgets.QMessageBox.information(self, 'Info', 'Use Real T7 to build LUT.')
            return
        if not self.plan:
            QtWidgets.QMessageBox.information(self, 'Info', 'Build a plan first.')
            return
        name_to_ain = {ex.name: ex.ain_monitor for ex in self.plan.excitations}
        for tag, widgets in self.lut_ain_widgets.items():
            ain = name_to_ain.get(tag, '?')
            widgets['plot'].setTitle(f"{tag} ({ain or 'NA'})")
            widgets['curve'].setData([], [])
            widgets['label'].setText('—')
        self._lut_abort_pending = False
        self.btn_build_lut.setEnabled(False)
        self.btn_abort_lut.setEnabled(True)
        self.lbl_lut_status.setText('Building LUT...')
        self._set_plan_ready(False)
        self.lut_thread = QtCore.QThread()
        plot_win = min((w['spin'].value() for w in self.lut_ain_widgets.values()))
        self.lut_worker = VppLutWorker(self.device, plot_win_s=plot_win, settle_s=0.015, measure_s=0.08)
        self.lut_worker.moveToThread(self.lut_thread)
        self.lut_thread.started.connect(self.lut_worker.run)
        self.lut_worker.waveReady.connect(self._on_lut_wave)
        self.lut_worker.stepStatus.connect(self._on_lut_status)
        self.lut_worker.finished.connect(self._on_lut_finished)
        self.lut_worker.error.connect(self._on_lut_error)
        self.lut_worker.finished.connect(self.lut_thread.quit)
        self.lut_worker.error.connect(self.lut_thread.quit)
        self.lut_thread.finished.connect(self._on_lut_thread_finished)
        self.lut_thread.start()

    @QtCore.pyqtSlot()
    def _on_lut_thread_finished(self):
        self.lut_thread = None
        self._set_plan_ready(self._plan_ready)
        if self._plan_ready:
            QtCore.QTimer.singleShot(0, self._maybe_auto_verify)

    @QtCore.pyqtSlot(object)
    def _on_lut_wave(self, payload):
        t = payload['t']
        waves = payload['waves']
        ain_to_tag = {ex.ain_monitor: ex.name for ex in self.plan.excitations if ex.ain_monitor}
        for ain, y in waves.items():
            tag = ain_to_tag.get(ain, None)
            if tag and tag in self.lut_ain_widgets:
                w = self.lut_ain_widgets[tag]
                win_s = float(w['spin'].value())
                if t.size > 1:
                    tx = t / t[-1] * win_s
                else:
                    tx = np.array([0.0])
                w['curve'].setData(tx, y)

    @QtCore.pyqtSlot(str)
    def _on_lut_status(self, msg):
        self.lbl_lut_status.setText(msg)
        print(msg)

    @QtCore.pyqtSlot(object)
    def _on_lut_finished(self, luts):
        if self._lut_abort_pending:
            self._cleanup_after_lut_abort(reason='aborted')
            return
        self.device._vpp_lut = luts
        tmp_path = 'vpp_lut.json.tmp'
        final_path = 'vpp_lut.json'
        try:
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(dict(luts=luts), f, indent=2)
            os.replace(tmp_path, final_path)
            print('[VPP LUT] saved vpp_lut.json (atomic)')
        except Exception as e:
            print(f'[VPP LUT] save failed: {e}')
            try:
                if os.path.isfile(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
        self.lbl_lut_status.setText('LUT build complete.')
        self.btn_build_lut.setEnabled(True)
        self.btn_abort_lut.setEnabled(False)
        try:
            if isinstance(self.device, T7Device):
                self.device.apply_plan_settings(lut_path=final_path, force_rebuild_lut=False, retrim_bias=True)
        except Exception as e:
            print(f'[VPP LUT] apply after build failed: {e}')
        self._set_plan_ready(True)
        self._maybe_auto_verify()

    @QtCore.pyqtSlot(str)
    def _on_lut_error(self, err):
        print('LUT error:', err)
        self.lbl_lut_status.setText(f'Error: {err}')
        self._cleanup_after_lut_abort(reason='error')

    def _clear_plot_widgets(self, plots):
        if not plots:
            return
        for pw in plots.values():
            try:
                pw.clear()
                if pw.plotItem.legend is None:
                    pw.addLegend(offset=(8, 8)).setBrush(pg.mkBrush(0, 0, 0, 160))
                pw.setXRange(0, self._window_sec, padding=0)
            except Exception:
                pass

    def _start_pipeline(self, mode):
        if not self.plan:
            QtWidgets.QMessageBox.information(self, 'Info', 'Build plan first.')
            return
        if self.acquire_thread and self.acquire_thread.isRunning():
            return
        self._current_mode = mode
        self._recording_enabled = mode == 'session'
        if mode == 'session':
            if self._lut_in_progress():
                QtWidgets.QMessageBox.information(self, 'LUT In Progress', 'Please wait for LUT build to finish before starting a session.')
                return
            if not self._plan_ready:
                QtWidgets.QMessageBox.information(self, 'Plan Not Ready', 'Build Plan (and complete LUT if required) before starting a session.')
                return
        if mode == 'session' and hasattr(self, 'plots_session'):
            self._clear_plot_widgets(self.plots_session)
        if mode == 'calibration' and hasattr(self, 'plots_calib'):
            self._clear_plot_widgets(self.plots_calib)
        if mode == 'session' and self.mplot_session:
            self.mplot_session.reset_items()
        if mode == 'calibration' and self.mplot_calib:
            self.mplot_calib.reset_items()
        if self._recording_enabled:
            self.csv_queue = queue.Queue(maxsize=16)
            subject = self._safe_name(self.cmb_subjects.currentText().strip() or 'FakeSubject')
            log_dir = self._base_dir
            if getattr(self, 'rb_use_subject', None) and self.rb_use_subject.isChecked():
                log_dir = os.path.join(log_dir, subject)
            os.makedirs(log_dir, exist_ok=True)
            out_path_h5 = os.path.join(log_dir, f"{subject}_{time.strftime('%Y%m%d')}_fiber_photometry.h5")
            override_plan = override_channels = override_channel_key = None
            if isinstance(self.device, MockT7):
                meta = self.device.get_loaded_metadata()
                if meta:
                    override_plan, override_channels, override_channel_key = meta
            self.csv_writer = H5Writer(self.csv_queue, out_path_h5, specs=self.specs, plan=self.plan, override_plan=override_plan, override_channels=override_channels, override_channel_key=override_channel_key)
            self.csv_writer.start()
            print(f'[Session] Logging to HDF5: {out_path_h5}')
        else:
            self.csv_queue = None
            self.csv_writer = None
        self.acquire_thread = QtCore.QThread()
        self.acq_worker = AcquireWorker(self.device)
        self.acq_worker.moveToThread(self.acquire_thread)
        self.acquire_thread.started.connect(self.acq_worker.start_loop)
        self.acq_worker.finished.connect(self.acquire_thread.quit)
        self.acq_worker.error.connect(self._on_error)
        self.process_thread = QtCore.QThread()
        self.proc_worker = ProcessWorker(self.plan, ui_hz=40.0, max_window_s=max(self._window_sec, 10.0), tau1_ms=8.0, tau2_ms=8.0, decim_hz=600.0, baseline_sec=5.0, dc_tau_s=2.0, common_mode=False)
        self.proc_worker.moveToThread(self.process_thread)
        self.acq_worker.chunkReady.connect(self.proc_worker.process_chunk)
        self.proc_worker.uiUpdate.connect(self._on_ui_update)
        self.proc_worker.rawBatch.connect(self._on_raw_batch)
        self.proc_worker.error.connect(self._on_error)
        self.proc_worker.finished.connect(self.process_thread.quit)
        if mode == 'session':
            self.btn_start_session.setEnabled(False)
            self.btn_stop_session.setEnabled(True)
            self.lbl_session_status.setText('Session running (recording).')
        else:
            self.btn_start_calib.setEnabled(False)
            self.btn_stop_calib.setEnabled(True)
            self.lbl_calib_status.setText('Calibration running (no recording).')
        self.acquire_thread.start()
        self.process_thread.start()

    def on_stop(self):
        try:
            if self.acq_worker:
                self.acq_worker.stop()
        except Exception:
            pass
        try:
            if self.proc_worker:
                self.proc_worker.stop()
        except Exception:
            pass
        if self.acquire_thread:
            self.acquire_thread.quit()
            self.acquire_thread.wait(2000)
            self.acquire_thread = None
        if self.process_thread:
            self.process_thread.quit()
            self.process_thread.wait(2000)
            self.process_thread = None
        if self.csv_writer:
            self.csv_writer.stop()
            self.csv_writer.join(timeout=2)
            self.csv_writer = None
        if self._current_mode == 'session':
            self.btn_start_session.setEnabled(True)
            self.btn_stop_session.setEnabled(False)
            self.lbl_session_status.setText('Session stopped.')
        else:
            self.btn_start_calib.setEnabled(True)
            self.btn_stop_calib.setEnabled(False)
            self.lbl_calib_status.setText('Calibration stopped.')

    def _on_ui_update(self, payload):
        if self._current_mode == 'session' and self.mplot_session:
            self.mplot_session.update(payload)
        elif self._current_mode == 'calibration' and self.mplot_calib:
            self.mplot_calib.update(payload)

    def _on_raw_batch(self, payload):
        if self._recording_enabled and self.csv_queue:
            if not hasattr(self, '_csv_stats'):
                self._csv_stats = {'put': 0, 'drop': 0, 'max_q': 0}
            self._csv_stats['put'] += 1
            q = self.csv_queue
            qsize = q.qsize()
            if qsize > self._csv_stats['max_q']:
                self._csv_stats['max_q'] = qsize
            try:
                q.put(payload, timeout=0.5)
            except queue.Full:
                self._csv_stats['drop'] += 1
                if self._csv_stats['drop'] % 10 == 1:
                    print(f"[CSVQueue] DROPPED {self._csv_stats['drop']} chunks (max_q={self._csv_stats['max_q']})")

    def _on_error(self, msg):
        print('ERROR:', msg)
        if self._current_mode == 'session':
            self.lbl_session_status.setText(f'Error: {msg}')
        else:
            self.lbl_calib_status.setText(f'Error: {msg}')

    def _update_window(self, v):
        self._window_sec = v
        if self.mplot_session:
            self.mplot_session.set_window(v)
        if self.mplot_calib:
            self.mplot_calib.set_window(v)

    def closeEvent(self, e):
        self.on_stop()
        try:
            if isinstance(self.device, T7Device):
                self.device.stop()
        except Exception:
            pass
        super().closeEvent(e)
