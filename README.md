<div align="center">

# OmniPresence `v2.4.1`

**Discord Rich Presence otomatis, ringan, dan reaktif untuk Windows.**

[![Version](https://img.shields.io/badge/Version-v2.4.1-5865F2?style=flat-square&logo=discord&logoColor=white)](https://github.com/Raf446/OmniPresence/releases)
[![License](https://img.shields.io/badge/License-MIT-22C55E?style=flat-square)](mit_license.md)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)](#cara-menjalankan--build)
[![Platform](https://img.shields.io/badge/Platform-Windows-0078D6?style=flat-square&logo=windows&logoColor=white)](#cara-menjalankan--build)

</div>

---

## Tentang OmniPresence

**OmniPresence** adalah aplikasi desktop yang secara otomatis menampilkan aktivitas kamu di **VLC Media Player**, **Spotify**, **YouTube**, **VS Code**, hingga **Game** langsung ke status profil Discord. Dirancang agar sangat ringan dan responsif, OmniPresence memperbarui status *Play/Pause* maupun perpindahan lagu/video secara instan tanpa delay dan bebas dari pemblokiran *rate-limit* Discord.

---

## Fitur Utama

- **Dynamic Multi-Client ID**: Status profil Discord berganti secara otomatis (*"Watching VLC Media Player"*, *"Watching YouTube"*, *"Listening to Spotify"*, atau *"OmniPresence"*) sesuai aktivitas yang sedang berjalan tanpa perlu menyalin atau memasukkan Client ID secara manual.
- **Integrasi VLC & Auto-Fetch Poster**: Menarik poster resmi anime, film, atau serial TV secara otomatis melalui **AniList** dan **TVMaze** lengkap dengan *in-memory cache*, serta *fallback* mulus ke logo bawaan VLC jika poster tidak ditemukan.
- **Custom Paused Artwork**: Saat video dijeda (*Paused*), tampilan gambar besar otomatis berubah ke karakter **`pause`** dan penghitung durasi berhenti seketika tanpa memutus koneksi Discord.
- **Zero-Delay & Anti Rate-Limit**: Deteksi cepat **250ms** yang hanya mengirim pembaruan saat terjadi perubahan status (*Play/Pause*, *seek* waktu, atau ganti tayangan) tanpa spam paket setiap detik, didukung sinkronisasi berkala yang aman setiap **40 detik**.
- **Modern Glass UI**: Antarmuka desktop modern dengan *live preview* kartu Discord secara langsung dan kontrol privasi cepat.

---

## Panduan Setup VLC Media Player (Wajib)

Agar OmniPresence dapat membaca judul dan durasi video dari VLC secara akurat, aktifkan fitur **Web Interface** di VLC satu kali saja:

1. Buka **VLC Media Player** $\rightarrow$ tekan `Ctrl + P` (*Tools* $\rightarrow$ *Preferences*).
2. Pada pojok kiri bawah, ubah **Show settings** menjadi **All**.
3. Masuk ke **Interface** $\rightarrow$ **Main interfaces**, lalu centang **Web**.
4. Masuk ke **Interface** $\rightarrow$ **Main interfaces** $\rightarrow$ **Lua**, lalu pada bagian **Lua HTTP**:
   - **Password**: `omnipresence`
   - **Port**: `8080`
5. Klik **Save**, lalu **restart VLC Media Player**.

---

## Discord Developer Portal Assets

Jika menggunakan konfigurasi aset kustom pada Discord Developer Portal, pastikan 2 *asset key* berikut tersedia:

| Asset Key | Fungsi |
| :--- | :--- |
| **`vlc`** | Logo kerucut VLC (digunakan sebagai gambar default/fallback dan ikon kecil). |
| **`pause`** | Karakter visual jeda yang tampil sebagai gambar utama saat video berstatus *Paused*. |

---

## Cara Menjalankan & Build

### Menjalankan dari Source Code
```powershell
git clone https://github.com/Raf446/OmniPresence.git
cd OmniPresence

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

python main.py
```

### Build Executable (`.exe`) & Installer
```powershell
# Build file executable mandiri (.exe)
python build_exe.py

# Buat paket installer Windows (membutuhkan Inno Setup Compiler)
iscc installer.iss
```

---

## Lisensi & Kredit

Dikembangkan oleh **[Raf446](https://github.com/Raf446)** di bawah lisensi **[MIT](mit_license.md)**.
