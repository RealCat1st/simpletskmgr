#!/usr/bin/env python3
"""
SimpleTskMgr – modern terminal task manager for Arch Linux & Debian/Ubuntu.
No dependencies besides Python 3 (reads directly from /proc and /sys).

Keys:
  q        Quit
  c / m    Sort by CPU / memory
  p / n    Sort by PID / name
  r        Reverse sort order
  ↑ ↓      Select process      PgUp/PgDn  page
  k        Terminate selected process/group (SIGTERM),  K = SIGKILL – asks first
  + / -    Change refresh interval
  /        Search processes (Enter = keep filter, Esc = clear)
  g        Group processes by app (→/← or Space to expand/collapse)
  Enter    Process details (or expand group)
  Mouse    Click to select, double-click for details, click headers to sort,
           wheel to scroll, click footer keys
  u        Install available update

Set STM_NO_UPDATE_CHECK=1 to disable the update check on start.
"""
import curses
import glob
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request

VERSION = "1.4.0"
REPO = "RealCat1st/simpletskmgr"
RAW_URL = f"https://raw.githubusercontent.com/{REPO}/main/simpletskmgr.py"
IN_SNAP = bool(os.environ.get("SNAP"))  # snaps update themselves via "snap refresh"
HOSTFS = "/var/lib/snapd/hostfs" if IN_SNAP and os.path.isdir("/var/lib/snapd/hostfs") else ""

CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")


def read(path, default=""):
    try:
        with open(path) as f:
            return f.read()
    except Exception:
        return default


def human(n, suffix="B"):
    for unit in ["", "K", "M", "G", "T", "P"]:
        if abs(n) < 1024:
            return f"{n:5.1f}{unit}{suffix}" if unit else f"{n:5.0f}{suffix}"
        n /= 1024
    return f"{n:.1f}E{suffix}"


# ───────────────────────── Data sources ─────────────────────────
class CPU:
    def __init__(self):
        self.prev = self._read()
        self.usage = [0.0] * len(self.prev)
        self.model = "CPU"
        for line in read("/proc/cpuinfo").splitlines():
            if line.startswith("model name"):
                self.model = line.split(":", 1)[1].strip()
                break

    @staticmethod
    def _read():
        out = []
        for line in read("/proc/stat").splitlines():
            if line.startswith("cpu"):
                v = list(map(int, line.split()[1:]))
                idle = v[3] + (v[4] if len(v) > 4 else 0)
                out.append((sum(v), idle))
        return out

    def update(self):
        cur = self._read()
        self.usage = []
        for (t1, i1), (t2, i2) in zip(self.prev, cur):
            dt = t2 - t1
            self.usage.append(0.0 if dt <= 0 else 100.0 * (1 - (i2 - i1) / dt))
        self.prev = cur
        self.total_jiffies = cur[0][0]

    @staticmethod
    def freq():
        fs = [int(read(p, "0") or 0) for p in
              glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq")]
        return (sum(fs) / len(fs) / 1000) if fs else None

    @staticmethod
    def temp():
        for hw in glob.glob("/sys/class/hwmon/hwmon*"):
            if read(hw + "/name").strip() in ("coretemp", "k10temp", "zenpower", "cpu_thermal"):
                t = read(hw + "/temp1_input").strip()
                if t:
                    return int(t) / 1000
        t = read("/sys/class/thermal/thermal_zone0/temp").strip()
        return int(t) / 1000 if t else None


def meminfo():
    m = {}
    for line in read("/proc/meminfo").splitlines():
        k, v = line.split(":", 1)
        m[k] = int(v.split()[0]) * 1024
    total = m.get("MemTotal", 1)
    avail = m.get("MemAvailable", m.get("MemFree", 0))
    return {
        "total": total, "used": total - avail, "cached": m.get("Cached", 0),
        "swap_total": m.get("SwapTotal", 0),
        "swap_used": m.get("SwapTotal", 0) - m.get("SwapFree", 0),
    }


INTEL_NAMES = {  # fallback if lspci is not installed
    "0xe20b": "Intel Arc B580", "0xe20c": "Intel Arc B570",
    "0x56a0": "Intel Arc A770", "0x56a1": "Intel Arc A750", "0x56a5": "Intel Arc A380",
    "0x56a6": "Intel Arc A310",
}
UNITS = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3}


def to_bytes(v):
    parts = v.split()
    try:
        return int(parts[0]) * UNITS.get(parts[1] if len(parts) > 1 else "B", 1)
    except (ValueError, IndexError):
        return 0


def parse_fdinfo(text):
    """Parse a DRM fdinfo block (i915, xe, amdgpu, nouveau, msm, ...).
    Returns (client_key, engines{name: (busy, total|None)}, capacity{name: n}, vram_bytes) or None."""
    d = {}
    for line in text.splitlines():
        k, _, v = line.partition(":")
        d[k.strip()] = v.strip()
    if "drm-client-id" not in d:
        return None
    key = (d.get("drm-pdev", ""), d["drm-client-id"])
    cap, eng, vram = {}, {}, 0
    for k, v in d.items():
        if k.startswith("drm-engine-capacity-"):
            try:
                cap[k[20:]] = max(1, int(v.split()[0]))
            except ValueError:
                pass
    for k, v in d.items():
        try:
            if k.startswith("drm-engine-") and not k.startswith("drm-engine-capacity-"):
                eng[k[11:]] = (int(v.split()[0]), None)  # busy time in ns (i915, amdgpu)
            elif k.startswith("drm-cycles-"):  # xe: busy cycles vs. total cycles
                tot = d.get("drm-total-cycles-" + k[11:])
                if tot:
                    eng[k[11:]] = (int(v.split()[0]), int(tot.split()[0]))
            elif k.startswith(("drm-resident-vram", "drm-resident-local", "drm-memory-vram")):
                vram += to_bytes(v)
        except ValueError:
            pass
    return key, eng, cap, vram


class GpuClients:
    """GPU usage per process and per device from /proc/<pid>/fdinfo.
    Works for Intel (i915 + xe, e.g. Arc B580), AMD and others. Only processes
    we may inspect are counted (all of them when running as root)."""

    def __init__(self):
        self.prev = {}
        self.per_pid = {}
        self.per_dev = {}

    def update(self):
        now = time.monotonic()
        cur = {}
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            fddir = f"/proc/{pid}/fd"
            try:
                fds = os.listdir(fddir)
            except OSError:
                continue
            for fd in fds:
                try:
                    if not os.readlink(f"{fddir}/{fd}").startswith("/dev/dri/"):
                        continue
                except OSError:
                    continue
                r = parse_fdinfo(read(f"/proc/{pid}/fdinfo/{fd}"))
                if r and r[0] not in cur:
                    cur[r[0]] = (int(pid), now) + r[1:]
        per_pid, dev_eng, dev_vram = {}, {}, {}
        for key, (pid, t, eng, cap, vram) in cur.items():
            pdev = key[0]
            dev_vram[pdev] = dev_vram.get(pdev, 0) + vram
            dev_eng.setdefault(pdev, {})
            prev = self.prev.get(key)
            if not prev:
                continue
            _, pt, peng, _, _ = prev
            best = 0.0
            for e, (busy, tot) in eng.items():
                if e not in peng:
                    continue
                pb, ptot = peng[e]
                c = cap.get(e, 1)
                if tot is not None and ptot is not None:
                    span = tot - ptot
                else:
                    span = (t - pt) * 1e9
                u = (busy - pb) / span * 100 / c if span > 0 else 0.0
                u = max(0.0, min(100.0, u))
                dev_eng[pdev][e] = dev_eng[pdev].get(e, 0.0) + u
                best = max(best, u)
            per_pid[pid] = per_pid.get(pid, 0.0) + best
        self.prev = cur
        self.per_pid = per_pid
        self.per_dev = {p: {"util": min(100.0, max(e.values())) if e else 0.0, "vram": dev_vram.get(p, 0)}
                        for p, e in dev_eng.items()}


