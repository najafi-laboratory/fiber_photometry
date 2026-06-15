import numpy as np

class ChannelSpec:
    def __init__(self, name, role, enabled=True, target_freq=None, vpp=0.4, vbias=3.0, von=None, ain=None, dac=None, dio=None, ad9833=None, mcp4728=None, vpp_shift=None):
        self.__dict__.update(locals())
        del self.__dict__['self']

class AD9833Cfg:
    def __init__(self, cs_dio, clk_dio='DIO3', miso_dio='DIO18', mosi_dio='DIO4', spi_mode=2, speed_throttle=0, mclk_hz=25000000.0):
        self.__dict__.update(locals())
        del self.__dict__['self']

class MCP4728Cfg:
    def __init__(self, chan, sda=2, scl=1, addr7=96, speed_throttle=65516):
        self.__dict__.update(locals())
        del self.__dict__['self']

class ShiftVppCfg:
    def __init__(self, ser_dio, srclk_dio='DIO11', rclk_dio='DIO12', nbits=6, msb_first=False):
        self.__dict__.update(locals())
        del self.__dict__['self']

class ExcitationPlan:
    def __init__(self, **kw):
        self.__dict__.update(kw)

class DevicePlan:
    def __init__(self, **kw):
        self.__dict__.update(kw)

def choose_L_and_k(Fs, targets, max_L=4096):
    ft = np.asarray(targets, float)
    candidates = [512, 768, 1000, 1024, 1600, 2000, 2048, 3072, 4096]
    candidates = [L for L in candidates if L <= max_L]
    best = None
    for L in candidates:
        df = Fs / L
        k = np.rint(ft / df).astype(int)
        k[k < 1] = 1
        fa = k * df
        err = float(np.max(np.abs(fa - ft)))
        score = err + 0.25 * (L / max_L)
        if best is None or score < best[0]:
            best = (score, L, k, fa, err)
    _, L, k_final, fa_final, err = best
    return (L, k_final, fa_final)

def build_plan(specs, Fs_requested, max_L=4096):
    excit = [c for c in specs if c.enabled and c.role == 'EXCITATION']
    excit_disabled = [c for c in specs if not c.enabled and c.role == 'EXCITATION']
    for c in excit_disabled:
        c.vbias = 0.0
        c.vpp = 0.0
    scan_ain = list(dict.fromkeys([c.ain for c in excit if c.ain] + [c.ain for c in specs if c.enabled and c.role == 'PHOTODIODE' and c.ain]))
    scan_dio = [c.dio for c in specs if c.enabled and c.role == 'DIGITAL_IN' and c.dio]
    chan_count = len(scan_ain) + len(scan_dio)
    if chan_count == 0:
        raise ValueError('Zero channels selected.')
    Fs = min(Fs_requested, 100000.0 / chan_count)
    targets = [e.target_freq for e in excit]
    for e in excit:
        if e.target_freq is None or e.target_freq <= 0:
            raise ValueError(f'Excitation {e.name} missing positive target_freq.')
    L, k_array, f_adj = choose_L_and_k(Fs, targets, max_L=max_L) if targets else (4096, np.array([], dtype=int), np.array([], dtype=float))
    t = np.arange(L) / L

    def make_excitation(e, k=0, fa=0.0):
        wave = (e.vbias + 0.5 * e.vpp * np.sin(2 * np.pi * int(k) * t)).astype(np.float32)
        return ExcitationPlan(name=e.name, dac=e.dac or 'DAC0', ain_monitor=e.ain, k=int(k), f_adj=float(fa), waveform=wave, ad9833=e.ad9833, mcp4728=e.mcp4728, vpp_shift=e.vpp_shift, vbias_volts=e.vbias, vpp_volts=e.vpp)
    return DevicePlan(mode='FDM', Fs=Fs, L=L, excitations=tuple((make_excitation(e, k, fa) for e, k, fa in zip(excit, k_array, f_adj))), excitations_disabled=tuple((make_excitation(e) for e in excit_disabled)), scan_ain=tuple(scan_ain), scan_dio=tuple(scan_dio), k_array=k_array.astype(int), adjusted_freqs=f_adj.astype(float))
