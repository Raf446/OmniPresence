<div align="center">

# 🌌 OmniPresence


<p align="center">
  <strong>High-Performance, Multi-Context Discord Rich Presence Engine for Windows</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-2.0.0-blue.svg?style=flat-square" alt="Version 2.0.0" />
  <img src="https://img.shields.io/badge/platform-Windows%2010%20%7C%2011-0078D6.svg?style=flat-square" alt="Platform" />
  <img src="https://img.shields.io/badge/python-3.11%2B-3776AB.svg?style=flat-square" alt="Python" />
  <img src="https://img.shields.io/badge/gui-PySide6%20(Qt6)-41CD52.svg?style=flat-square" alt="GUI" />
  <img src="https://img.shields.io/badge/ipc-Discord%20Native%20Pipe-5865F2.svg?style=flat-square" alt="Discord IPC" />
</p>

---

## Overview

**OmniPresence** adalah utiliti desktop automasi Discord Rich Presence yang menyelesaikan konflik status serentak melalui hierarki timbang tara berperingkat (*tiered priority arbitration*). Enjin ini mengesan aktiviti penyusun kod (*compilers*), persekitaran pembangunan (IDE), mod permainan skrin penuh, sesi media audio (Spotify/SMTC), dan penstriman video penyemak imbas (*browser video streaming*) secara masa nyata dengan latensi peralihan mikrosaat.

Dilengkapi penyelesai metadata asinkronus (`QThreadPool`), OmniPresence memaparkan seni kulit album (*album art*) dan lakaran kecil (*video thumbnails*) resolusi tinggi tanpa memerlukan kunci API luaran.

---

## Core Architecture


```

```
              ┌──────────────────────────────────────────────┐
              │          System Hardware & OS Hooks          │
              │  (WinEvent Hook, SMTC Media, Process Enum)   │
              └──────────────────────┬───────────────────────┘
                                     │ Telemetry & Events
                                     ▼

```

┌────────────────────────────────────────────────────────────────────────────────────────┐
│                                 Priority Engine Core                                   │
│                                                                                        │
│  [P1] Manual Override    -> Hardcoded Developer Debug / Static Lock                    │
│  [P2] Toolchain / Build  -> Compilers, Linkers, Debuggers (GCC, MSVC, Clang, Rustc)    │
│  [P3] Fullscreen Games   -> Direct3D/Vulkan Exclusive & Borderless Windows             │
│  [P4] IDEs & Workspaces  -> Active Focused Windows (VS Code, Visual Studio, etc.)     │
│  [P5] Media & Browsers   -> YouTube Streamers, Spotify Desktop, SMTC Sessions          │
│  [P6] Idle & AFK System  -> Win32 LastInputInfo Sleep Arbitrator                       │
└────────────────────────────────────────┬───────────────────────────────────────────────┘
│ Resolved PresencePayload
▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                              Async Scrobbler & Resolvers                               │
│                                                                                        │
│  • YouTubeThumbnailResolver -> Gzip stream scraping, videoId regex, hqdefault URL     │
│  • MediaScrobbler           -> Noise stripping, difflib fuzzy score, tribute reject   │
└────────────────────────────────────────┬───────────────────────────────────────────────┘
│ Enriched Payload
▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                            Adaptive IPC Transmission Loop                              │
│                                                                                        │
│  • Fast-Lane Priority Switch : 0.25s Debounce (Rule / Tier / Track Transitions)        │
│  • Sustained State Cooldown  : 4.50s Rate-Limit (Discord 4 req / 20s bucket safe)      │
│  • Pipe Transport Failover   : Auto-reconnect \.\pipe\discord-ipc-0 -> 9              │
└────────────────────────────────────────────────────────────────────────────────────────┘

```

---

## Key Features

