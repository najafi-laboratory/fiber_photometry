import json
import math
import os
import threading
import time
import numpy as np
from PyQt6 import QtCore
try:
    from labjack import ljm
    HAVE_LJM = True
except Exception:
    ljm = None
    HAVE_LJM = False
MCP4728_VDD_VOLTS = 5.032

def _mcp4728_build_simple(chan_letter, volts):
    cmd_map = {'A': 88, 'B': 90, 'C': 92, 'D': 94}
    ch = chan_letter.upper()
    if ch not in cmd_map:
        raise ValueError('MCP4728 chan must be A/B/C/D')
    code = int(round(max(0.0, min(float(volts), MCP4728_VDD_VOLTS)) * 4095 / MCP4728_VDD_VOLTS))
    return [cmd_map[ch], 16 + (code >> 8 & 15), code & 255]

def _ad9833_words_for_freq(f_hz, mclk):
    fw = int(round(f_hz * (1 << 28) / mclk))
    return (16384 | fw & 16383, 16384 | fw >> 14 & 16383)

def _spi_tx_words(handle, cs, clk, miso, mosi, words, mode, speed_throttle):
    ljm.eWriteName(handle, 'SPI_CS_DIONUM', int(cs.replace('DIO', '')))
    ljm.eWriteName(handle, 'SPI_CLK_DIONUM', int(clk.replace('DIO', '')))
    ljm.eWriteName(handle, 'SPI_MISO_DIONUM', int(miso.replace('DIO', '')))
    ljm.eWriteName(handle, 'SPI_MOSI_DIONUM', int(mosi.replace('DIO', '')))
    ljm.eWriteName(handle, 'SPI_MODE', mode)
    ljm.eWriteName(handle, 'SPI_SPEED_THROTTLE', speed_throttle)
    ljm.eWriteName(handle, 'SPI_OPTIONS', 0)
    tx = b''.join((int(w & 65535).to_bytes(2, 'big') for w in words))
    ljm.eWriteName(handle, 'SPI_NUM_BYTES', len(tx))
    ljm.eWriteNameByteArray(handle, 'SPI_DATA_TX', len(tx), list(tx))
    ljm.eWriteName(handle, 'SPI_GO', 1)
MCP_I2C_MIN_INTERVAL_S = 0.06
_I2C_LAST_TS = {}
_I2C_LOCKS = {}

def _i2c_tx_bytes_rate_limited(handle, addr7, sda, scl, speed_throttle, payload, min_interval_s=MCP_I2C_MIN_INTERVAL_S):
    lock = _I2C_LOCKS.setdefault(addr7, threading.Lock())
    with lock:
        now = time.perf_counter()
        wait = min_interval_s - (now - _I2C_LAST_TS.get(addr7, 0.0))
        if wait > 0:
            time.sleep(wait)
        for name, value in (('I2C_SDA_DIONUM', sda), ('I2C_SCL_DIONUM', scl), ('I2C_SPEED_THROTTLE', speed_throttle), ('I2C_OPTIONS', 0), ('I2C_SLAVE_ADDRESS', addr7), ('I2C_NUM_BYTES_TX', len(payload)), ('I2C_NUM_BYTES_RX', 0)):
            ljm.eWriteName(handle, name, value)
        ljm.eWriteNameByteArray(handle, 'I2C_DATA_TX', len(payload), payload)
        ljm.eWriteName(handle, 'I2C_GO', 1)
        _I2C_LAST_TS[addr7] = time.perf_counter()
TSETUP = 5e-07
THOLD = 5e-07
TCLK_H = 8e-07
TCLK_L = 8e-07
TLATCH = 8e-07

def _dio_write(h, pin, v):
    ljm.eWriteName(h, pin, int(bool(v)))

def _shift_vpp_parallel(h, ser_pins, srclk, rclk, codes, nbits, msb_first):
    _dio_write(h, srclk, 0)
    _dio_write(h, rclk, 0)
    for sp in ser_pins:
        _dio_write(h, sp, 0)
    bit_range = range(nbits - 1, -1, -1) if msb_first else range(nbits)
    bit_rows = [[code >> b & 1 for b in bit_range] for code in codes]
    for i in range(nbits):
        for sp, bits in zip(ser_pins, bit_rows):
            _dio_write(h, sp, bits[i])
        time.sleep(TSETUP)
        _dio_write(h, srclk, 1)
        time.sleep(TCLK_H)
        _dio_write(h, srclk, 0)
        time.sleep(TCLK_L)
        time.sleep(THOLD)
    _dio_write(h, rclk, 1)
    time.sleep(TLATCH)
    _dio_write(h, rclk, 0)

