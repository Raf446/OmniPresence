
<div align="center">

# OmniPresence

**Intelligent, Multi-Context Discord Rich Presence Engine for Windows**

[![Version](https://img.shields.io/badge/version-2.3.1-0078D6.svg?style=flat-square)](https://github.com/Raf446/OmniPresence/releases)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%20%7C%2011-0078D6.svg?style=flat-square)](#)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB.svg?style=flat-square)](#)
[![GUI](https://img.shields.io/badge/gui-PySide6%20(Qt6)-41CD52.svg?style=flat-square)](#)
[![License](https://img.shields.io/badge/license-MIT-green.svg?style=flat-square)](mit_license.md)

</div>

---

## ⚡ Overview

**OmniPresence** adalah engine Discord Rich Presence otomatis untuk Windows yang dirancang buat menangani banyak aktivitas sekaligus tanpa konflik status. Menggunakan sistem **Tiered Priority Arbitration (P1–P6)**, aplikasi ini otomatis menentukan aktivitas mana yang paling relevan untuk ditampilkan di profil Discord—mulai dari proses kompilasi kode, game full-screen, IDE, sesi media (Spotify/YouTube), hingga deteksi status idle saat kamu AFK.

Dirancang *plug-and-play* dengan gateway terpusat, antarmuka modern **Pure Obsidian Dark**, auto-updater bawaan, dan installer setup sekali klik tanpa perlu setup manual yang ribet.

---

## ✨ Fitur Unggulan

* **Plug & Play Gateway (Official App ID):**  
  Langsung jalan out-of-the-box menggunakan Official Gateway Application ID terpusat ala PreMiD. Cukup install dan jalankan—tanpa perlu repot bikin bot atau copy-paste Client ID sendiri di Discord Developer Portal (opsi Custom ID tetap tersedia di tab Settings untuk kebutuhan khusus).

* **Auto-Updater via GitHub Releases:**  
  Aplikasi otomatis mengecek rilis terbaru di latar belakang secara *silent*. Saat versi baru rilis di GitHub, banner update akan muncul di UI lengkap dengan tombol instalasi dan mekanisme *hot-swap* otomatis saat aplikasi di-restart.

* **Inno Setup Installer Resmi:**  
  Tersedia installer `.exe` berbasis per-user install (`%LocalAppData%`). Pemasangan praktis tanpa butuh izin Administrator, lengkap dengan opsi Desktop Shortcut dan otomatis jalan saat Windows startup.

* **Zero-Key YouTube & Media Resolvers:**  
  Menampilkan video YouTube dan lagu Spotify yang sedang diputar beserta thumbnail resolusi tinggi secara *real-time* tanpa butuh API key atau akun developer apa pun. Dilengkapi filter cerdas untuk membersihkan judul video dari karakter sampah, unread badge, dan nama browser.

* **Preemptive Foreground & Z-Order Arbitration:**  
  Aktivitas jendela yang sedang aktif di foreground otomatis mengambil prioritas tertinggi dengan latensi transisi super cepat (<0.25s) tetap patuh pada *rate-limit* IPC Discord (4 req / 20s).

* **Pure Obsidian UI:**  
  Dibangun dengan PySide6 (Qt6) bertema *Obsidian Dark* yang bersih, kontras tinggi, dan ringan di hardware tanpa *visual glitch*.

---

## 🏗️ Alur Prioritas Engine (P1 – P6)

OmniPresence membagi aktivitas ke dalam 6 tier deterministik:


```

[P1] Manual Override / Privacy Shield
└─► [P2] Compilers & Toolchains (GCC, Clang, MSVC, Rustc)
└─► [P3] Full-screen Games (DirectX / Vulkan)
└─► [P4] IDE & Workspace (VS Code, Visual Studio, Code::Blocks)
└─► [P5] Media & Browsers (Spotify, YouTube, Windows SMTC)
└─► [P6] Idle & Celestial Sync (Win32 LastInputInfo / Cuaca)

```

| Tier | Kategori | Contoh Trigger | Perilaku & Durasi |
| :--- | :--- | :--- | :--- |
| **P1** | **Manual Override** | Toggle tombol Override di UI | Mengunci status kustom pilihanmu dan mengabaikan tier di bawahnya. |
| **P2** | **Toolchain / Build** | `gcc.exe`, `cl.exe`, `cargo.exe`, `ninja` | Aktif selama proses kompilasi berjalan; langsung lepas saat proses selesai. |
| **P3** | **Fullscreen Games** | Valorant, Forza, Minecraft, dsb. | Mengambil fokus layar penuh agar status media/IDE tidak menimpa game. |
| **P4** | **IDEs & Editors** | `code.exe`, `codeblocks.exe`, Visual Studio | Aktif saat jendela coding sedang dikerjakan di foreground. |
| **P5** | **Media & Browsers** | YouTube (Chrome/Edge/Brave), Spotify | Menampilkan cover album / thumbnail video dengan metadata bersih. |
| **P6** | **Idle / Celestial** | `GetLastInputInfo()` > 300s | Menampilkan status santai, ikon cuaca lokal dinamis, atau custom idle icon. |

---

## 📁 Struktur Repositori


```

OmniPresence/
├── assets/               # Ikon aplikasi, artwork, dan tema styling QSS
├── config/
│   ├── rules.json        # Basis data aturan deteksi proses, regex, dan tier
│   └── settings.json     # Konfigurasi aplikasi pengguna
├── core/
│   ├── ipc_manager.py    # Handler Discord IPC native named pipe socket
│   ├── priority_engine.py# Engine arbitrase tier prioritas & template parser
│   ├── process_watcher.py# WinEvent hook & pelacak jendela aktif foreground
│   ├── settings_manager.py # Pengelola konfigurasi & gateway ID terpusat
│   └── updater.py        # Worker auto-updater & GitHub Releases checker
├── extractors/
│   ├── celestial_sync.py # Sinkronisasi cuaca & status idle dinamis
│   ├── media_scrobbler.py# Windows SMTC extractor & normalisasi metadata musik
│   └── youtube_resolver.py# Async parser thumbnail YouTube tanpa API key
├── installer/
│   └── installer.iss     # Skrip kompilasi installer Inno Setup
├── ui/
│   ├── discord_preview.py# Widget preview profile card Discord secara real-time
│   └── main_window.py    # Antarmuka dashboard, kontrol tier, dan update banner
├── build.bat             # Skrip build binary standalone PyInstaller
├── main.py               # Entrypoint aplikasi utama
└── requirements.txt      # Dependensi Python

```

---

## 🚀 Cara Penggunaan

### Opsi 1: Menggunakan Installer (Rekomendasi Pengguna)
Cukup unduh installer terbaru di tab **[Releases](https://github.com/Raf446/OmniPresence/releases)**:
1. Jalankan `OmniPresence-Setup-v2.3.1.exe`.
2. Ikuti wizard instalasi sampai selesai.
3. Buka OmniPresence dari Desktop atau Start Menu. Aplikasi akan otomatis terhubung ke Discord dan memeriksa pembaruan di latar belakang.

### Opsi 2: Menjalankan dari Source Code (Developer)
Pastikan kamu menggunakan **Windows 10/11 (64-bit)** dan **Python 3.11+**:

```powershell
# 1. Clone repositori
git clone [https://github.com/Raf446/OmniPresence.git](https://github.com/Raf446/OmniPresence.git)
cd OmniPresence

# 2. Buat virtual environment & install dependensi
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 3. Jalankan smoke-test untuk verifikasi modul
python main.py --smoke-test 2.0

# 4. Jalankan aplikasi
python main.py

```

---

## 📦 Build Standalone & Installer Sendiri

Jika ingin membuat binary `.exe` dan file setup installer secara lokal:

1. **Build Standalone Exe:**
```powershell
.\build.bat

```


*File binary akan di-generate di folder `dist\`.*
2. **Kompilasi Setup Installer:**
Buka file `installer/installer.iss` menggunakan [Inno Setup 6](https://jrsoftware.org/isdl.php), lalu tekan `Ctrl + F9` (Compile). Installer setup siap pakai akan otomatis berada di direktori `dist_installer\`.

---

## 📄 Lisensi

Proyek ini dirilis di bawah lisensi [MIT License](https://www.google.com/search?q=mit_license.md). Bebas digunakan, dimodifikasi, dan didistribusikan.

```