### 1. Dynamic Zero-Key YouTube Thumbnail Resolver
* **Scraping Resolusi Tinggi:** Mengambil lakaran kecil (`hqdefault.jpg`) terus daripada pengecam `videoId` menggunakan aliran data `gzip` mampat (<180 KB payload) dalam masa bawah ~900 ms tanpa kuota Google Cloud API.
* **Toleran Bunyi Bar Tajuk (*Title Noise Resilience*):** Menapis secara automatik glif pemutaran (`▶`, `►`), penunjuk audio (`🔊`), nombor pemberitahuan tab `(1)`, *zero-width spaces* (`\u200b`), dan akhiran penjenamaan pelayar (`- Brave`, `- Google Chrome`, `- Microsoft Edge`, `— Mozilla Firefox`).
* **Halaman Navigasi:** Memaparkan status elegan seperti `"Browsing YouTube (Subscriptions)"` atau `"Browsing YouTube"` apabila berada di suapan utama.

### 2. Sanitasi Metadata & Pengesahan Album Muzik
* **Algoritma Pembersihan RegEx:** Menyingkirkan teks sampingan seperti `[Official Music Video]`, `(Remastered 2024)`, `(From "Movie Soundtrack")`, dan senarai kolaborator panjang (`feat.`, `with`).
* **Penilaian Calon (*Candidate Similarity Scoring*):** Menggunakan nisbah rentetan token `difflib.SequenceMatcher` dengan ambang kelulusan ketat (`threshold = 0.58`). Menolak padanan palsu daripada kumpulan penghormatan (*tribute bands*), versi karaoke, dan rakaman peminat secara automatik.

### 3. Timbang Tara Konteks Pantas (*Preemptive Arbitration*)
* **Z-Order Window Scanning:** Apabila tetingkap aktif ditinggalkan, konteks latar belakang berbayang (*sticky hold*) serta-merta memberi laluan kepada sesi media (`foreground=True` mengatasi `foreground=False`).
* **Peralihan Pantas IPC (0.25s):** Perubahan lagu atau aplikasi diproses serta-merta tanpa perlu menunggu sela masa semakan pemasa (*polling timer*).

---

## Priority Hierarchy Matrix

| Tier | Pengelas | Skop Pengesanan | Tingkah Laku & Pelucutan (*Eviction*) |
| :---: | :--- | :--- | :--- |
| **P1** | **Manual Override** | Bar kawalan tindakan UI | Mengatasi semua aktiviti sistem secara mutlak sehingga dinyahaktifkan. |
| **P2** | **Toolchain / Build** | `gcc`, `cl`, `rustc`, `cargo`, `ninja` | Aktif semasa pengkompilasi menggunakan kitaran CPU; lupus serta-merta selepas pautan siap. |
| **P3** | **Fullscreen Games** | Senarai binari proses permainan & mod skrin penuh | Mengesan tetingkap DirectX/Vulkan eksklusif; menghalang status media mengaburkan permainan. |
| **P4** | **IDEs & Editors** | `code.exe`, `devenv.exe`, `codeblocks.exe` | Aktif semasa fokus; mempunyai tempoh penimbal (*sticky buffer*) yang gugur serta-merta jika media aktif. |
| **P5** | **Media & Browsers** | Brave, Chrome, Firefox, Edge, Spotify, SMTC | Memaparkan metadata lagu/video aktif bersama kulit album atau lakaran kecil langsung. |
| **P6** | **Idle / Standby** | Win32 `GetLastInputInfo()` (> 300s) | Melupuskan status kerja secara automatik apabila tiada interaksi fizikal papan kekunci/tetikus. |

---

## Project Structure