class MockT7(QtCore.QObject):

    def __init__(self):
        super().__init__()
        self.plan = None
        self.running = False
        self._si = 0
        self.measure_names = []
        self._rng = np.random.default_rng(1234)
        self._next_time = 0.0
        self._play_data = None
        self._play_t = None
        self._play_pos = 0
        self._play_names = None
        self._loaded_plan_meta = None
        self._loaded_channels_meta = None
        self._loaded_channel_key_meta = None

    def load_csv(self, path):
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        cols = []
        names = None
        ts = []
        self._loaded_plan_meta = None
        self._loaded_channels_meta = None
        self._loaded_channel_key_meta = None
        with open(path, 'r') as fh:
            for ln in fh:
                if ln.startswith('#'):
                    if ln.startswith('# PLAN '):
                        try:
                            self._loaded_plan_meta = json.loads(ln[len('# PLAN '):].strip())
                        except Exception:
                            pass
                    elif ln.startswith('# CHANNELS '):
                        try:
                            self._loaded_channels_meta = json.loads(ln[len('# CHANNELS '):].strip())
                        except Exception:
                            pass
                    elif ln.startswith('# CHANNEL_KEY '):
                        try:
                            self._loaded_channel_key_meta = json.loads(ln[len('# CHANNEL_KEY '):].strip())
                        except Exception:
                            pass
                    continue
                parts = ln.strip().split(',')
                if names is None:
                    names = parts
                    if names[0].lower() != 'timestamp':
                        raise ValueError("First column must be 'timestamp'")
                    names = names[1:]
                    continue
                ts.append(float(parts[0]))
                row = [float(x) for x in parts[1:]]
                cols.append(row)
        if not cols:
            raise ValueError('No data rows in CSV.')
        arr = np.array(cols, dtype=np.float32)
        self._play_t = np.array(ts, dtype=np.float64)
        self._play_data = arr.T
        self._play_pos = 0
        self._play_names = list(names)

    def get_loaded_metadata(self):
        if self._loaded_plan_meta is None and self._loaded_channels_meta is None and (self._loaded_channel_key_meta is None):
            return None
        return (self._loaded_plan_meta or {}, self._loaded_channels_meta or [], self._loaded_channel_key_meta or {})

    def apply_plan(self, plan):
        if self.running:
            raise RuntimeError('Stop before re-applying plan.')
        self.plan = plan
        self.measure_names = list(plan.scan_ain) + list(plan.scan_dio)
        if self._play_data is not None and self._play_names is not None:
            if len(self._play_names) != len(self.measure_names):
                print('Warning: playback column count != plan measurement count; truncating to match.')
                m = min(len(self._play_names), len(self.measure_names))
                self._play_data = self._play_data[:m]
                self.measure_names = self.measure_names[:m]

    def start(self):
        if not self.plan:
            raise RuntimeError('No plan applied.')
        self.running = True
        self._si = 0
        self._next_time = time.perf_counter()
        self._play_pos = 0

    def stop(self):
        self.running = False

    def _synth_chunk(self, L, Fs):
        excit_map = {ex.ain_monitor: ex.waveform for ex in self.plan.excitations if ex.ain_monitor}
        rows = []
        rng = self._rng
        for nm in self.plan.scan_ain:
            if nm in excit_map:
                w = excit_map[nm] + 0.002 * rng.standard_normal(L)
                rows.append(w.astype(np.float32))
            else:
                mix = np.zeros(L, dtype=np.float32)
                for idx, ex in enumerate(self.plan.excitations):
                    mix += (0.25 + 0.05 * idx) * np.sin(2 * math.pi * ex.k * np.arange(L) / L)
                mix += 0.02 * rng.standard_normal(L)
                mix -= mix.min()
                mmax = mix.max()
                if mmax > 0:
                    mix /= mmax
                rows.append(mix.astype(np.float32))
        for dnm in self.plan.scan_dio:
            dig = np.zeros(L, dtype=np.float32)
            if rng.random() < 0.3:
                pos = rng.integers(0, L - 30)
                dig[pos:pos + 20] = 3.3
            rows.append(dig)
        return np.stack(rows, axis=0)

    def _playback_chunk(self, L):
        if self._play_data is None:
            return None
        N = self._play_data.shape[1]
        if self._play_pos >= N:
            self._play_pos = 0
        end = min(self._play_pos + L, N)
        chunk = self._play_data[:, self._play_pos:end]
        if chunk.shape[1] < L:
            needed = L - chunk.shape[1]
            extra = self._play_data[:, :needed]
            chunk = np.concatenate([chunk, extra], axis=1)
            self._play_pos = needed
        else:
            self._play_pos = end
        return chunk.astype(np.float32)

    def read_chunk(self):
        if not self.running or not self.plan:
            return None
        L = self.plan.L
        Fs = self.plan.Fs
        now = time.perf_counter()
        slot = L / Fs
        if now < self._next_time:
            time.sleep(self._next_time - now)
        self._next_time += slot
        if self._play_data is not None:
            data = self._playback_chunk(L)
        else:
            data = self._synth_chunk(L, Fs)
        si0 = self._si
        self._si += L
        return (si0, data, self.measure_names)