class GPU:
    """NVIDIA (nvidia-smi), AMD (amdgpu sysfs), Intel i915/xe (fdinfo + sysfs)."""

    def __init__(self):
        self.has_nvsmi = shutil.which("nvidia-smi") is not None
        self.last_nv = []
        self.last_nv_t = 0
        self.names = {}
        self.energy = {}
        self.clients = GpuClients()

    def _name(self, card, dev, driver):
        pdev = os.path.basename(os.path.realpath(dev))
        if pdev in self.names:
            return self.names[pdev]
        name = None
        if shutil.which("lspci"):
            try:
                out = subprocess.run(["lspci", "-mm", "-s", pdev], capture_output=True,
                                     text=True, timeout=2).stdout
                f = re.findall(r'"([^"]*)"', out)
                if len(f) >= 3:
                    vendor = f[1].split()[0] if f[1] else ""
                    m = re.search(r"\[([^\]]+)\]", f[2])  # "Battlemage G21 [Arc B580]" -> "Arc B580"
                    name = f"{vendor} {m.group(1) if m else f[2]}".strip()
            except Exception:
                pass
        if not name:
            name = INTEL_NAMES.get(read(dev + "/device").strip(), f"{os.path.basename(card)} ({driver})")
        self.names[pdev] = name
        return name

    def _power(self, dev, key):
        for hw in glob.glob(dev + "/hwmon/hwmon*"):
            for f in ("power1_average", "power1_input"):
                v = read(f"{hw}/{f}").strip()
                if v and int(v) > 0:
                    return int(v) / 1e6
            v = read(hw + "/energy1_input").strip()  # µJ counter (Intel xe / i915)
            if v:
                e, t = int(v), time.monotonic()
                prev = self.energy.get(key)
                self.energy[key] = (e, t)
                if prev and t > prev[1]:
                    return max(0.0, (e - prev[0]) / 1e6 / (t - prev[1]))
        return None

    def info(self):
        self.clients.update()
        gpus = []
        if self.has_nvsmi:
            if time.time() - self.last_nv_t > 1.5:
                try:
                    out = subprocess.run(
                        ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
                         "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=2).stdout
                    self.last_nv = []
                    for line in out.strip().splitlines():
                        n, u, mu, mt, t, pw = ([x.strip() for x in line.split(",")] + ["", ""])[:6]
                        self.last_nv.append({"name": n, "util": float(u), "mem_used": float(mu) * 2**20,
                                             "mem_total": float(mt) * 2**20, "temp": float(t),
                                             "power": float(pw) if pw.replace(".", "").isdigit() else None})
                except Exception:
                    pass
                self.last_nv_t = time.time()
            gpus += self.last_nv
        for card in sorted(glob.glob("/sys/class/drm/card[0-9]*")):
            if "-" in os.path.basename(card):
                continue  # connectors like card1-HDMI-A-1
            dev = card + "/device"
            driver = os.path.basename(os.path.realpath(dev + "/driver")) if os.path.exists(dev + "/driver") else "?"
            if driver == "nvidia" and self.has_nvsmi:
                continue
            pdev = os.path.basename(os.path.realpath(dev))
            g = {"name": self._name(card, dev, driver), "util": None, "mem_used": None,
                 "mem_total": None, "temp": None, "power": None, "driver": driver}
            busy = read(dev + "/gpu_busy_percent").strip()
            fd = self.clients.per_dev.get(pdev)
            if busy:
                g["util"] = float(busy)
            elif driver in ("xe", "i915"):
                g["util"] = fd["util"] if fd else 0.0
            mu, mt = read(dev + "/mem_info_vram_used").strip(), read(dev + "/mem_info_vram_total").strip()
            if mu and mt:
                g["mem_used"], g["mem_total"] = int(mu), int(mt)
            elif driver in ("xe", "i915"):
                total = 0
                for f in glob.glob(dev + "/tile*/physical_vram_size_bytes") + glob.glob(dev + "/lmem_total_bytes"):
                    try:
                        total += int(read(f, "0").strip() or 0)
                    except ValueError:
                        pass
                if total:
                    g["mem_total"] = total
                    g["mem_used"] = fd["vram"] if fd else 0
            temps = []
            for t in sorted(glob.glob(dev + "/hwmon/hwmon*/temp*_input")):
                try:
                    temps.append(int(read(t, "0").strip() or 0) / 1000)
                except ValueError:
                    pass
            temps = [t for t in temps if 0 < t < 150]
            if temps:
                g["temp"] = max(temps)
            g["power"] = self._power(dev, pdev)
            gpus.append(g)
        return gpus


class NPU:
    """Intel NPU (intel_vpu) / AMD XDNA (amdxdna) via /sys/class/accel."""

    def __init__(self):
        self.prev = {}

    def info(self):
        res = []
        now = time.time()
        for acc in sorted(glob.glob("/sys/class/accel/accel*")):
            dev = acc + "/device"
            driver = os.path.basename(os.path.realpath(dev + "/driver")) if os.path.exists(dev + "/driver") else "?"
            n = {"name": f"{os.path.basename(acc)} ({driver})", "util": None}
            busy = read(dev + "/npu_busy_time_us").strip()
            if busy:
                b = int(busy)
                if acc in self.prev:
                    pb, pt = self.prev[acc]
                    n["util"] = min(100.0, max(0.0, (b - pb) / ((now - pt) * 1e6) * 100))
                self.prev[acc] = (b, now)
            res.append(n)
        return res


class Disk:
    SKIP = ("tmpfs", "devtmpfs", "proc", "sysfs", "cgroup", "cgroup2", "overlay", "squashfs",
            "efivarfs", "securityfs", "debugfs", "tracefs", "pstore", "bpf", "configfs",
            "fusectl", "mqueue", "hugetlbfs", "autofs", "devpts", "ramfs", "binfmt_misc", "nsfs",
            "fuse.portal", "fuse.gvfsd-fuse", "rpc_pipefs", "selinuxfs")

    def __init__(self):
        self.prev = self._io()
        self.prev_t = time.time()
        self.rate = (0, 0)

    @staticmethod
    def _io():
        r = w = 0
        for line in read("/proc/diskstats").splitlines():
            p = line.split()
            if len(p) < 10:
                continue
            name = p[2]
            if name.startswith(("loop", "ram", "dm-", "zram")):
                continue
            if os.path.exists(f"/sys/block/{name}"):  # whole devices only
                r += int(p[5]) * 512
                w += int(p[9]) * 512
        return r, w

    def update(self):
        cur, now = self._io(), time.time()
        dt = now - self.prev_t or 1
        self.rate = ((cur[0] - self.prev[0]) / dt, (cur[1] - self.prev[1]) / dt)
        self.prev, self.prev_t = cur, now

    def mounts(self):
        seen, out = set(), []
        # inside a snap our own mount table is the sandbox -> use the host's (pid 1)
        table = (read("/proc/1/mounts") if IN_SNAP else "") or read("/proc/mounts")
        for line in table.splitlines():
            dev, mnt, fs = line.split()[:3]
            mnt = mnt.replace("\\040", " ")
            if fs in self.SKIP or dev in seen or mnt.startswith(("/snap", "/var/lib/docker", "/var/lib/snapd")):
                continue
            st = None
            for path in ([HOSTFS + mnt] if HOSTFS else []) + [mnt]:
                try:
                    st = os.statvfs(path)
                    break
                except Exception:
                    pass
            if st is None:
                continue
            total = st.f_blocks * st.f_frsize
            if total == 0:
                continue
            seen.add(dev)
            out.append((mnt, total, (st.f_blocks - st.f_bfree) * st.f_frsize, fs))
        return out


class Net:
    def __init__(self):
        self.prev, self.prev_t = self._read(), time.time()
        self.rate = (0, 0)

    @staticmethod
    def _read():
        rx = tx = 0
        for line in read("/proc/net/dev").splitlines()[2:]:
            name, data = line.split(":", 1)
            if name.strip() == "lo":
                continue
            d = data.split()
            rx += int(d[0]); tx += int(d[8])
        return rx, tx

    def update(self):
        cur, now = self._read(), time.time()
        dt = now - self.prev_t or 1
        self.rate = ((cur[0] - self.prev[0]) / dt, (cur[1] - self.prev[1]) / dt)
        self.prev, self.prev_t = cur, now


INTERPRETERS = re.compile(
    r"^(python[\d.]*|pypy[\d.]*|node|nodejs|deno|bun|java|perl[\d.]*|ruby[\d.]*|php[\d.]*|"
    r"lua[\d.]*|luajit|sh|bash|dash|zsh|fish|ksh|mono|dotnet|electron\d*|gjs|wine(64)?(-preloader)?|"
    r"box64|box86|qemu-[\w-]+|ld-linux[\w.-]*\.so[\d.]*)$")
VALUE_OPTS = {"-m", "-c", "-cp", "-classpath", "-jar", "--rcfile", "--init-file"}


def pretty_app_id(app_id):
    """org.vinegarhq.Sober -> Sober, com.discordapp.Discord -> Discord"""
    parts = [x for x in app_id.split(".") if x]
    if not parts:
        return app_id
    last = parts[-1]
    if last.lower() in ("desktop", "app", "client", "stable", "beta") and len(parts) > 1:
        last = parts[-2]
    return last[:1].upper() + last[1:]


def basename_any(path):
    """basename that also understands Windows paths (wine)"""
    return re.split(r"[\\/]", path.rstrip("/\\"))[-1] or path


def friendly_name(pid, comm, argv):
    if not argv:
        return f"[{comm}]"  # kernel thread
    a0 = argv[0]
    if " " in a0 and len(argv) == 1:  # process rewrote its title, e.g. "sshd-session: user@pts/0"
        a0 = a0.split()[0].rstrip(":")
    base = basename_any(a0)
    m = INTERPRETERS.match(base)
    if m and len(argv) > 1:
        skip = False
        for arg in argv[1:]:
            if skip:
                if base.startswith("python") and arg and not arg.startswith("-"):
                    return f"{base} -m {arg}"
                skip = False
                continue
            if arg in VALUE_OPTS:
                skip = arg not in ("-jar",)
                continue
            if arg.startswith("-"):
                continue
            cand = basename_any(arg)
            if cand:
                base = re.sub(r"\.(py|js|mjs|cjs|pl|rb|php|lua|sh|jar)$", "", cand)
            break
    if base == comm[:15] or not base:
        base = base or comm
    app = None
    info = read(f"/proc/{pid}/root/.flatpak-info")
    if info:
        for line in info.splitlines():
            if line.startswith("name="):
                app = pretty_app_id(line[5:].strip())
                break
    if app and app.lower() not in base.lower():
        return f"{app} › {base}"
    return app if app and app.lower() == base.lower() else base


class Procs:
    def __init__(self):
        self.names = {}
        self.home = os.path.expanduser("~")
        self.prev = {}
        self.prev_total = None
        self.users = {}
        for line in read("/etc/passwd").splitlines():
            p = line.split(":")
            if len(p) > 2:
                self.users[int(p[2])] = p[0]

    def update(self, total_jiffies, ncpu, memtotal):
        procs, cur = [], {}
        dt = (total_jiffies - self.prev_total) if self.prev_total else 0
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            stat = read(f"/proc/{pid}/stat")
            if not stat:
                continue
            try:
                name = stat[stat.index("(") + 1: stat.rindex(")")]
                f = stat[stat.rindex(")") + 2:].split()
                state, utime, stime, threads, rss = f[0], int(f[11]), int(f[12]), int(f[17]), int(f[21]) * PAGE
                start = f[19]
                uid = os.stat(f"/proc/{pid}").st_uid
            except Exception:
                continue
            t = utime + stime
            cur[pid] = t
            cpu = 0.0
            if dt and pid in self.prev:
                # share of total CPU × cores -> like "top" (100% = 1 core)
                cpu = (t - self.prev[pid]) / dt * 100 * ncpu
            raw = read(f"/proc/{pid}/cmdline")
            argv = [a for a in raw.split("\0") if a] if raw else []
            key = (pid, start)
            if key not in self.names:
                self.names[key] = friendly_name(pid, name, argv)
            cmd = " ".join(argv) or f"[{name}]"
            if self.home != "/":
                cmd = cmd.replace(self.home, "~")
            procs.append({"pid": int(pid), "name": self.names[key], "user": self.users.get(uid, str(uid)),
                          "state": state, "cpu": cpu, "mem": rss, "memp": rss / memtotal * 100,
                          "threads": threads, "cmd": cmd})
        self.prev, self.prev_total = cur, total_jiffies
        alive = {(p, cur_start) for (p, cur_start) in self.names if p in cur}
        self.names = {k: v for k, v in self.names.items() if k in alive}
        return procs


# ───────────────────────── UI ─────────────────────────
# xterm-256 colour codes
GRAD = [48, 49, 50, 86, 122, 156, 190, 226, 220, 214, 208, 202, 197, 196]  # low → high load
SPARK = [39, 45, 51, 87, 123, 159, 195, 231]
TRACK = 237
DIM = 244
TEXT = 252
BRIGHT = 231
ACCENT = {"cpu": 45, "gpu": 207, "ram": 120, "sys": 141, "disk": 214, "proc": 111, "cores": 81}
TITLE_BG, HEADER_BG, SEL_BG, ZEBRA_BG = 55, 24, 31, 235
MONO_BG = {TITLE_BG: 238, HEADER_BG: 238, SEL_BG: 244, 81: 250, 120: 250, 161: 240}

BASIC8 = [(0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0),
          (0, 0, 238), (205, 0, 205), (0, 205, 205), (229, 229, 229)]


def x256_rgb(c):
    if c < 16:
        return BASIC8[c % 8]
    if c >= 232:
        v = 8 + (c - 232) * 10
        return (v, v, v)
    c -= 16
    lv = [0, 95, 135, 175, 215, 255]
    return (lv[c // 36], lv[c // 6 % 6], lv[c % 6])


class App:
    def __init__(self, scr):
        self.scr = scr
        self.cpu, self.gpu, self.npu = CPU(), GPU(), NPU()
        self.disk, self.net, self.procs = Disk(), Net(), Procs()
        self.sort, self.rev = "cpu", True
        self.sel, self.off = 0, 0
        self.interval = 1.0
        self.msg, self.msg_t = "", 0
        self.plist, self.history, self.all = [], [], []
        self.query, self.searching = "", False
        self.grouped, self.expanded, self.rows = False, set(), []
        self.detail, self.confirm = None, None
        self.hits, self.last_click, self.page, self.follow_top = [], (-1, 0.0), 10, True
        self.new_version = None
        self.restart = False
        if not os.environ.get("STM_NO_UPDATE_CHECK") and not IN_SNAP:
            threading.Thread(target=self.check_update, daemon=True).start()
        self.distro = "Linux"
        for line in read("/etc/os-release").splitlines():
            if line.startswith("PRETTY_NAME="):
                self.distro = line.split("=", 1)[1].strip('"')
        curses.curs_set(0)
        curses.start_color()
        curses.use_default_colors()
        self.rich = curses.COLORS >= 256
        self.pairs = {}
        scr.nodelay(True)
        scr.idlok(False)  # no line-scroll optimisation -> no ghost lines
        scr.keypad(True)
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS)
            curses.mouseinterval(0)
        except curses.error:
            pass

    # ── updates ──
    @staticmethod
    def _vt(v):
        return tuple(int(x) for x in re.findall(r"\d+", v)[:3])

    def check_update(self):
        try:
            with urllib.request.urlopen(RAW_URL, timeout=4) as r:
                head = r.read(4096).decode("utf-8", "replace")
            m = re.search(r'^VERSION = "([^"]+)"', head, re.M)
            if m and self._vt(m.group(1)) > self._vt(VERSION):
                self.new_version = m.group(1)
        except Exception:
            pass  # offline etc. – just skip

    def install_update(self):
        if IN_SNAP:
            self.msg, self.msg_t = " Snap updates automatically (sudo snap refresh simpletskmgr) ", time.time()
            return
        if not self.new_version:
            self.msg, self.msg_t = " You are on the latest version ", time.time()
            return
        path = os.path.realpath(__file__)
        if not os.access(path, os.W_OK) or not os.access(os.path.dirname(path), os.W_OK):
            self.msg, self.msg_t = " No write access – rerun the installer with sudo ", time.time()
            return
        self.msg, self.msg_t = " Downloading update... ", time.time()
        self.draw()
        try:
            with urllib.request.urlopen(RAW_URL, timeout=15) as r:
                code = r.read()
            compile(code, "simpletskmgr.py", "exec")  # sanity check
            tmp = path + ".new"
            with open(tmp, "wb") as f:
                f.write(code)
            os.chmod(tmp, os.stat(path).st_mode)
            os.replace(tmp, path)
        except Exception as e:
            self.msg, self.msg_t = f" Update failed: {e} "[:60], time.time()
            return
        self.restart = True

    # ── colour helpers ──
    def _map(self, c):
        if c < 0 or self.rich:
            return c
        if 232 <= c <= 250:
            return -1 if c < 240 else 7
        r, g, b = x256_rgb(c)
        return min(range(8), key=lambda i: sum((a - b2) ** 2 for a, b2 in zip(BASIC8[i], (r, g, b))))

    def col(self, fg, bg=-1, attr=0, keep=False):
        if not keep:  # monochrome UI: only the ring gauges keep colour
            fg = fg if fg in (-1, 16) or fg >= 232 else BRIGHT
            bg = bg if bg == -1 or bg >= 232 else MONO_BG.get(bg, 238)
        if not self.rich and bg == ZEBRA_BG:
            bg = -1
        key = (fg, bg)
        if key not in self.pairs:
            n = len(self.pairs) + 1
            if n >= curses.COLOR_PAIRS:
                return attr
            try:
                curses.init_pair(n, self._map(fg), self._map(bg))
            except curses.error:
                return attr
            self.pairs[key] = curses.color_pair(n)
        return self.pairs[key] | attr

    @staticmethod
    def grad(p):
        p = max(0.0, min(100.0, p))
        return GRAD[int(p / 100 * (len(GRAD) - 1) + 0.5)]

    def put(self, y, x, s, attr=0):
        h, w = self.scr.getmaxyx()
        if 0 <= y < h and 0 <= x < w:
            try:
                self.scr.addnstr(y, x, s, w - x - (1 if y == h - 1 else 0), attr)
            except curses.error:
                pass

    # ── widgets ──
    def box(self, y, x, h, w, title, accent, right=""):
        a = self.col(accent)
        self.put(y, x, "╭" + "─" * (w - 2) + "╮", a)
        for i in range(1, h - 1):
            self.put(y + i, x, "│", a)
            self.put(y + i, x + w - 1, "│", a)
        self.put(y + h - 1, x, "╰" + "─" * (w - 2) + "╯", a)
        self.put(y, x + 2, f" {title} ", self.col(accent, attr=curses.A_BOLD))
        if right:
            self.put(y, x + w - len(right) - 3, f" {right} ", self.col(DIM))

    def bar(self, y, x, width, pct, label, extra="", lcol=ACCENT["cores"]):
        """Gradient bar: label ━━━━━━━━━━  42.0%  extra"""
        pct = max(0.0, min(100.0, pct))
        lw = 6
        self.put(y, x, f"{label:<{lw}}"[:lw], self.col(lcol, attr=curses.A_BOLD))
        bw = max(4, width - lw - 8 - (len(extra) + 1 if extra else 0))
        filled = pct / 100 * bw
        for i in range(bw):
            if i < int(filled) or (i == int(filled) and filled - i >= 0.5):
                self.put(y, x + lw + i, "━", self.col(self.grad(i / max(1, bw - 1) * 100), attr=curses.A_BOLD))
            else:
                self.put(y, x + lw + i, "━", self.col(TRACK))
        self.put(y, x + lw + bw, f"{pct:6.1f}%", self.col(self.grad(pct), attr=curses.A_BOLD))
        if extra:
            self.put(y, x + lw + bw + 8, extra, self.col(DIM))

    def ring(self, y, x, H, pct, label, avail=True):
        """Circular gauge drawn with braille dots; W = 2*H cells gives a round circle."""
        W = 2 * H
        DW, DH = W * 2, H * 4
        cx, cy = DW / 2, DH / 2
        r = min(DW, DH) / 2 - 0.3
        t = max(2.0, r * 0.24)
        bits = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))
        p = max(0.0, min(100.0, pct)) if avail else 0.0
        for row in range(H):
            for c in range(W):
                m = 0
                for dy in range(4):
                    for dx in range(2):
                        d = math.hypot(c * 2 + dx + 0.5 - cx, row * 4 + dy + 0.5 - cy)
                        if r - t <= d <= r:
                            m |= bits[dy][dx]
                if not m:
                    continue
                frac = (math.atan2(c * 2 + 1 - cx, -(row * 4 + 2 - cy)) / (2 * math.pi)) % 1.0
                if frac * 100 < p:
                    a = self.col(self.grad(frac * 100), attr=curses.A_BOLD, keep=True)
                else:
                    a = self.col(TRACK)
                self.put(y + row, x + c, chr(0x2800 + m), a)
        mid = y + H // 2
        txt = f"{pct:.0f}%" if avail else "N/A"
        self.put(mid - (1 if H >= 7 else 0), x + (W - len(txt)) // 2, txt,
                 self.col(self.grad(p) if avail else DIM, attr=curses.A_BOLD, keep=True))
        if H >= 7:
            self.put(mid + 1, x + (W - len(label)) // 2, label, self.col(DIM))

    def gauge_panel(self, y, x, w, h, title, accent, pct, label, info, avail=True):
        self.box(y, x, h, w, title, accent)
        H = max(3, min(h - 3, (w - 4) // 6))
        self.ring(y + 1 + (h - 2 - H) // 2, x + 2, H, pct, label, avail)
        ix = x + 2 + 2 * H + 2
        iw = x + w - 2 - ix
        for i, (k, v, vc) in enumerate(info[: h - 2]):
            yy = y + 1 + i
            self.put(yy, ix, f"{k:<8}"[:iw], self.col(accent, attr=curses.A_BOLD))
            self.put(yy, ix + 8, str(v)[: max(0, iw - 8)], self.col(vc))

    def spark(self, y, x, data, width):
        chars = "▁▂▃▄▅▆▇█"
        for i, v in enumerate(data[-width:]):
            k = min(7, int(v / 100 * 7.99))
            self.put(y, x + i, chars[k], self.col(self.grad(v)))

    # ── logic ──
    def update(self):
        self.cpu.update()
        self.disk.update()
        self.net.update()
        self.mem = meminfo()
        self.gpus = self.gpu.info()
        self.npus = self.npu.info()
        self.mounts = self.disk.mounts()
        self.history.append(self.cpu.usage[0])
        self.history = self.history[-300:]
        procs = self.procs.update(self.cpu.total_jiffies, len(self.cpu.usage) - 1, self.mem["total"])
        per_pid = self.gpu.clients.per_pid
        for p in procs:
            p["gpu"] = per_pid.get(p["pid"], 0.0)
        self.all = procs
        self.resort()

    def sort_key(self, p):
        v = p[self.sort]
        return v.lower() if isinstance(v, str) else v

    def resort(self):
        self.all.sort(key=self.sort_key, reverse=self.rev)
        self.apply_filter()

    def apply_filter(self):
        q = self.query.lower().strip()
        if not q:
            self.plist = self.all
        else:
            self.plist = [p for p in self.all if q in p["name"].lower() or q in p["cmd"].lower()
                          or q == str(p["pid"]) or q == p["user"].lower()]
        self.build_rows()

    @staticmethod
    def group_name(name):
        return "Kernel threads" if name.startswith("[") else name.split(" › ")[0]

    def row_ident(self, i):
        if 0 <= i < len(self.rows):
            kind, obj, _ = self.rows[i]
            return (kind, obj["name"] if kind == "g" else obj["pid"])
        return None

    def build_rows(self):
        ident = self.row_ident(self.sel) if getattr(self, "rows", None) else None
        if not self.grouped:
            rows = [("p", p, 0) for p in self.plist]
        else:
            groups = {}
            for p in self.plist:
                groups.setdefault(self.group_name(p["name"]), []).append(p)
            glist = []
            for name, ps in groups.items():
                users = {p["user"] for p in ps}
                glist.append({"name": name, "procs": ps, "count": len(ps),
                              "pid": min(p["pid"] for p in ps),
                              "user": ps[0]["user"] if len(users) == 1 else "(multiple)",
                              "cpu": sum(p["cpu"] for p in ps), "gpu": sum(p["gpu"] for p in ps),
                              "mem": sum(p["mem"] for p in ps), "memp": sum(p["memp"] for p in ps),
                              "threads": sum(p["threads"] for p in ps), "state": "",
                              "cmd": f"{len(ps)} processes"})
            glist.sort(key=self.sort_key, reverse=self.rev)
            rows = []
            for g in glist:
                if g["count"] == 1:
                    rows.append(("p", g["procs"][0], 0))
                    continue
                rows.append(("g", g, 0))
                if g["name"] in self.expanded:
                    rows += [("p", p, 1) for p in g["procs"]]
        self.rows = rows
        if ident and not self.follow_top:
            for i in range(len(rows)):
                if self.row_ident(i) == ident:
                    self.sel = i
                    break

    def selected_target(self):
        """-> (list of pids, label) for the selected row"""
        if not self.rows:
            return [], ""
        kind, obj, _ = self.rows[max(0, min(self.sel, len(self.rows) - 1))]
        if kind == "g":
            return [p["pid"] for p in obj["procs"]], f'{obj["count"]} processes of "{obj["name"]}"'
        return [obj["pid"]], f'"{obj["name"]}" (PID {obj["pid"]})'

    def set_sort(self, key):
        if self.sort == key:
            self.rev = not self.rev
        else:
            self.sort, self.rev = key, key not in ("pid", "name", "user")
        self.resort()

    def toggle_group(self, open_=None):
        if not self.rows:
            return
        kind, obj, depth = self.rows[self.sel]
        if kind == "p" and depth:  # child row -> act on its group
            name = self.group_name(obj["name"])
        elif kind == "g":
            name = obj["name"]
        else:
            return
        want = (name not in self.expanded) if open_ is None else open_
        if want:
            self.expanded.add(name)
        else:
            self.expanded.discard(name)
        self.build_rows()
        if not want:
            for i, (k, o, _) in enumerate(self.rows):
                if k == "g" and o["name"] == name:
                    self.sel = i
                    break

    def activate(self):
        """Enter / double click: expand group or open process details"""
        if not self.rows:
            return
        kind, obj, _ = self.rows[self.sel]
        if kind == "g":
            self.toggle_group()
        else:
            self.detail = obj["pid"]

    def ask_kill(self, sig):
        pids, label = self.selected_target() if self.detail is None else \
            ([self.detail], f"PID {self.detail}")
        if not pids:
            return
        verb = "Terminate" if sig == signal.SIGTERM else "Force kill"
        self.confirm = {"text": f"{verb} {label}?", "action": lambda: self.kill(pids, sig)}

    def kill(self, pids, sig):
        ok, denied, gone = 0, 0, 0
        for pid in pids:
            try:
                os.kill(pid, sig)
                ok += 1
            except PermissionError:
                denied += 1
            except ProcessLookupError:
                gone += 1
        if denied:
            self.msg = f" Permission denied for {denied} process(es) – try sudo "
        elif ok:
            self.msg = f" Sent {'SIGTERM' if sig == signal.SIGTERM else 'SIGKILL'} to {ok} process(es) "
        else:
            self.msg = " Process no longer exists "
        self.msg_t = time.time()
        self.detail = None

    def draw(self):
        s = self.scr
        s.erase()
        self.hits = []
        h, w = s.getmaxyx()
        if h < 10 or w < 40:
            self.put(0, 0, f"Window too small ({w}x{h})"[: w], self.col(BRIGHT, attr=curses.A_BOLD))
            self.put(1, 0, "Please enlarge it (min 40x10)"[: w], self.col(DIM))
            s.refresh()
            return
        m = self.mem

        # ── title bar ──
        up = float(read("/proc/uptime", "0").split()[0])
        load = " ".join(read("/proc/loadavg").split()[:3])
        tb = self.col(BRIGHT, TITLE_BG, curses.A_BOLD)
        self.put(0, 0, " " * w, tb)
        segs = [("◆ SimpleTskMgr", self.col(226, TITLE_BG, curses.A_BOLD)),
                (os.uname().nodename, self.col(159, TITLE_BG, curses.A_BOLD)),
                (self.distro, self.col(219, TITLE_BG, curses.A_BOLD)),
                (f"up {int(up // 86400)}d {int(up % 86400 // 3600):02d}:{int(up % 3600 // 60):02d}",
                 self.col(194, TITLE_BG)),
                (f"load {load}", self.col(223, TITLE_BG))]
        x = 1
        for i, (t, a) in enumerate(segs):
            if i:
                self.put(0, x, " │ ", self.col(140, TITLE_BG)); x += 3
            self.put(0, x, t, a); x += len(t)
        clock = time.strftime(" %H:%M:%S ")
        self.put(0, w - len(clock), clock, tb)
        if self.new_version:
            up_txt = f" ↑ v{self.new_version} available – press u "
            self.put(0, max(0, w - len(clock) - len(up_txt) - 1), up_txt, self.col(16, 250, curses.A_BOLD))

        # ── gauge row: CPU / GPU / RAM ──
        compact = h < 20 or w < 60
        gh = 11 if h >= 42 else 9 if h >= 34 else 7 if h >= 26 else 5
        gw = w // 3
        fr, tp = self.cpu.freq(), self.cpu.temp()
        ncores = len(self.cpu.usage) - 1
        cpu_info = [("Model", self.cpu.model.replace("Processor", "").replace("-Core", "C").strip(), TEXT),
                    ("Cores", f"{ncores} threads", TEXT),
                    ("Clock", f"{fr:.0f} MHz" if fr else "n/a", 117),
                    ("Temp", f"{tp:.0f} °C" if tp else "n/a", self.grad(tp) if tp else DIM),
                    ("Load", load, 223),
                    ("Procs", str(len(self.all)), 183)]
        rp = m["used"] / m["total"] * 100
        g = self.gpus[0] if self.gpus else None
        if compact:  # small window: three plain bars instead of ring gauges
            self.bar(1, 1, w - 2, self.cpu.usage[0], "CPU")
            if g and g["util"] is not None:
                self.bar(2, 1, w - 2, g["util"], "GPU")
            else:
                self.put(2, 1, "GPU   n/a", self.col(DIM))
            self.bar(3, 1, w - 2, rp, "RAM", f"{human(m['used']).strip()}/{human(m['total']).strip()}")
            py = 4
            return self.draw_procs(py, h, w)
        self.gauge_panel(1, 0, gw, gh, "CPU", ACCENT["cpu"], self.cpu.usage[0], "usage", cpu_info)

        if g:
            gi = [("Name", g["name"], TEXT),
                  ("Usage", f"{g['util']:.0f} %" if g["util"] is not None else "not reported", 117 if g["util"] is not None else DIM),
                  ("VRAM", f"{human(g['mem_used']).strip()} / {human(g['mem_total']).strip()}" if g["mem_total"] else "n/a", 183),
                  ("Temp", f"{g['temp']:.0f} °C" if g["temp"] else "n/a", self.grad(g["temp"]) if g["temp"] else DIM)]
            if g.get("power") is not None:
                gi.append(("Power", f"{g['power']:.0f} W", TEXT))
            if len(self.gpus) > 1:
                gi.append(("More", f"+{len(self.gpus) - 1} GPU(s)", DIM))
            self.gauge_panel(1, gw, gw, gh, "GPU", ACCENT["gpu"], g["util"] or 0, "usage", gi, g["util"] is not None)
        else:
            self.gauge_panel(1, gw, gw, gh, "GPU", ACCENT["gpu"], 0, "usage", [("Name", "no GPU found", DIM)], False)

        ram_info = [("Used", human(m["used"]).strip(), self.grad(rp)),
                    ("Free", human(m["total"] - m["used"]).strip(), 120),
                    ("Total", human(m["total"]).strip(), TEXT),
                    ("Cached", human(m["cached"]).strip(), 117),
                    ("Swap", f"{human(m['swap_used']).strip()} / {human(m['swap_total']).strip()}" if m["swap_total"] else "none", 183)]
        self.gauge_panel(1, 2 * gw, w - 2 * gw, gh, "Memory", ACCENT["ram"], rp, "used", ram_info)

        # ── cores + system row (only if there is room) ──
        y0 = 1 + gh
        if h < 34:  # not enough room for cores/system/disks -> more process rows
            return self.draw_procs(y0, h, w)
        half = w // 2
        cores = self.cpu.usage[1:]
        ncols = 2 if half > 70 else 1
        sys_lines = 2 + len(self.gpus[:2]) + max(1, len(self.npus)) + 2
        rows_needed = max((len(cores) + ncols - 1) // ncols, sys_lines)
        sh = min(rows_needed, max(3, (h - y0 - 12) // 2)) + 2
        crow = sh - 2
        self.box(y0, 0, sh, half, "CPU cores", ACCENT["cores"])
        cw = (half - 4) // ncols
        for i, u in enumerate(cores):
            r_, c_ = i % crow, i // crow
            if c_ >= ncols:
                break
            self.bar(y0 + 1 + r_, 2 + c_ * cw, cw - 1, u, f"C{i}")

        self.box(y0, half, sh, w - half, "System", ACCENT["sys"])
        bw = w - half - 4
        yy = y0 + 1
        lines = []
        if m["swap_total"]:
            lines.append(("bar", m["swap_used"] / m["swap_total"] * 100, "Swap",
                          f"{human(m['swap_used']).strip()}/{human(m['swap_total']).strip()}", 183))
        for i, gg in enumerate(self.gpus[:2]):
            if gg["mem_total"]:
                lines.append(("bar", gg["mem_used"] / gg["mem_total"] * 100, f"VRAM{i}",
                              f"{human(gg['mem_used']).strip()}/{human(gg['mem_total']).strip()}", ACCENT["gpu"]))
        if self.npus:
            for i, n in enumerate(self.npus):
                if n["util"] is not None:
                    lines.append(("bar", n["util"], f"NPU{i}", n["name"], 214))
                else:
                    lines.append(("txt", f"NPU{i}", n["name"] + "  (no usage data)", 214, DIM))
        else:
            lines.append(("txt", "NPU", "no NPU found", 214, DIM))
        rx, tx = self.net.rate
        lines.append(("net", rx, tx))
        dr, dwr = self.disk.rate
        lines.append(("io", dr, dwr))
        for ln in lines:
            if yy >= y0 + sh - 1:
                break
            if ln[0] == "bar":
                self.bar(yy, half + 2, bw, ln[1], ln[2], ln[3], ln[4])
            elif ln[0] == "txt":
                self.put(yy, half + 2, f"{ln[1]:<6}", self.col(ln[3], attr=curses.A_BOLD))
                self.put(yy, half + 8, ln[2][: bw - 6], self.col(ln[4]))
            elif ln[0] == "net":
                self.put(yy, half + 2, "Net", self.col(51, attr=curses.A_BOLD))
                self.put(yy, half + 8, f"↓ {human(ln[1]).strip()}/s", self.col(120, attr=curses.A_BOLD))
                self.put(yy, half + 24, f"↑ {human(ln[2]).strip()}/s", self.col(213, attr=curses.A_BOLD))
            else:
                self.put(yy, half + 2, "Disk", self.col(ACCENT["disk"], attr=curses.A_BOLD))
                self.put(yy, half + 8, f"R {human(ln[1]).strip()}/s", self.col(228, attr=curses.A_BOLD))
                self.put(yy, half + 24, f"W {human(ln[2]).strip()}/s", self.col(209, attr=curses.A_BOLD))
            yy += 1

        # ── disks ──
        dy = y0 + sh
        dh = min(len(self.mounts), 4, max(1, (h - dy - 9) // 2)) + 2
        if h - dy - dh < 12:
            return self.draw_procs(dy, h, w)
        self.box(dy, 0, dh, w, "Disks", ACCENT["disk"], f"{len(self.mounts)} mounted")
        for i, (mnt, tot, used, fs) in enumerate(self.mounts[: dh - 2]):
            self.bar(dy + 1 + i, 2, w - 4, used / tot * 100, mnt[-6:],
                     f"{human(used).strip()}/{human(tot).strip()}  {fs:<5} {mnt}", ACCENT["disk"])

        self.draw_procs(dy + dh, h, w)

    def hit(self, y, x0, x1, action, overlay=False):
        self.hits.append((y, x0, x1, action, overlay))

    def draw_procs(self, py, h, w):
        s = self.scr
        # ── processes ──
        ph = h - py - 1
        title = f"Processes ({len(self.plist)})"
        if self.query:
            title = f"Processes ({len(self.plist)}/{len(self.all)}) · filter: {self.query}"
        if self.grouped:
            title += " · grouped"
        self.box(py, 0, ph, w, title[: w - 6], ACCENT["proc"],
                 f"sorted by {self.sort} {'▼' if self.rev else '▲'}" if w >= 70 else "")
        if not self.plist and self.query:
            self.put(py + 3, 3, f'No process matches "{self.query}"', self.col(DIM))
        arrow = "▼" if self.rev else "▲"
        nw = max(14, min(30, (w - 56) // 3))
        wide = w >= 96
        cols = [("pid", "PID", 7)] + ([("user", "USER", 10), ("", "S", 2)] if wide else []) + \
               [("cpu", "CPU%", 7)] + ([("gpu", "GPU%", 6)] if wide else []) + [("mem", "MEM", 9)] + \
               ([("memp", "MEM%", 6), ("threads", "THR", 5)] if wide else []) + \
               [("name", "NAME", nw), ("", "COMMAND", 0)]
        self.put(py + 1, 1, " " * (w - 2), self.col(BRIGHT, HEADER_BG, curses.A_BOLD))
        x = 1
        for k, n, wd in cols:
            n = n + (arrow if k == self.sort else "")
            txt = (n.ljust(wd) if k == "name" else n.rjust(wd)) if wd else n
            self.put(py + 1, x, txt, self.col(BRIGHT, HEADER_BG, curses.A_BOLD))
            if k:
                self.hit(py + 1, x, x + max(wd, len(n)), lambda k=k: self.set_sort(k))
            x += (wd or len(n)) + 1
        rows = ph - 3
        self.page = max(1, rows)
        self.sel = max(0, min(self.sel, len(self.rows) - 1))
        if self.sel < self.off:
            self.off = self.sel
        if self.sel >= self.off + rows:
            self.off = self.sel - rows + 1
        self.off = max(0, min(self.off, max(0, len(self.rows) - rows)))
        for i, (kind, p, depth) in enumerate(self.rows[self.off: self.off + rows]):
            yy = py + 2 + i
            idx = self.off + i
            selected = idx == self.sel
            bg = SEL_BG if selected else (ZEBRA_BG if i % 2 else -1)
            ex = curses.A_BOLD if selected else 0
            self.put(yy, 1, " " * (w - 2), self.col(TEXT, bg))
            self.hit(yy, 1, w - 2, lambda idx=idx: self.click_row(idx))
            x = 1

            def cell(txt, wd, fg, attr=0):
                nonlocal x
                self.put(yy, x, txt, self.col(BRIGHT if selected else fg, bg, attr | ex))
                x += wd + 1

            grp = kind == "g"
            cell(f"{'' if grp else p['pid']:>7}", 7, 141)
            if wide:
                cell(f"{p['user'][:10]:>10}", 10, 203 if p["user"] == "root" else 75)
                cell(f"{p['state']:>2}", 2, {"R": 82, "S": DIM, "I": 240, "D": 208, "Z": 196}.get(p["state"], DIM))
            cell(f"{p['cpu']:7.1f}", 7, BRIGHT if p["cpu"] >= 0.1 else 240,
                 curses.A_BOLD if p["cpu"] > 5 else 0)
            if wide:
                cell(f"{p['gpu']:6.1f}" if p["gpu"] >= 0.05 else f"{'·':>6}", 6,
                     BRIGHT if p["gpu"] >= 0.1 else 240)
            mc = BRIGHT if p["memp"] >= 0.1 else 240
            cell(f"{human(p['mem']):>9}", 9, mc)
            if wide:
                cell(f"{p['memp']:6.1f}", 6, mc)
                cell(f"{p['threads']:>5}", 5, DIM)
            if grp:
                nm = f"{'▾' if p['name'] in self.expanded else '▸'} {p['name']} ({p['count']})"
            elif depth:
                nm = f"  · {p['name']}"
            else:
                nm = p["name"]
            nm = nm if len(nm) <= nw else nm[: nw - 1] + "…"
            cell(f"{nm:<{nw}}", nw, BRIGHT, curses.A_BOLD)
            room = w - x - 1
            if room > 3:
                self.put(yy, x, p["cmd"][:room], self.col(BRIGHT if selected else DIM, bg))

        # ── footer ──
        keys = [("q", "Quit", ord("q")), ("/", "Search", ord("/")), ("g", "Group", ord("g")),
                ("Enter", "Details", 10), ("k", "Kill", ord("k")), ("c", "CPU", ord("c")),
                ("m", "Mem", ord("m")), ("p", "PID", ord("p")), ("n", "Name", ord("n")),
                ("r", "Reverse", ord("r")), ("+/-", "Interval", ord("+"))]
        if self.new_version:
            keys.insert(0, ("u", "Update", ord("u")))
        self.put(h - 1, 0, " " * w, self.col(TEXT, 236))
        x = 1
        for k, d, code in keys:
            if x + len(k) + len(d) + 5 > w - 16:
                break
            x0 = x
            self.put(h - 1, x, f" {k} ", self.col(16, 81, curses.A_BOLD)); x += len(k) + 2
            self.put(h - 1, x, f" {d}  ", self.col(TEXT, 236)); x += len(d) + 3
            self.hit(h - 1, x0, x - 2, lambda code=code: self.handle_key(code))
        if self.searching:
            self.put(h - 1, 0, " " * w, self.col(TEXT, 236))
            self.put(h - 1, 1, " / ", self.col(16, 250, curses.A_BOLD))
            self.put(h - 1, 5, f"Search: {self.query}", self.col(BRIGHT, 236, curses.A_BOLD))
            self.put(h - 1, 13 + len(self.query), "█", self.col(BRIGHT, 236))
            hint = " Enter keep · Esc clear "
            if w > len(self.query) + 40:
                self.put(h - 1, w - len(hint) - 1, hint, self.col(DIM, 236))
        elif self.msg and time.time() - self.msg_t < 3:
            self.put(h - 1, max(0, w - len(self.msg) - 1), self.msg, self.col(BRIGHT, 161, curses.A_BOLD))
        else:
            iv = f" v{VERSION} · {self.interval:.1f}s "
            self.put(h - 1, w - len(iv) - 1, iv, self.col(16, 120, curses.A_BOLD))

        if self.detail is not None:
            self.draw_details(h, w)
        if self.confirm:
            self.draw_confirm(h, w)
        s.refresh()

    # ── overlays ──
    def panel(self, h, w, ph, pw, title):
        y, x = max(0, (h - ph) // 2), max(0, (w - pw) // 2)
        for i in range(ph):
            self.put(y + i, x, " " * pw, self.col(TEXT, 234))
        a = self.col(BRIGHT, 234)
        self.put(y, x, "╭" + "─" * (pw - 2) + "╮", a)
        for i in range(1, ph - 1):
            self.put(y + i, x, "│", a)
            self.put(y + i, x + pw - 1, "│", a)
        self.put(y + ph - 1, x, "╰" + "─" * (pw - 2) + "╯", a)
        self.put(y, x + 2, f" {title} "[: pw - 4], self.col(BRIGHT, 234, curses.A_BOLD))
        return y, x

    def button(self, y, x, label, action, primary=False):
        self.put(y, x, f" {label} ", self.col(16 if primary else BRIGHT, 250 if primary else 240, curses.A_BOLD))
        self.hit(y, x, x + len(label) + 1, action, overlay=True)
        return x + len(label) + 3

    def draw_confirm(self, h, w):
        text = self.confirm["text"]
        pw = min(w - 2, max(44, len(text) + 6))
        y, x = self.panel(h, w, 7, pw, "Confirm")
        self.put(y + 2, x + 3, text[: pw - 6], self.col(BRIGHT, 234, curses.A_BOLD))
        bx = x + pw - 28
        bx = self.button(y + 4, bx, "Yes (y)", lambda: self.handle_key(ord("y")), primary=True)
        self.button(y + 4, bx, "No (n)", lambda: self.handle_key(ord("n")))

    def proc_details(self, pid):
        base = f"/proc/{pid}"
        stat = read(base + "/stat")
        if not stat:
            return None
        f = stat[stat.rindex(")") + 2:].split()
        status = {}
        for line in read(base + "/status").splitlines():
            k, _, v = line.partition(":")
            status[k] = v.strip()
        btime = 0
        for line in read("/proc/stat").splitlines():
            if line.startswith("btime"):
                btime = int(line.split()[1])
        started = btime + int(f[19]) / CLK_TCK
        run = max(0, time.time() - started)

        def link(p):
            try:
                return os.readlink(p)
            except OSError:
                return "(no access)"

        io = {}
        for line in read(base + "/io").splitlines():
            k, _, v = line.partition(":")
            io[k] = int(v.strip() or 0)
        unit = ""
        for line in read(base + "/cgroup").splitlines():
            for part in line.split("/")[::-1]:
                if part.endswith((".service", ".scope")):
                    unit = part
                    break
            if unit:
                break
        try:
            nfd = str(len(os.listdir(base + "/fd")))
        except OSError:
            nfd = "(no access)"
        ppid = int(f[1])
        pname = read(f"/proc/{ppid}/comm").strip() if ppid else ""
        p = next((x for x in self.all if x["pid"] == pid), None)
        d, hh, mm = int(run // 86400), int(run % 86400 // 3600), int(run % 3600 // 60)
        rows = [
            ("Name", p["name"] if p else read(base + "/comm").strip()),
            ("PID", f"{pid}    parent: {ppid} {('(' + pname + ')') if pname else ''}"),
            ("User", f"{p['user'] if p else '?'}    state: {status.get('State', '?')}"),
            ("CPU / GPU", f"{p['cpu']:.1f} %  /  {p['gpu']:.1f} %" if p else "?"),
            ("Memory", f"RSS {human(int(status.get('VmRSS', '0 kB').split()[0]) * 1024).strip()}"
                       f"   swap {human(int(status.get('VmSwap', '0 kB').split()[0]) * 1024).strip()}"),
            ("Threads", f"{status.get('Threads', '?')}    priority (nice): {f[16]}    open files: {nfd}"),
            ("Started", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started))
             + f"   (running {d}d {hh:02d}:{mm:02d})"),
            ("Disk I/O", f"read {human(io['read_bytes']).strip()}   written {human(io['write_bytes']).strip()}"
             if io else "(no access – try sudo)"),
            ("Service", unit or "–"),
            ("Program", link(base + "/exe")),
            ("Work dir", link(base + "/cwd")),
            ("Command", p["cmd"] if p else read(base + "/cmdline").replace("\0", " ")),
        ]
        return rows

    def draw_details(self, h, w):
        rows = self.proc_details(self.detail)
        if rows is None:
            self.detail = None
            self.msg, self.msg_t = " Process has exited ", time.time()
            return
        pw = min(w - 2, 100)
        vw = pw - 17
        lines = []
        for k, v in rows:
            v = str(v) or "–"
            chunks = [v[i: i + vw] for i in range(0, len(v), vw)] or [""]
            if k == "Command":
                chunks = chunks[: max(1, h - 20)]
            else:
                chunks = chunks[:1]
            for j, c in enumerate(chunks):
                lines.append((k if j == 0 else "", c))
        ph = min(h - 1, len(lines) + 5)
        y, x = self.panel(h, w, ph, pw, "Process details")
        for i, (k, v) in enumerate(lines[: ph - 5]):
            self.put(y + 2 + i, x + 3, f"{k:<12}", self.col(DIM, 234, curses.A_BOLD))
            self.put(y + 2 + i, x + 16, v, self.col(BRIGHT, 234))
        by = y + ph - 2
        bx = x + 3
        bx = self.button(by, bx, "Terminate (k)", lambda: self.handle_key(ord("k")))
        bx = self.button(by, bx, "Force kill (K)", lambda: self.handle_key(ord("K")))
        self.button(by, bx, "Close (Esc)", lambda: self.handle_key(27), primary=True)

    # ── input ──
    def click_row(self, idx):
        now = time.time()
        double = idx == self.last_click[0] and now - self.last_click[1] < 0.4
        self.sel = idx
        self.follow_top = False
        self.last_click = (idx, 0 if double else now)
        if double:
            self.activate()

    def on_mouse(self):
        try:
            _, mx, my, _, bstate = curses.getmouse()
        except curses.error:
            return
        up = bstate & curses.BUTTON4_PRESSED
        down = bstate & getattr(curses, "BUTTON5_PRESSED", 0x200000)
        if (up or down) and self.detail is None and not self.confirm:
            self.sel += -3 if up else 3
            self.follow_top = False
            return
        if not bstate & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED):
            return
        overlay = self.detail is not None or self.confirm
        for y, x0, x1, action, ov in reversed(self.hits):
            if overlay and not ov:
                continue
            if y == my and x0 <= mx <= x1:
                action()
                return

    def handle_key(self, k):
        """Returns "quit" to exit the main loop."""
        if k == curses.KEY_MOUSE:
            self.on_mouse()
            return None
        if self.confirm:
            if k in (ord("y"), ord("Y"), 10, 13, curses.KEY_ENTER):
                action, self.confirm = self.confirm["action"], None
                action()
            elif k in (ord("n"), ord("N"), 27, ord("q")):
                self.confirm = None
            return None
        if self.detail is not None:
            if k in (27, 10, 13, curses.KEY_ENTER, ord("q"), curses.KEY_LEFT, curses.KEY_BACKSPACE):
                self.detail = None
            elif k == ord("k"):
                self.ask_kill(signal.SIGTERM)
            elif k == ord("K"):
                self.ask_kill(signal.SIGKILL)
            return None
        if self.searching:
            if k in (10, 13, curses.KEY_ENTER):
                self.searching = False
            elif k == 27:
                self.searching, self.query = False, ""
            elif k in (curses.KEY_BACKSPACE, 127, 8):
                self.query = self.query[:-1]
            elif k == curses.KEY_UP:
                self.sel -= 1
            elif k == curses.KEY_DOWN:
                self.sel += 1
            elif 32 <= k < 127:
                self.query += chr(k)
            if k not in (curses.KEY_UP, curses.KEY_DOWN):
                self.sel = self.off = 0
            self.apply_filter()
            return None
        keymap = {ord("c"): "cpu", ord("m"): "mem", ord("p"): "pid", ord("n"): "name"}
        if k == 27 and self.query:  # Esc clears an active filter first
            self.query = ""
            self.apply_filter()
        elif k in (ord("q"), ord("Q"), 27):
            return "quit"
        elif k == ord("/"):
            self.searching = True
        elif k in keymap:
            self.sort, self.rev = keymap[k], keymap[k] not in ("pid", "name")
            self.resort()
        elif k == ord("r"):
            self.rev = not self.rev
            self.resort()
        elif k == ord("g"):
            self.grouped = not self.grouped
            self.sel = self.off = 0
            self.build_rows()
        elif k in (10, 13, curses.KEY_ENTER):
            self.activate()
        elif k in (curses.KEY_RIGHT, ord(" ")):
            self.toggle_group(True if k == curses.KEY_RIGHT else None)
        elif k == curses.KEY_LEFT:
            self.toggle_group(False)
        elif k in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_PPAGE, curses.KEY_NPAGE, curses.KEY_HOME, curses.KEY_END):
            step = {curses.KEY_UP: -1, curses.KEY_DOWN: 1, curses.KEY_PPAGE: -self.page,
                    curses.KEY_NPAGE: self.page, curses.KEY_HOME: -10 ** 9, curses.KEY_END: 10 ** 9}[k]
            self.sel = max(0, min(len(self.rows) - 1, self.sel + step))
            self.follow_top = False
        elif k == ord("k"):
            self.ask_kill(signal.SIGTERM)
        elif k == ord("K"):
            self.ask_kill(signal.SIGKILL)
        elif k == ord("+"):
            self.interval = min(10, self.interval + 0.5)
        elif k == ord("-"):
            self.interval = max(0.5, self.interval - 0.5)
        elif k in (ord("u"), ord("U")):
            self.install_update()
            if self.restart:
                return "quit"
        return None

    def run(self):
        time.sleep(0.2)
        self.update()
        last = time.time()
        while True:
            self.draw()
            while True:
                k = self.scr.getch()
                if k == -1:
                    if time.time() - last >= self.interval:
                        break
                    time.sleep(0.03)
                    continue
                if self.handle_key(k) == "quit":
                    return
                self.draw()
            self.update()
            last = time.time()


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("-v", "--version"):
        print(f"SimpleTskMgr {VERSION}")
        return
    os.environ.setdefault("ESCDELAY", "25")
    app = {}

    def start(scr):
        app["a"] = App(scr)
        app["a"].run()

    curses.wrapper(start)
    if app.get("a") and app["a"].restart:  # updated -> start the new version
        print(f"SimpleTskMgr updated to v{app['a'].new_version}, restarting...")
        path = os.path.realpath(__file__)
        os.execv(sys.executable, [sys.executable, path] + sys.argv[1:])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
