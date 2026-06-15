import math
import time
import numpy as np
from PyQt6 import QtCore

class VppLutWorker(QtCore.QObject):
    waveReady = QtCore.pyqtSignal(object)
    stepStatus = QtCore.pyqtSignal(str)
    finished = QtCore.pyqtSignal(object)
    error = QtCore.pyqtSignal(str)

    def __init__(self, dev, codes=None, plot_win_s=0.003, settle_s=0.015, measure_s=0.08):
        super().__init__()
        self._dev = dev
        self.codes = list(codes) if codes is not None else list(range(64))
        self.plot_win_s = float(plot_win_s)
        self.settle_s = float(settle_s)
        self.measure_s = float(measure_s)
        self._stop = False

    @QtCore.pyqtSlot()
    def run(self):
        try:
            if self._dev.plan is None:
                raise RuntimeError('No plan applied.')
            names = [ex.name for ex in self._dev.plan.excitations if ex.vpp_shift]
            luts = {nm: [float('nan')] * 64 for nm in names}
            for c in self.codes:
                if self._stop:
                    break
                self._dev._parallel_shift_codes({nm: int(c) for nm in luts.keys()})
                time.sleep(self.settle_s)
                t, ain_names, raw = self._dev.sample_monitors_raw(window_s=self.plot_win_s, settling_us=10)
                waves = {ain: raw[i].copy() for i, ain in enumerate(ain_names)}
                self.waveReady.emit(dict(t=t, waves=waves, code=int(c)))
                meas = self._dev.measure_monitors_pp(duration_s=self.measure_s)
                msg = f'[VPP LUT] code {c:02d} -> ' + ', '.join((f"{nm}:vbias={meas.get(nm, {}).get('vbias', float('nan')):.3f} vpp={meas.get(nm, {}).get('vpp', float('nan')):.3f}" for nm in luts.keys()))
                self.stepStatus.emit(msg)
                for nm in luts.keys():
                    vpp = meas.get(nm, {}).get('vpp', float('nan'))
                    luts[nm][c] = float(vpp)
            self.finished.emit(luts)
        except Exception as e:
            self.error.emit(str(e))

    @QtCore.pyqtSlot()
    def stop(self):
        self._stop = True

class AcquireWorker(QtCore.QObject):
    chunkReady = QtCore.pyqtSignal(object)
    finished = QtCore.pyqtSignal()
    error = QtCore.pyqtSignal(str)

    def __init__(self, device):
        super().__init__()
        self._dev = device
        self._running = False

    @QtCore.pyqtSlot()
    def start_loop(self):
        self._running = True
        try:
            self._dev.start()
            while self._running:
                rc = self._dev.read_chunk()
                if rc is None:
                    break
                si0, data, meas = rc
                self.chunkReady.emit((si0, data, list(meas), self._dev.plan.Fs, self._dev.plan.L))
            self._dev.stop()
        except Exception as e:
            self.error.emit(f'Acquire: {e}')
        finally:
            self._running = False
            self.finished.emit()

    @QtCore.pyqtSlot()
    def stop(self):
        self._running = False
        try:
            self._dev.stop()
        except Exception:
            pass

