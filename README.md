# SimpleTskMgr

A modern, minimal task manager for the Linux terminal — monochrome UI with
colored ring gauges for **CPU**, **GPU** and **memory**.

## Features

- Ring gauges for CPU, GPU and RAM usage (green → yellow → red)
- Per-core CPU bars with usage history
- GPU: NVIDIA (via `nvidia-smi`), AMD (via `amdgpu` sysfs), others detected
- NPU: Intel NPU (`intel_vpu`) usage, AMD XDNA detected
- Swap, VRAM, network and disk I/O throughput
- Disk usage for all mounted filesystems
- Process list with readable app names (Flatpak apps, Wine, Python/Node/Java scripts) — sort by CPU, memory, PID or name, terminate or kill processes
- Live process search
- Built-in update check
- **Zero dependencies**: a single Python file that reads `/proc` and `/sys` directly

Works on Arch, Debian, Ubuntu, Fedora, openSUSE and any other Linux distro with Python 3.8+.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/RealCat1st/simpletskmgr/main/install.sh | sh
```

This installs:

- the `simpletskmgr` command, plus the short alias **`stm`** (only if no other `stm` command exists on your system)
- a **SimpleTskMgr** entry in your app menu that opens it in a terminal

Everything goes to `~/.local` — no root needed. For a system-wide install into `/usr/local`:

```sh
curl -fsSL https://raw.githubusercontent.com/RealCat1st/simpletskmgr/main/install.sh | sudo sh
```

Or just run it directly without installing:

```sh
git clone https://github.com/RealCat1st/simpletskmgr.git
cd simpletskmgr
./simpletskmgr.py
```

### Uninstall

```sh
curl -fsSL https://raw.githubusercontent.com/RealCat1st/simpletskmgr/main/install.sh | sh -s -- --uninstall
```

## Usage

```sh
stm            # or: simpletskmgr
```

or launch **SimpleTskMgr** from your application menu.

Run with `sudo` to be able to kill processes of other users.

| Key | Action |
|---|---|
| `/` | Search processes by name, command, PID or user (`Enter` keep filter, `Esc` clear) |
| `c` / `m` / `p` / `n` | Sort by CPU / memory / PID / name |
| `r` | Reverse sort order |
| `↑` `↓` `PgUp` `PgDn` `Home` | Select process |
| `k` | Terminate selected process (SIGTERM) |
| `K` | Force kill selected process (SIGKILL) |
| `+` / `-` | Slower / faster refresh |
| `u` | Install update (shown when a new version is available) |
| `q` | Quit |

### Updates

On every start SimpleTskMgr checks GitHub for a newer version (in the background, it never slows down startup).
If one is available, the title bar shows it — press `u` to update and restart.
Set `STM_NO_UPDATE_CHECK=1` to disable the check.

The layout adapts to the window size (minimum 40×10). Best experience at 100×34 or larger with 256-color support (virtually all modern terminals).

## License

[MIT](LICENSE)
