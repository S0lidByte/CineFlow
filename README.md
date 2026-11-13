<p align="center">
  <img src="https://github.com/S0lidByte/CineFlow/blob/main/assets/Cineflow-Logo.png" alt="CineFlow" width="280">
</p>

<h3 align="center">Modern media automation — stream torrent-based content directly to your media server.</h3>

<p align="center">
  <a href="https://github.com/S0lidByte/CineFlow/stargazers"><img alt="Stars" src="https://img.shields.io/github/stars/S0lidByte/CineFlow?style=flat-square&color=f5a623"></a>
  <a href="https://github.com/S0lidByte/CineFlow/issues"><img alt="Issues" src="https://img.shields.io/github/issues/S0lidByte/CineFlow?style=flat-square"></a>
  <a href="https://github.com/S0lidByte/CineFlow/blob/main/LICENSE"><img alt="License" src="https://img.shields.io/github/license/S0lidByte/CineFlow?style=flat-square"></a>
  <a href="https://github.com/S0lidByte/CineFlow/graphs/contributors"><img alt="Contributors" src="https://img.shields.io/github/contributors/S0lidByte/CineFlow?style=flat-square"></a>
</p>

---

## What is CineFlow?

CineFlow is a self-hosted media automation platform that bridges Debrid providers, content sources, and scrapers to deliver seamless streaming directly to your media server — no manual downloading required.

