<div align="center">

# OmniPresence `v2.4.1`

**Discord Rich Presence otomatis, cerdas, dan reaktif untuk Windows.**

[![Version](https://img.shields.io/badge/Version-v2.4.1-5865F2?style=for-the-badge&logo=discord&logoColor=white)](https://github.com/Raf446/OmniPresence/releases)
[![License](https://img.shields.io/badge/License-MIT-22C55E?style=for-the-badge)](mit_license.md)
[![Platform](https://img.shields.io/badge/Platform-Windows-0078D6?style=for-the-badge&logo=windows&logoColor=white)](#cara-instalasi--menjalankan-quickstart)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](#cara-instalasi--menjalankan-quickstart)

</div>

---

## Tentang OmniPresence

**OmniPresence** adalah aplikasi desktop yang menghubungkan aktivitas harian di PC kamu—mulai dari menonton anime atau film di **VLC Media Player**, mendengarkan musik di **Spotify**, menonton video **YouTube**, *coding* di **VS Code**, hingga bermain **Game**—langsung ke status profil Discord secara *real-time*.

Berbeda dari *script* Rich Presence biasa yang sering mengalami keterlambatan (*delay*) saat video dijeda atau membuat klien Discord terkena pembatasan *rate-limit*, OmniPresence dirancang agar sangat ringan di latar belakang, merespons perubahan status seketika, dan menjaga tampilan kartu aktivitasmu selalu akurat tanpa membuat Discord *lag*.

---

## Sorotan Fitur Utama

### 1. Dynamic Multi-Client ID (Otomatis Ganti Status)
Kamu tidak perlu lagi repot menyalin atau mengganti Application ID secara manual di menu pengaturan setiap kali berpindah aplikasi. OmniPresence secara cerdas menyesuaikan identitas status Discord kamu sesuai aktivitas yang sedang aktif:

| Aktivitas yang Terdeteksi | Status yang Tampil di Profil Discord |
| :--- | :--- |
| **VLC Media Player** | *Watching **VLC Media Player*** |
| **YouTube (Browser)** | *Watching **YouTube*** |
| **Spotify** | *Listening to **Spotify*** |
| **VS Code / Gaming / Standby** | *Playing / Using **OmniPresence*** |

### 2. Integrasi VLC & Auto-Fetch Poster Online
- **Pencarian Poster Resmi Otomatis**: Membersihkan judul file video dari tag rilis atau resolusi (seperti `[1080p]` atau nomor episode), lalu menarik sampul/poster resmi anime melalui **AniList GraphQL** serta serial TV atau film barat melalui **TVMaze**.
- **Hemat Kuota dengan Cache Lokal**: Poster yang sudah ditemukan disimpan di memori lokal, sehingga saat kamu berpindah ke episode berikutnya pada judul yang sama, aplikasi tidak perlu mengunduh ulang gambar atau memakan kuota internet.
- **Fallback Otomatis**: Jika kamu sedang memutar video rekaman pribadi atau komputer sedang tidak terhubung ke internet (*offline*), tampilan gambar otomatis beralih dengan mulus ke logo kerucut VLC.

### 3. Tampilan Khusus Saat Video Dijeda (*Custom Paused Art*)
Saat kamu menekan tombol jeda (*Pause*) di VLC, status Rich Presence di Discord tidak akan hilang atau terputus. Sebagai gantinya, gambar utama akan langsung berganti menampilkan karakter visual jeda (**`pause`**) dan bar durasi waktu berhenti otomatis hingga kamu melanjutkan pemutaran.

### 4. Bebas Delay & Anti Rate-Limit
- **Respons Play/Pause Instan (`<300ms`)**: Data hanya dikirim ke Discord saat benar-benar terjadi aktivitas nyata—seperti menekan *Play/Pause*, melompati durasi video (*seek* lebih dari 2 detik), atau mengganti tayangan.
- **Animasi Durasi Efisien**: Waktu berjalan dihitung secara mandiri oleh klien Discord tanpa perlu membanjiri koneksi dengan paket data setiap detik, serta dilengkapi sinkronisasi berkala setiap **40 detik** saat video diputar agar posisi menit tetap presisi.

### 5. Modern Glass UI & Privasi
- **Live Discord Card Preview**: Lihat pratinjau langsung tampilan kartu profil Discord kamu lengkap dengan avatar, banner asli, dan sampul media yang sedang aktif.
- **Activity Console & Stealth Mode**: Pantau riwayat perpindahan status lewat konsol aktivitas yang bersih, atau aktifkan tombol **Stealth Mode** kapan saja untuk menyembunyikan seluruh aktivitasmu dengan satu klik.

---

## Panduan Setup VLC Media Player (Wajib)

Agar OmniPresence dapat membaca judul tayangan, status *Play/Pause*, dan durasi waktu dari VLC secara presisi, aktifkan modul **Web Interface** bawaan VLC satu kali saja:

1. Buka **VLC Media Player**, lalu masuk ke menu **Preferences** (`Ctrl + P`).
2. Di pojok kiri bawah pada bagian *Show settings*, pilih opsi **All**.
3. Buka menu **Interface** $\rightarrow$ **Main interfaces**, lalu centang kotak **Web**.
4. Perluas menu di bawahnya ke **Interface** $\rightarrow$ **Main interfaces** $\rightarrow$ **Lua**, lalu pada bagian **Lua HTTP** isikan:
   - **Password**: `omnipresence`
   - **Port**: `8080`
5. Klik **Save**, lalu **tutup dan buka kembali (restart) VLC Media Player**.

> [!TIP]
> Pengaturan ini hanya perlu dilakukan sekali. Setelah disimpan, VLC akan otomatis mengaktifkan antarmuka lokal setiap kali dibuka.

---

## Referensi Aset Discord Developer Portal (Khusus Custom ID)

OmniPresence sudah langsung siap digunakan tanpa konfigurasi tambahan. Namun, jika kamu ingin memakai **Application ID** buatanmu sendiri melalui [Discord Developer Portal](https://discord.com/developers/applications) (menu *Rich Presence* $\rightarrow$ *Art Assets*), pastikan kamu mengunggah 2 aset gambar dengan nama *key* berikut:

| Nama Key Aset | Kegunaan pada Kartu Discord |
| :--- | :--- |
| **`vlc`** | Ikon kerucut VLC yang digunakan sebagai gambar default/*fallback* serta badge kecil di sudut poster. |
| **`pause`** | Gambar visual karakter yang ditampilkan sebagai gambar utama saat video sedang dijeda (*Paused*). |

---

## Cara Instalasi & Menjalankan (*Quickstart*)

Pastikan **Python 3.10+** dan aplikasi **Discord Desktop** sudah terpasang di Windows kamu, lalu jalankan perintah berikut di terminal (PowerShell / CMD):

```powershell
# 1. Clone repositori dan masuk ke folder proyek
git clone https://github.com/Raf446/OmniPresence.git
cd OmniPresence

# 2. Buat dan aktifkan virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 3. Instal seluruh dependensi yang diperlukan
pip install -r requirements.txt

# 4. Jalankan OmniPresence
python main.py
```

---

## Lisensi & Pembuat

Dibuat dan dikelola oleh **[Raf446](https://github.com/Raf446)** di bawah lisensi **[MIT License](mit_license.md)**.