```

OmniPresence/
├── assets/                  # Vektor SVG, ikon aplikasi, dan lembaran gaya CSS
├── config/
│   └── rules.json           # Skema JSON pengisytiharan peraturan, regex, dan keutamaan
├── core/
│   ├── ipc_manager.py       # Pengendali paip soket Discord IPC, giliran preemptif, & pengehad kadar
│   ├── priority_engine.py   # Logik penyelesai hierarki, penghurai templat, & timbang tara konteks
│   ├── process_watcher.py   # WinEvent foreground hook & pengimbas tetingkap Z-order Win32
│   └── state_manager.py     # Pengurus kitaran hayat aplikasi & penyelarasan isyarat PySide6
├── extractors/
│   ├── media_scrobbler.py   # Pengekstrak sesi Windows SMTC & pengesah album seni iTunes/Deezer
│   └── youtube_resolver.py  # Penyelesai lakaran kecil video YouTube tak segerak (tanpa API key)
├── ui/
│   ├── discord_preview.py   # Widget visualisasi kad Discord masa nyata
│   └── main_window.py       # Antara muka kawalan PySide6 & log telemetri langsung
├── build.bat                # Skrip automasi binaan PyInstaller satu fail (.exe)
├── build_exe.py             # Spesifikasi kompilasi PyInstaller & pembenaman metadata Windows
├── main.py                  # Titik masuk aplikasi, penghurai CLI, & pemula gelung acara Qt
└── requirements.txt         # Ketergantungan pustaka teras runtime

```

---

## Installation & Setup

### Keperluan Sistem
* **Sistem Operasi:** Windows 10 / 11 (64-bit)
* **Persekitaran:** Python 3.11 atau lebih baharu (untuk mod skrip mentah)
* **Klien Sasaran:** Discord Desktop (Stable / PTB / Canary)

### Pemasangan Mod Pembangun

1. **Klon repositori:**
   ```powershell
   git clone [https://github.com/Raf446/OmniPresence.git](https://github.com/Raf446/OmniPresence.git)
   cd OmniPresence

```

2. **Cipta persekitaran maya & pasang pakej:**
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt

```


3. **Uji pelaksanaan skrip (*Smoke Test*):**
```powershell
python main.py --smoke-test 2.0

```


4. **Jalankan aplikasi:**
```powershell
python main.py

```



---

## Production Build (Standalone .EXE)

Untuk menghasilkan satu fail binari mandiri tanpa pergantungan persekitaran Python tempatan:

```powershell
.\build.bat

```

Skrip pengkompilasi akan melaksanakan:

* Pengesahan kebersihan modul dan penguraian metadata versi `v2.0.0`.
* Pembungkusan PyInstaller mod fail tunggal (*one-file*) berprestasi tinggi bersama runtime PySide6 dan Windows SDK DLL.
* Penempatan artifak boleh laku akhir di direktori `dist\OmniPresence.exe` berserta folder sokongan `dist\config\`.

---

## Configuration (`rules.json`)

Tingkah laku pengesanan boleh diubah suai melalui `config/rules.json`. Setiap peraturan menyokong padanan berasaskan corak teks, pembolehubah templat, dan penetapan tahap hierarki:

```json
{
  "id": "browser-youtube",
  "name": "YouTube in the browser",
  "enabled": true,
  "tier": "media",
  "match": {
    "processes": ["chrome.exe", "brave.exe", "msedge.exe", "firefox.exe"],
    "title_regex": [
      "^(?:.*?[\\u25b6\\U0001f50a]?\\s*)?(?P<video>\\S.*?)\\s*[-\\u2013\\u2014]\\s*YouTube(?:\\s*[-\\u2013\\u2014].*)?$"
    ]
  },
  "sticky_seconds": 90,
  "presence": {
    "details": "{video}",
    "state": "Watching on YouTube • {app_label}",
    "large_image": "{youtube_thumbnail|youtube}",
    "large_text": "{video|YouTube}",
    "small_image": "browser",
    "small_text": "{app_label}"
  }
}

```

---

## License

Dilesenkan di bawah [MIT License](https://www.google.com/search?q=LICENSE). Bebas digunakan, diubah suai, dan diedarkan untuk kegunaan peribadi mahupun komersial.

```

```