class ProcessWorker(QtCore.QObject):
    uiUpdate = QtCore.pyqtSignal(object)
    rawBatch = QtCore.pyqtSignal(object)
    finished = QtCore.pyqtSignal()
    error = QtCore.pyqtSignal(str)

    def __init__(self, plan, ui_hz=40.0, max_window_s=10.0, tau1_ms=10.0, tau2_ms=10.0, decim_hz=500.0, baseline_sec=5.0, dc_tau_s=2.0, common_mode=True):
        super().__init__()
        self.plan = plan
        self.ui_interval = 1.0 / ui_hz
        self.max_window_s = max_window_s
        self._last_ui = 0.0
        self._running = False
        self._raw_t = []
        self._raw_buf = []
        self._A_time_blocks = []
        self._A_blocks = []
        self.tau1_ms = tau1_ms
        self.tau2_ms = tau2_ms
        self.decim_hz = decim_hz
        self.baseline_sec = baseline_sec
        self.dc_tau_s = dc_tau_s
        self.common_mode = common_mode
        self._Fs = plan.Fs
        self._dt = 1.0 / self._Fs
        self._alpha1 = None
        self._alpha2 = None
        self._decim_every = None
        self._alpha_base = None
        self._alpha_dc = None
        self._I1 = None
        self._Q1 = None
        self._I2 = None
        self._Q2 = None
        self._baseline = None
        self._dc_level = None
        self._cos_table = None
        self._sin_table = None
        self._sample_index = 0
        self._pd_rows = None
        self._n_pd = 0
        self._n_exc = len(self.plan.excitations)

    def stop(self):
        self._running = False

    @QtCore.pyqtSlot(object)
    def process_chunk(self, chunk):
        if not self._running:
            self._running = True
        try:
            si0, data, names, Fs, L = chunk
            item = self._ingest_raw(si0, data, names, Fs, L)
            demod = self._lockin_stream(si0, data, names, L)
            if demod:
                item['demod_t'], item['demod_A'] = demod
            self.rawBatch.emit(item)
            self._maybe_emit_ui(Fs)
        except Exception as e:
            self.error.emit(f'Process: {e}')

    def _ingest_raw(self, si0, data, names, Fs, L):
        self._raw_t.append(si0 / Fs)
        start_t = self._raw_t[-1] - self.max_window_s
        self._raw_buf.append(data)
        while len(self._raw_t) > 1 and self._raw_t[0] < start_t:
            self._raw_t.pop(0)
            self._raw_buf.pop(0)
        t = si0 / Fs + np.arange(L) / Fs
        return dict(si0=si0, L=L, Fs=Fs, t=t, raw=data.copy(), names=names)

    def _init_lockin_state(self, names):
        exc_monitors = [ex.ain_monitor for ex in self.plan.excitations if ex.ain_monitor]
        name_to_row = {n: i for i, n in enumerate(names)}
        self._pd_rows = [name_to_row[n] for n in self.plan.scan_ain if n not in exc_monitors]
        self._n_pd = len(self._pd_rows)
        if self._n_pd == 0:
            return
        self._alpha1 = self._dt / (self.tau1_ms / 1000.0)
        if self._alpha1 > 1.0:
            self._alpha1 = 1.0
        if self.tau2_ms:
            self._alpha2 = self._dt / (self.tau2_ms / 1000.0)
            if self._alpha2 > 1.0:
                self._alpha2 = 1.0
        else:
            self._alpha2 = None
        self._decim_every = max(1, int(round(self._Fs / self.decim_hz)))
        decim_dt = self._decim_every / self._Fs
        self._alpha_base = math.exp(-decim_dt / self.baseline_sec) if self.baseline_sec > 0 else 0.0
        self._alpha_dc = math.exp(-self._dt / self.dc_tau_s) if self.dc_tau_s > 0 else 0.0
        self._I1 = np.zeros((self._n_pd, self._n_exc), dtype=np.float32)
        self._Q1 = np.zeros_like(self._I1)
        self._I2 = np.zeros_like(self._I1) if self._alpha2 is not None else None
        self._Q2 = np.zeros_like(self._Q1) if self._alpha2 is not None else None
        self._baseline = np.zeros((self._n_pd, self._n_exc), dtype=np.float32)
        self._dc_level = np.zeros(self._n_pd, dtype=np.float32)
        L = self.plan.L
        m = np.arange(L, dtype=np.float32) / L
        cos_t = []
        sin_t = []
        for ex in self.plan.excitations:
            k = ex.k
            ph = 2.0 * math.pi * k * m
            cos_t.append(np.cos(ph, dtype=np.float32))
            sin_t.append(np.sin(ph, dtype=np.float32))
        self._cos_table = np.stack(cos_t, axis=0)
        self._sin_table = np.stack(sin_t, axis=0)

    def _lockin_stream(self, si0, chunk_data, names, Lc):
        if self._I1 is None:
            self._init_lockin_state(names)
        if self._n_pd == 0:
            return
        data = chunk_data[self._pd_rows]
        L_tbl = self.plan.L
        alpha1 = self._alpha1
        alpha2 = self._alpha2
        alpha_dc = self._alpha_dc
        alpha_base = self._alpha_base
        decim_every = self._decim_every
        t_samples = []
        A_samples = []
        for s in range(Lc):
            idx = (si0 + s) % L_tbl
            c_ref = self._cos_table[:, idx]
            s_ref = self._sin_table[:, idx]
            x = data[:, s].astype(np.float32)
            if self.common_mode and self._n_pd > 1:
                cm = float(x.mean())
                x = x - cm
            if alpha_dc > 0:
                self._dc_level = alpha_dc * self._dc_level + (1 - alpha_dc) * x
                x_c = x - self._dc_level
            else:
                x_c = x
            mix_c = x_c[:, None] * c_ref[None, :]
            mix_s = x_c[:, None] * s_ref[None, :]
            self._I1 = (1 - alpha1) * self._I1 + alpha1 * mix_c
            self._Q1 = (1 - alpha1) * self._Q1 + alpha1 * mix_s
            if alpha2 is not None:
                self._I2 = (1 - alpha2) * self._I2 + alpha2 * self._I1
                self._Q2 = (1 - alpha2) * self._Q2 + alpha2 * self._Q1
                I_eff = self._I2
                Q_eff = self._Q2
            else:
                I_eff = self._I1
                Q_eff = self._Q1
            if self._sample_index % decim_every == 0:
                A = 2.0 * np.sqrt(I_eff * I_eff + Q_eff * Q_eff)
                if alpha_base > 0:
                    self._baseline = alpha_base * self._baseline + (1 - alpha_base) * A
                t_samples.append((si0 + s) / self._Fs)
                A_samples.append(A.copy())
            self._sample_index += 1
        if t_samples:
            t_arr = np.asarray(t_samples, dtype=np.float64)
            A_arr = np.stack(A_samples, axis=0)
            self._A_time_blocks.append(t_arr)
            self._A_blocks.append(A_arr)
            latest = t_arr[-1]
            cutoff = latest - self.max_window_s
            while len(self._A_time_blocks) > 1 and self._A_time_blocks[0][-1] < cutoff:
                self._A_time_blocks.pop(0)
                self._A_blocks.pop(0)
            return (t_arr, A_arr.copy())
        return None

    def _maybe_emit_ui(self, Fs):
        now = time.perf_counter()
        if now - self._last_ui < self.ui_interval:
            return
        self._last_ui = now
        if not self._A_time_blocks:
            return
        raw_cat = np.concatenate(self._raw_buf, axis=1)
        t0 = self._raw_t[0]
        total_samples = raw_cat.shape[1]
        traw = t0 + np.arange(total_samples) / Fs
        t_amp = np.concatenate(self._A_time_blocks)
        A_cat = np.concatenate(self._A_blocks, axis=0)
        self.uiUpdate.emit(dict(t_raw=traw, raw=raw_cat, t_amp=t_amp, A=A_cat))
