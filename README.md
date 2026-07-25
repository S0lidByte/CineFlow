<p align="center">
  <img src="https://github.com/S0lidByte/CineFlow/blob/main/assets/Cineflow-Logo.png" alt="CineFlow" width="280">
</p>

<h3 align="center">Modern media automation — stream torrent-based content directly to your media server.</h3>

<p align="center">
  <a href="https://github.com/S0lidByte/CineFlow/stargazers"><img alt="Stars" src="https://img.shields.io/github/stars/S0lidByte/CineFlow?style=flat-square&color=f5a623"></a>
  <a href="https://github.com/S0lidByte/CineFlow/issues"><img alt="Issues" src="https://img.shields.io/github/issues/S0lidByte/CineFlow?style=flat-square"></a>
  <a href="https://github.com/S0lidByte/CineFlow/blob/main/LICENSE"><img alt="License" src="https://img.shields.io/github/license/S0lidByte/CineFlow?style=flat-square"></a>
  <a href="https://github.com/S0lidByte/CineFlow/graphs/contributors"><img alt="Contributors" src="https://img.shields.io/github/contributors/S0lidByte/CineFlow?style=flat-square"></a>
  <a href="https://todo"><img alt="Discord" src="https://img.shields.io/badge/Discord-Join%20Community-5865F2?style=flat-square&logo=discord&logoColor=white"></a>
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

CineFlow ships with a built-in virtual filesystem (VFS) optimised for streaming, intelligent caching, and media organisation.

| Setting | Description |
|---------|-------------|
| `filesystem.cache_dir` | Directory where cached chunks are stored (default `/dev/shm/riven-cache`) |
| `filesystem.cache_max_size_mb` | Maximum total cache size in MB (default 10240) |
| `filesystem.cache_ttl_seconds` | TTL used when eviction policy is TTL (default 2 hours) |
| `filesystem.cache_eviction` | Eviction policy: `LRU` (default) or `TTL` |
| `filesystem.cache_metrics` | Emit periodic VFS cache stats logs (default true) |
| `stream.chunk_size_mb` | Size of each on-demand CDN request chunk (default 1 MiB) |
| `stream.sequential_read_tolerance_blocks` | Interleaved sequential-read tolerance in 128 KiB blocks (default 10) |
| `stream.scan_tolerance_blocks` | Jump size that classifies a read as a library scan (default 25) |

Chunks are fetched **on demand** for the bytes requested by the media client. There is no ahead-of-playback prefetch setting.

By default, CineFlow uses **LRU eviction** — the least recently used cache blocks are removed when space runs low. Set `filesystem.cache_eviction` to `TTL` to expire blocks by `filesystem.cache_ttl_seconds` instead.

### Baseline metrics before further cache work

With `filesystem.cache_metrics` enabled, VFS logs emit `Cache stats: {...}` about every 30s. Capture these before proposing prefetch or extra cache tiers:

- `hits` / `misses` (and derived hit ratio)
- `bytes_from_cache` / `bytes_written`
- `evictions`, `total_bytes`, `entries`

Also note STREAM/VFS log lines for read types (`header_scan`, `footer_scan`, `sequential_read`, `seek`, `body_read`) during library scan, playback, and seek. Miss latency and CDN byte volume are not in the snapshot today — time those from logs or external tooling if needed.

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
