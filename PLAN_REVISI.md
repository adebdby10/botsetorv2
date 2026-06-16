# Plan Revisi Bot Setor v2

## File yang Diubah: `bot/handler.py`

---

### Revisi 1: Ubah format pesan "ditolak buyer"
**Lokasi:** `event_cb` → case `"rejected"` dan `"late_rejected"` (sekitar baris 520-540)

**Sekarang:**
```python
f"⚠️ <b>{phone}</b> ditolak buyer (dikembalikan)"
```

**Menjadi:**
```python
f"⚠️ <b>{phone}</b> ditolak buyer"
```

**Jumlah lokasi:** 2 tempat (rejected + late_rejected)

---

### Revisi 2: Hapus fungsi `_send_back_problem_sessions` dan pengirim `gagal.zip`
**Lokasi:** 
- Fungsi `_send_back_problem_sessions()` (sekitar baris 440-490) — **HAPUS SELURUHNYA**
- Pemanggilan di `_do_automation()` (sekitar baris 580) — **HAPUS BARIS PANGGILANNYA**

**Sekarang:** Fungsi membuat `gagal.zip` berisi session 2FA ON, Device lain, Unauth, Rejected, Error lalu mengirim ke user.

**Menjadi:** Fungsi dihapus total. Session gagal tetap diarsipkan via `_archive_batch()`, tapi **tidak dikirim kembali ke user**.

**Detail perubahan:**
1. Hapus seluruh fungsi `_send_back_problem_sessions()`
2. Di `_do_automation()`, hapus baris:
   ```python
   counts, categories = await _send_back_problem_sessions(bot, chat_id, user_id)
   ```
3. Ganti perhitungan `n_rejected` dan `n_other_fail` dengan menghitung dari folder langsung (tanpa bikin zip):

```python
# Hitung dari folder langsung
n_rejected = len(list((REJECTED_DIR / str(user_id)).glob("*.session"))) if (REJECTED_DIR / str(user_id)).exists() else 0
n_2fa_on   = len(list((TWO_FA_ON_DIR / str(user_id)).glob("*.session"))) if (TWO_FA_ON_DIR / str(user_id)).exists() else 0
n_device   = len(list((OTHER_DEVICE_DIR / str(user_id)).glob("*.session"))) if (OTHER_DEVICE_DIR / str(user_id)).exists() else 0
n_unauth   = len(list((UNAUTH_DIR / str(user_id)).glob("*.session"))) if (UNAUTH_DIR / str(user_id)).exists() else 0
n_misc     = len(list((SESSIONS_DIR / str(user_id)).glob("*.session"))) if (SESSIONS_DIR / str(user_id)).exists() else 0
n_other_fail = n_2fa_on + n_device + n_unauth + n_misc
```

4. Untuk `_archive_batch()`, tetap pertahankan tapi ubah parameter `failed_categories`:
```python
failed_categories = [
    (TWO_FA_ON_DIR / str(user_id), "2fa_on"),
    (OTHER_DEVICE_DIR / str(user_id), "device"),
    (UNAUTH_DIR / str(user_id), "unauth"),
    (REJECTED_DIR / str(user_id), "rejected"),
]
_archive_batch(user_id, sessions, failed_categories)
```

---

### Revisi 3: Ubah format summary akhir
**Lokasi:** `_do_automation()` — bagian akhir setelah proses selesai (sekitar baris 590-600)

**Sekarang:**
```
Selesai memproses x session ke @botbuyer.
Berhasil : y
Rejected : z
Gagal    : w
```

**Menjadi:**
```
Selesai memproses x session ke @botbuyer.

Berhasil: y
Rejected: z
Gagal: w
  - 2FA ON: a
  - Device lain: b
  - Unauth: c
  - Error: d
```

**Detail perubahan:** Update `final_text` untuk include breakdown gagal per kategori.

---

## Ringkasan Perubahan

| # | Revisi | File | Baris | Aksi |
|---|--------|------|-------|------|
| 1 | Hapus "(dikembalikan)" | `bot/handler.py` | ~520, ~535 | Edit 2 string |
| 2 | Hapus `gagal.zip` | `bot/handler.py` | ~440-490 | Hapus fungsi + refaktor pemanggilan |
| 3 | Summary detail gagal | `bot/handler.py` | ~590-600 | Edit format text |

**File yang diubah:** Hanya 1 file (`bot/handler.py`)
**File lain:** Tidak ada perubahan