class T7Device(QtCore.QObject):

    def __init__(self):
        super().__init__()
        self.handle = None
        self.plan = None
        self.running = False
        self._si = 0
        self.measure_names = []
        self._opened = False

    def _open(self):
        if not HAVE_LJM:
            raise RuntimeError('labjack.ljm not available in this environment.')
        self.handle = ljm.openS('T7', 'USB', 'ANY')
        ljm.eWriteName(self.handle, 'AIN_ALL_NEGATIVE_CH', 199)
        ljm.eWriteName(self.handle, 'AIN_ALL_RANGE', 10.0)
        ljm.eWriteName(self.handle, 'STREAM_RESOLUTION_INDEX', 0)
        ljm.eWriteName(self.handle, 'STREAM_SETTLING_US', 0)
        ljm.eWriteName(self.handle, 'STREAM_CLOCK_SOURCE', 0)
        self._opened = True

    def _close(self):
        if self._opened and self.handle is not None:
            try:
                ljm.close(self.handle)
            except Exception as e:
                try:
                    msg = getattr(e, 'args', [str(e)])[0]
                    if 'LJME_DEVICE_NOT_OPEN' not in str(msg):
                        print('[T7] close warning:', e)
                except Exception:
                    pass
            finally:
                self.handle = None
                self._opened = False

    def dispose(self):
        try:
            self.stop()
        except Exception:
            pass
        try:
            self._close()
        except Exception:
            pass

    def apply_plan(self, plan):
        if self.running:
            raise RuntimeError('Stop before re-applying plan.')
        if self.handle is None:
            self._open()
        self.plan = plan
        ext_mode = True
        if not ext_mode:
            for idx, ex in enumerate(self.plan.excitations):
                addr, _ = ljm.namesToAddresses(1, [ex.dac])
                ljm.periodicStreamOut(self.handle, idx, addr[0], plan.Fs, plan.L, ex.waveform.astype(np.float64))
            scan_names = [f'STREAM_OUT{i}' for i, _ in enumerate(plan.excitations)]
            scan_names.extend(plan.scan_ain)
            scan_names.extend(plan.scan_dio)
            self._scan_names = scan_names
            addrs, _ = ljm.namesToAddresses(len(scan_names), scan_names)
            self._scan_addresses = list(addrs)
            self.measure_names = [n for n in scan_names if not n.startswith('STREAM_OUT')]
            print('[T7] Applied plan (onboard DAC mode).')
            return
        print('[T7] Applying plan (external IC mode).')
        vpp_entries = []
        for ex in self.plan.excitations:
            if ex.ad9833:
                lsb, msb = _ad9833_words_for_freq(ex.f_adj, ex.ad9833.mclk_hz)
                _spi_tx_words(self.handle, ex.ad9833.cs_dio, ex.ad9833.clk_dio, ex.ad9833.miso_dio, ex.ad9833.mosi_dio, [8448], ex.ad9833.spi_mode, ex.ad9833.speed_throttle)
                _spi_tx_words(self.handle, ex.ad9833.cs_dio, ex.ad9833.clk_dio, ex.ad9833.miso_dio, ex.ad9833.mosi_dio, [lsb, msb], ex.ad9833.spi_mode, ex.ad9833.speed_throttle)
                _spi_tx_words(self.handle, ex.ad9833.cs_dio, ex.ad9833.clk_dio, ex.ad9833.miso_dio, ex.ad9833.mosi_dio, [8192], ex.ad9833.spi_mode, ex.ad9833.speed_throttle)
                print(f'[AD9833] {ex.name} -> {ex.f_adj:.2f} Hz (cs={ex.ad9833.cs_dio})')
            if ex.mcp4728 and ex.vbias_volts is not None:
                payload = _mcp4728_build_simple(ex.mcp4728.chan, ex.vbias_volts)
                _i2c_tx_bytes_rate_limited(self.handle, ex.mcp4728.addr7, ex.mcp4728.sda, ex.mcp4728.scl, ex.mcp4728.speed_throttle, payload)
                print(f'[MCP4728] {ex.name} {ex.mcp4728.chan} -> {ex.vbias_volts:.3f} V  bytes={payload}')
            if ex.vpp_shift and ex.vpp_volts is not None:
                code6 = int(np.clip(ex.vpp_volts, 0.0, 3.3) / 3.3 * (2 ** ex.vpp_shift.nbits - 1))
                vpp_entries.append((ex.name, ex.vpp_shift, code6))
        for ex in self.plan.excitations_disabled:
            if ex.ad9833:
                lsb, msb = _ad9833_words_for_freq(ex.f_adj, ex.ad9833.mclk_hz)
                _spi_tx_words(self.handle, ex.ad9833.cs_dio, ex.ad9833.clk_dio, ex.ad9833.miso_dio, ex.ad9833.mosi_dio, [8448], ex.ad9833.spi_mode, ex.ad9833.speed_throttle)
                _spi_tx_words(self.handle, ex.ad9833.cs_dio, ex.ad9833.clk_dio, ex.ad9833.miso_dio, ex.ad9833.mosi_dio, [lsb, msb], ex.ad9833.spi_mode, ex.ad9833.speed_throttle)
                _spi_tx_words(self.handle, ex.ad9833.cs_dio, ex.ad9833.clk_dio, ex.ad9833.miso_dio, ex.ad9833.mosi_dio, [8192], ex.ad9833.spi_mode, ex.ad9833.speed_throttle)
                print(f'[AD9833] {ex.name} -> {ex.f_adj:.2f} Hz (cs={ex.ad9833.cs_dio})')
            if ex.mcp4728 and ex.vbias_volts is not None:
                payload = _mcp4728_build_simple(ex.mcp4728.chan, ex.vbias_volts)
                _i2c_tx_bytes_rate_limited(self.handle, ex.mcp4728.addr7, ex.mcp4728.sda, ex.mcp4728.scl, ex.mcp4728.speed_throttle, payload)
                print(f'[MCP4728] {ex.name} {ex.mcp4728.chan} -> {ex.vbias_volts:.3f} V  bytes={payload}')
            if ex.vpp_shift and ex.vpp_volts is not None:
                code6 = int(np.clip(ex.vpp_volts, 0.0, 3.3) / 3.3 * (2 ** ex.vpp_shift.nbits - 1))
                vpp_entries.append((ex.name, ex.vpp_shift, code6))
        if vpp_entries:
            srclk = vpp_entries[0][1].srclk_dio
            rclk = vpp_entries[0][1].rclk_dio
            nbits = vpp_entries[0][1].nbits
            msb_first = vpp_entries[0][1].msb_first
            for _, cfg, _ in vpp_entries[1:]:
                if cfg.srclk_dio != srclk or cfg.rclk_dio != rclk:
                    raise ValueError('Vpp SRCLK/RCLK must match.')
            order = {'IE': 0, 'E1': 1, 'E2': 2}
            vpp_entries.sort(key=lambda t: order.get(t[0], 99))
            ser_pins = [cfg.ser_dio for _, cfg, _ in vpp_entries]
            codes = [cd for *_, cd in vpp_entries]
            _shift_vpp_parallel(self.handle, ser_pins, srclk, rclk, codes, nbits, msb_first)
            print('[VPP] parallel shift -> ' + ', '.join((f'{n}:{c:02d}' for n, _, c in vpp_entries)))
        scan_names = list(plan.scan_ain) + list(plan.scan_dio)
        self._scan_names = scan_names
        addrs, _ = ljm.namesToAddresses(len(scan_names), scan_names)
        self._scan_addresses = list(addrs)
        self.measure_names = list(scan_names)

    def apply_plan_settings(self, lut_path='vpp_lut.json', force_rebuild_lut=False, retrim_bias=True):
        if not self.plan:
            raise RuntimeError('No plan applied.')
        if any((ex.vpp_shift for ex in self.plan.excitations)):
            print(f"[Apply] Ensuring Vpp LUTs (path='{lut_path}', force={force_rebuild_lut})")
            self.ensure_vpp_luts(path=lut_path, force_rebuild=force_rebuild_lut)
        for ex in self.plan.excitations:
            target_vpp = ex.vpp_volts if ex.vpp_volts is not None else None
            target_bias = ex.vbias_volts if ex.vbias_volts is not None else 1.5
            if ex.vpp_shift and target_vpp is not None:
                self.set_vpp_target(ex.name, target_vpp, retrim_bias=retrim_bias, vbias_target=target_bias)
            elif ex.mcp4728:
                self.set_vbias_target(ex.name, target_vbias=target_bias)
        try:
            meas = self.measure_monitors_pp(duration_s=0.12)
            print('[Apply] Final monitor snapshot:', ', '.join((f"{k}: vbias={v['vbias']:.3f}V vpp={v['vpp']:.3f}V" for k, v in meas.items())))
        except Exception as e:
            print(f'[Apply] Measurement snapshot failed: {e}')

    def start(self):
        if not self.plan:
            raise RuntimeError('No plan applied.')
        if self.running:
            return
        scans_per_read = self.plan.L
        rate = ljm.eStreamStart(self.handle, scans_per_read, len(self._scan_addresses), self._scan_addresses, self.plan.Fs)
        if abs(rate - self.plan.Fs) > 0.001:
            print(f'Warning: stream rate mismatch {rate} vs {self.plan.Fs}')
        self.running = True
        self._si = 0

    def stop(self):
        if self.running and self.handle:
            try:
                ljm.eStreamStop(self.handle)
            except Exception:
                pass
        self.running = False

    def read_chunk(self):
        if not self.running:
            return None
        aData, dev_backlog, ljm_backlog = ljm.eStreamRead(self.handle)
        n_stream_out = len(self.plan.excitations)
        total_scans = self.plan.L
        trailing = n_stream_out * total_scans
        trailing = 0
        leading = aData[:len(aData) - trailing]
        arr = np.asarray(leading, dtype=np.float32)
        n_meas = len(self.measure_names)
        data = arr.reshape(total_scans, n_meas).T
        si0 = self._si
        self._si += total_scans
        return (si0, data, self.measure_names)

    def __del__(self):
        try:
            self.dispose()
        except Exception:
            pass

    def _exc_monitors(self):
        if not self.plan:
            return []
        return [(ex.name, ex.ain_monitor) for ex in self.plan.excitations if ex.ain_monitor]

    def measure_monitors_pp(self, duration_s=0.1, rate=None, settling_us=10, qtrim=0.005):
        mons = self._exc_monitors()
        if not mons:
            return {}
        names = [ain for _, ain in mons]
        addrs, _ = ljm.namesToAddresses(len(names), names)
        Fs = float(rate or (self.plan.Fs if self.plan else 12500.0))
        scans_per_read = max(256, int(min(4096, Fs * 0.02)))
        reads = max(1, int(round(duration_s / (scans_per_read / Fs))))
        prev_settle = ljm.eReadName(self.handle, 'STREAM_SETTLING_US')
        ljm.eWriteName(self.handle, 'STREAM_SETTLING_US', int(settling_us))
        try:
            ljm.eStreamStart(self.handle, scans_per_read, len(addrs), list(addrs), Fs)
            acc = {nm: [] for nm in names}
            for _ in range(reads):
                aData, _, _ = ljm.eStreamRead(self.handle)
                arr = np.asarray(aData, np.float32).reshape(scans_per_read, len(names))
                for j, nm in enumerate(names):
                    acc[nm].append(arr[:, j])
        finally:
            try:
                ljm.eStreamStop(self.handle)
            except:
                pass
            ljm.eWriteName(self.handle, 'STREAM_SETTLING_US', float(prev_settle))
        out = {}
        for nm, ain in mons:
            y = np.concatenate(acc[ain]) if acc[ain] else np.empty(0, np.float32)
            if y.size == 0:
                out[nm] = dict(vbias=float('nan'), vpp=float('nan'))
                continue
            vb = float(np.median(y))
            lo = float(np.quantile(y, qtrim))
            hi = float(np.quantile(y, 1 - qtrim))
            vpp = max(0.0, hi - lo)
            if vpp > 6.0:
                vpp = float(np.clip(vpp, 0.0, 5.2))
            out[nm] = dict(vbias=vb, vpp=vpp)
        return out

    def _parallel_shift_codes(self, codes_by_name):
        entries = []
        if not hasattr(self, '_current_vpp_codes'):
            self._current_vpp_codes = {}
        for ex in self.plan.excitations:
            if not ex.vpp_shift:
                continue
            cur = self._current_vpp_codes.get(ex.name, 0)
            entries.append((ex.name, ex.vpp_shift, int(codes_by_name.get(ex.name, cur))))
        if not entries:
            return
        srclk = entries[0][1].srclk_dio
        rclk = entries[0][1].rclk_dio
        nbits = entries[0][1].nbits
        msb_first = entries[0][1].msb_first
        for _, cfg, _ in entries[1:]:
            if cfg.srclk_dio != srclk or cfg.rclk_dio != rclk:
                raise ValueError('SRCLK/RCLK mismatch.')
            if cfg.nbits != nbits or cfg.msb_first != msb_first:
                raise ValueError('nbits/order mismatch.')
        order = {'IE': 0, 'E1': 1, 'E2': 2}
        entries.sort(key=lambda t: order.get(t[0], 99))
        ser = [cfg.ser_dio for _, cfg, _ in entries]
        codes = [c for *_, c in entries]
        _shift_vpp_parallel(self.handle, ser, srclk, rclk, codes, nbits, msb_first)
        for nm, _, cd in entries:
            self._current_vpp_codes[nm] = int(cd)
        print('[VPP] parallel set -> ' + ', '.join((f'{nm}:{cd:02d}' for nm, _, cd in entries)))

    def calibrate_vpp_luts(self, codes=tuple(range(64)), settle_s=0.015, measure_s=0.08, save_json=None):
        if not hasattr(self, '_current_vpp_codes'):
            self._current_vpp_codes = {}
        luts = {ex.name: [0.0] * 64 for ex in self.plan.excitations if ex.vpp_shift}
        for c in codes:
            self._parallel_shift_codes({nm: int(c) for nm in luts.keys()})
            time.sleep(settle_s)
            meas = self.measure_monitors_pp(duration_s=measure_s)
            for nm in luts.keys():
                luts[nm][c] = meas.get(nm, {}).get('vpp', float('nan'))
            print(f'[VPP LUT] code {c:02d} -> ' + ', '.join((f'{nm}:{luts[nm][c]:.3f}V' for nm in luts.keys())))
        if save_json:
            try:
                with open(save_json, 'w', encoding='utf-8') as f:
                    json.dump(dict(luts=luts, timestamp=time.time()), f, indent=2)
                print(f'[VPP LUT] saved {save_json}')
            except Exception as e:
                print(f'[VPP LUT] save failed: {e}')
        self._vpp_lut = luts
        return luts

    def load_vpp_luts(self, path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                obj = json.load(f)
        except Exception as e:
            print(f'[VPP LUT] load failed: {e}')
            return False
        luts = obj.get('luts') if isinstance(obj, dict) else obj
        if not isinstance(luts, dict):
            print('[VPP LUT] invalid file')
            return False
        want = [ex.name for ex in self.plan.excitations if ex.vpp_shift]
        for nm in list(luts.keys()):
            if nm not in want:
                print(f"[VPP LUT] dropping extra channel in file: '{nm}' (not in current plan)")
                luts.pop(nm, None)
        missing = [nm for nm in want if nm not in luts]
        if missing:
            print(f'[VPP LUT] missing required channels in file: {missing} -> rebuild needed')
            return False
        for nm in want:
            arr = luts.get(nm)
            if not isinstance(arr, list) or len(arr) != 64:
                print(f"[VPP LUT] channel '{nm}' has invalid table (need 64 values) -> rebuild")
                return False
        self._vpp_lut = {nm: list(map(float, luts[nm])) for nm in want}
        if not hasattr(self, '_current_vpp_codes'):
            self._current_vpp_codes = {nm: 0 for nm in want}
        print(f'[VPP LUT] loaded from {path} (channels: {want})')
        return True

    def ensure_vpp_luts(self, path='vpp_lut.json', force_rebuild=False, settle_s=0.015, measure_s=0.08):
        if not force_rebuild and os.path.isfile(path) and self.load_vpp_luts(path):
            return self._vpp_lut
        return self.calibrate_vpp_luts(settle_s=settle_s, measure_s=measure_s, save_json=path)

    def set_vbias_target(self, name, target_vbias=1.5, tol=0.01, max_iter=3, settle_s=0.3, measure_s=0.1):
        ex = next((e for e in self.plan.excitations if e.name == name), None)
        if not ex or not ex.mcp4728:
            raise ValueError(f'{name} not bound to MCP4728')
        if not hasattr(self, '_vbias_cmd'):
            self._vbias_cmd = {}
        v_cmd = float(self._vbias_cmd.get(name, target_vbias))
        vb = float('nan')
        for i in range(max_iter):
            payload = _mcp4728_build_simple(ex.mcp4728.chan, v_cmd)
            _i2c_tx_bytes_rate_limited(self.handle, ex.mcp4728.addr7, ex.mcp4728.sda, ex.mcp4728.scl, ex.mcp4728.speed_throttle, payload)
            self._vbias_cmd[name] = v_cmd
            time.sleep(settle_s)
            meas = self.measure_monitors_pp(duration_s=measure_s)
            vb = float(meas.get(name, {}).get('vbias', float('nan')))
            if not np.isfinite(vb):
                print(f'[VBias] {name}: measurement failed')
                break
            err = target_vbias - vb
            print(f'[VBias] {name} iter {i + 1}: cmd={v_cmd:.3f}V  meas={vb:.3f}V  err={err * 1000.0:.0f} mV')
            if abs(err) <= tol:
                return vb
            v_cmd = float(np.clip(v_cmd + err, 0.0, 4.95))
        return vb

    def set_vpp_target(self, name, target_vpp, clamp=True, retrim_bias=True, vbias_target=1.5, bias_tol=0.01, bias_settle_s=0.3, bias_measure_s=0.1):
        if not hasattr(self, '_vpp_lut') or name not in self._vpp_lut:
            raise RuntimeError('Vpp LUT not available; run ensure_vpp_luts()')
        lut = np.asarray(self._vpp_lut[name], float)
        if clamp:
            target_vpp = float(np.clip(target_vpp, np.nanmin(lut), np.nanmax(lut)))
        idx = int(np.nanargmin(np.abs(lut - target_vpp)))
        if not hasattr(self, '_current_vpp_codes'):
            self._current_vpp_codes = {}
        new_codes = dict(self._current_vpp_codes)
        new_codes[name] = idx
        self._parallel_shift_codes(new_codes)
        print(f'[VPP] {name}: target {target_vpp:.3f}V -> code {idx} (meas≈{lut[idx]:.3f}V)')
        if retrim_bias:
            self.set_vbias_target(name, vbias_target, bias_tol, 3, bias_settle_s, bias_measure_s)

    def sample_monitors_raw(self, window_s=0.003, settling_us=10):
        mons = self._exc_monitors()
        if not mons:
            return (np.empty(0), [], np.empty((0, 0), dtype=np.float32))
        names = [ain for _, ain in mons]
        addrs, _ = ljm.namesToAddresses(len(names), names)
        Fs = float(self.plan.Fs if self.plan else 12500.0)
        scans = max(32, int(round(window_s * Fs)))
        prev_settle = ljm.eReadName(self.handle, 'STREAM_SETTLING_US')
        ljm.eWriteName(self.handle, 'STREAM_SETTLING_US', int(settling_us))
        try:
            ljm.eStreamStart(self.handle, scans, len(addrs), list(addrs), Fs)
            aData, _, _ = ljm.eStreamRead(self.handle)
        finally:
            try:
                ljm.eStreamStop(self.handle)
            except Exception:
                pass
            ljm.eWriteName(self.handle, 'STREAM_SETTLING_US', float(prev_settle))
        arr = np.asarray(aData, dtype=np.float32).reshape(scans, len(names)).T
        t = np.arange(scans, dtype=np.float64) / Fs
        return (t, names, arr)