Originally forked from [Riven](https://github.com/rivenmedia/riven), CineFlow has evolved into its own project with a new vision focused on **modularity**, **reliability**, and **long-term maintainability**. We're grateful to the original Riven contributors for their foundational work.

---

## Supported Services

| Category | Services |
|----------|----------|
| **Debrid Providers** | Real-Debrid, AllDebrid |
| **Content Sources** | Plex Watchlist, Overseerr, Mdblist, Listrr, Trakt |
| **Scrapers** | Comet, Jackett, Torrentio, Orionoid, Mediafusion, Prowlarr, Zilean, Rarbg |
| **Media Servers** | Plex, Jellyfin, Emby |

---

## Table of Contents

- [Self Hosted](#self-hosted)
  - [Installation](#installation)
  - [Plex Setup](#plex-setup)
  - [Troubleshooting](#troubleshooting)
- [VFS & Caching](#cineflow-vfs-and-caching)
- [Contributing](#contributing)
- [License](#license)

---

## Self Hosted

### Installation

Pick a directory on your host that CineFlow will use as its mount point. Throughout this guide it is referenced as:

```
/path/to/cineflow/mount
```

#### 1. Configure Docker Compose

Copy `docker-compose.yml` into your local compose file and update the volume paths to match your environment:

```yaml
volumes:
  - /path/to/cineflow/data:/cineflow/data
  - /path/to/cineflow/mount:/mount:rshared,z
```

> **Important:** Always include `:rshared,z` when mounting `/mount` inside containers to ensure correct mount propagation.

---

#### 2. Create and Share the Mount Directory

Run these commands once per boot:

```bash
sudo mkdir -p /path/to/cineflow/mount
sudo mount --bind /path/to/cineflow/mount /path/to/cineflow/mount
sudo mount --make-rshared /path/to/cineflow/mount
```

Verify propagation:

```bash
findmnt -T /path/to/cineflow/mount -o TARGET,PROPAGATION
```

Expected output: `shared` or `rshared`

---

#### 3. Persist on Boot (Optional)

**Option A — systemd unit**

Create `/etc/systemd/system/cineflow-bind-shared.service`:

```ini
[Unit]
Description=Make CineFlow data bind mount shared
After=local-fs.target
Before=docker.service

[Service]
Type=oneshot
ExecStart=/usr/bin/mount --bind /path/to/cineflow/mount /path/to/cineflow/mount
ExecStart=/usr/bin/mount --make-rshared /path/to/cineflow/mount
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
```

Enable it:

```bash
sudo systemctl enable --now cineflow-bind-shared.service
```

**Option B — fstab**

```
/path/to/cineflow/mount  /path/to/cineflow/mount  none  bind,rshared  0  0
```

---

### Plex Setup

CineFlow currently expects the following Plex library layout:

| Library Type | Category Names |
|-------------|----------------|
| Movies | `movies`, `anime_movies` |
| Shows | `shows`, `anime_shows` |

> These category names may become fully configurable in a future release.

---

### Troubleshooting

#### Plex shows an empty `/mount` after CineFlow restarts

This is almost always a mount propagation or container startup order issue. Work through the following checks:

**1. Re-apply host mount propagation**

```bash
sudo mount --bind /path/to/cineflow/mount /path/to/cineflow/mount
sudo mount --make-rshared /path/to/cineflow/mount
findmnt -T /path/to/cineflow/mount -o TARGET,PROPAGATION
```

**2. Check propagation inside the Plex container**

```bash
docker exec -it plex sh -c 'findmnt -T /mount -o TARGET,PROPAGATION,OPTIONS,FSTYPE'
```

Expected: `rslave` or `rshared`

**3. Verify your Docker Compose Plex volume**

```yaml
- /path/to/cineflow/mount:/mount:rslave,z
```

**4. Clear stale FUSE mounts**

If CineFlow crashed or exited unexpectedly:

```bash
sudo fusermount -uz /path/to/cineflow/mount || sudo umount -l /path/to/cineflow/mount
```

Then restart CineFlow.

---

## CineFlow VFS and Caching

CineFlow ships with a high-performance built-in virtual filesystem (VFS) powered by `pyfuse3` on Linux, featuring an adaptive prefetch engine, multi-tier LRU caching, and direct streaming optimizations.

### Streaming & Cache Settings

| Setting | Type / Range | Default | Description |
|---|---|---|---|
| `filesystem.cache_dir` | string | `/dev/shm/riven-cache` | Directory where cached media chunks are stored (tmpfs or disk) |
| `filesystem.cache_hot_dir` | string / null | `null` | Optional high-speed NVMe directory for two-tier cache demotion |
| `filesystem.cache_max_size_mb` | integer | `10240` (10 GiB) | Maximum total cache capacity in MB across tiers |
| `filesystem.tmpfs_cache_max_mb` | integer | `1024` (1 GiB) | Hard safety cap when cache resides on tmpfs (`/dev/shm`) to prevent OOM |
| `filesystem.cache_ttl_seconds` | integer | `7200` (2h) | Expiration window when eviction policy is set to `TTL` |
| `filesystem.cache_eviction` | `LRU` \| `TTL` | `LRU` | Cache eviction policy (least recently used or time-to-live) |
| `filesystem.cache_metrics` | boolean | `true` | Periodic cache performance metrics logged every 30 seconds |
| `stream.chunk_size_mb` | integer (1–16) | `1` | Size of each on-demand CDN request chunk (4 MiB recommended for 4K) |
| `stream.prefetch_chunks` | integer (0–128) | `12` | Base sequential chunks prefetched ahead of the active read position |
| `stream.target_buffer_seconds` | float (5.0–120.0) | `15.0` | Target playback buffer runway maintained continuously by prefetch |
| `stream.min_safe_runway_seconds` | float (2.0–60.0) | `8.0` | Minimum safe cached buffer before starvation warning or burst refill |
| `stream.parallel_prefetch_workers` | `1` \| `2` \| `4` | `1` | Concurrent prefetch workers (`1` recommended to avoid CDN rate contention) |
| `stream.autotune_mode` | enum | `balanced` | Dynamic prefetch mode (`disabled`, `conservative`, `balanced`, `aggressive`) |
| `stream.sequential_read_tolerance_blocks`| integer | `10` | Interleaved sequential-read tolerance in 128 KiB blocks |
| `stream.scan_tolerance_blocks` | integer | `25` | Forward seek jump size that classifies a read as a library scan |

### Adaptive Prefetching Engine

CineFlow continuously maintains a comfortable playback runway ahead of media players:
- **Continuous Buffer Refill**: Unlike naive on-demand readers, the adaptive prefetch manager evaluates cached buffer runway ($T_{\text{runway}} = \text{cached\_bytes} / \text{bitrate}$) and maintains delivery until `target_buffer_seconds` is satisfied.
- **Bitrate Estimation & Cold-Start Derivation**: Automatically calculates Exponential Moving Average (EMA) playback bitrates. For unprobed large files ($\ge 1\text{ GB}$), CineFlow derives a conservative baseline bitrate (up to 120 Mbps for 4K Remux) to prevent initial buffer starvation.
- **User Settings Priority**: User-configured buffer targets and prefetch chunk limits strictly dictate prefetch ceilings; hardcoded legacy bounds are never applied over user settings.
- **Kernel I/O Optimization**: FUSE readahead coalescing (`max_readahead=1048576`), direct stream handle caching, and Linux page cache retention (`keep_cache=True`) ensure zero stutter during seek-ahead and header inspection.

### Recommended Settings for 4K UHD Remux & Dolby Vision (DoVi)

For high-bitrate 4K Remux, Dolby Vision Profile 7/8, and IMAX Enhanced media (60–100+ Mbps):

```json
{
  "stream": {
    "chunk_size_mb": 4,
    "prefetch_chunks": 32,
    "target_buffer_seconds": 25.0,
    "min_safe_runway_seconds": 10.0,
    "parallel_prefetch_workers": 1,
    "autotune_mode": "balanced"
  },
  "filesystem": {
    "cache_dir": "/mnt/fast-ssd/cineflow-cache",
    "cache_max_size_mb": 20480
  }
}
```

> **Client Direct Play Notice:** Web browsers (such as Chrome / Plex Web) lack native support for Dolby Vision Profile 7/8 in MKV containers, forcing Plex Media Server to transcode 4K video on the fly. Always use dedicated client apps that support Direct Play (e.g. **Plex Desktop for Windows/macOS**, **Infuse on Apple TV**, or **Kodi / Shield TV**) for seamless high-bitrate playback.

### Live Restart API

Operational streaming settings take effect upon backend restart. You can trigger a graceful restart directly from the Frontend (saving the **Operations** settings tab automatically schedules a restart) or via API:

```bash
curl -X POST "http://localhost:8080/api/v1/system/restart" -H "x-api-key: YOUR_API_KEY"
```

---

## Contributing

Contributions are welcome! Before opening a PR, please read:

- [`CONTRIBUTING.md`](CONTRIBUTING.md) — development setup, branch conventions, and code guidelines
- [GitHub Issues](https://github.com/S0lidByte/CineFlow/issues) — bug reports and feature requests
- [Discord](https://todo) — questions, discussion, and community support

All commits should follow the [Conventional Commits](https://www.conventionalcommits.org/) specification.

---

## License

CineFlow is licensed under the **GNU GPLv3**. See the [`LICENSE`](LICENSE) file for full details.
