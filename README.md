<div align="center">

# 🛡️ Telegram Message Lifeguard (TML)
**The Ultimate High-Speed Telegram Backup & Recovery Pipeline**

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Telethon](https://img.shields.io/badge/Telethon-v1.37+-0088cc.svg)](https://docs.telethon.dev/)
[![cryptg](https://img.shields.io/badge/cryptg-Accelerated-brightgreen.svg)](https://github.com/cher-nov/cryptg)

TML is a massively upgraded, parallel-processing script designed to seamlessly fetch, download, backup, and instantly re-upload deleted messages and media from source Telegram groups to your own private destination channels.

</div>

---

## ✨ Why TML?

This project has been rewritten from the ground up to solve the most difficult bottlenecks of Telegram archiving:

- 🚀 **FastTelethon Parallel Chunking**: Uses hardware-accelerated C-bindings (`cryptg`) and multi-connection streaming to smash Telegram's 1-2 MB/s single-connection limit. Downloads and uploads are now **blazingly fast**.
- 🤖 **Dual-Client Account Protection**: Safeguard your personal user account from API bans. Configure TML to download deeply hidden messages with your User Session, while simultaneously uploading them to the destination using an immortal Bot Token.
- ✂️ **Automatic 2GB File Slicing**: Bypasses Telegram's strict 2GB file limit natively in Python. Massive 5GB+ files are split into `.part` chunks via async memory buffering, instantly uploaded, and deleted locally to save your storage drive.
- 🔄 **Smart Resume**: Accidental server crash? TML maintains a unified JSON log. Upon restart, it instantly skips fully uploaded files and avoids re-downloading existing media, saving you hundreds of gigabytes in bandwidth.
- 📊 **Live Telegram Tracker**: Send `/status` to your configured Bot to view a beautifully formatted, auto-refreshing progress bar (WZML-X style) showing active transfers, speeds, and ETAs in real time.
- ⚡ **Selectable Engine Pipelines**: Choose between **Direct Memory Streaming** (pipes data from source server directly to destination server without touching your hard drive) or **Local Disk Backup** (caches files securely before uploading).

---

## 📦 Installation & Setup

### 1. Create a Python Virtual Environment
We highly recommend running TML in an isolated environment so dependencies do not pollute your global system.

**Mac / Linux:**
```bash
python3 -m venv venv
source venv/bin/activate
```

**Windows:**
```powershell
python -m venv venv
.\venv\Scripts\activate
```

### 2. Install Dependencies
Install the required packages, including the custom `cryptg` binding for AES speed acceleration.
```bash
pip install -r requirements.txt
```

---

## ⚙️ Configuration (.env)

Duplicate the `.env.example` file to create your own localized `.env` file for your secret credentials:
```bash
cp .env.example .env
```

Open `.env` and fill it out:

```ini
API_ID=12345678
API_HASH=your_api_hash_here
SOURCE_GROUP_ID=-100YOUR_SOURCE_GROUP
DESTINATION_GROUP_ID=-100YOUR_DESTINATION_GROUP

# --- ADVANCED DUAL CLIENT SYSTEM ---
BOT_TOKEN=12345:ABCDEFG_your_bot_token_here
USE_BOT_FOR_DOWNLOAD=false
```

### How the Dual-Client System Works
If you provide a `BOT_TOKEN`, the script creates two separate persistent session files (`tg_session` and `tg_bot_session`).
- It uses the Bot Token for the **Uploader Worker**, preventing your main account from being restricted for mass-uploading files.
- You can freely use your User Account to download files from locked source channels where a Bot isn't allowed to join.
- *(Optional)* If your Bot *is* an admin in the source group, change `USE_BOT_FOR_DOWNLOAD=true` for maximum speed and safety across both pipelines!

> **Where do I get an API ID?** Log in at [my.telegram.org](https://my.telegram.org/), click "API development tools", and copy your ID and Hash.

---

## 🚀 Running the Pipeline

You can run the Unified Sync tool in Interactive Mode, or skip all questions by passing CLI flags for fully automated server orchestration!

### Interactive Mode
TML features a beautifully designed interactive Terminal Interface.
```bash
python -m src.backup
```

### Automated CLI Mode
```bash
python -m src.backup --mode 1 --min-id 0 --max-id 0 --auto-resend --stream
```
* **`--mode`**: `1` (Export All), `2` (Only Media), `3` (Only Text)
* **`--auto-resend`**: Triggers the upload pipeline to your `DESTINATION_GROUP_ID`.
* **`--stream`**: Forces Direct Memory Streaming (skips saving to local disk).
* **`--disk`**: Forces Local Disk Backup (caches files to hard drive first).

*(Note: On your very first run, TML will ask for your Telegram phone number and 2FA code to generate your local `tg_session.session` file. You will not have to login again.)*

---

## 📊 Live Progress Tracker (`/status`)

If you provided a `BOT_TOKEN` in your `.env`, TML binds a command listener to it. 
Simply open Telegram and send `/status` to your bot.

It will instantly reply with an **auto-refreshing live progress board** detailing every active download, split, and upload happening on your server:

```text
1. Massive_Course_Video.mp4
┟ [⬢⬢⬢⬢⬢⬢⬡⬡⬡⬡⬡⬡] 50.0%
┠ Processed → 975.00MB of 1.95GB
┠ Status → Uploading
┠ Speed → 12.50MB/s
┠ Time → 1m18s elapsed ( ETA 1m18s )
┖ Engine → FastTelethon
```
*(The message automatically updates itself every 5-seconds until all transfers complete!)*

---

## 🧩 Handling 2GB+ Massive Files

If a file exceeds 1.95GB, TML slices it smoothly into `.part1`, `.part2`, etc., making it fit natively within Telegram's upload limits. It does this losslessly at the byte level. 

To merge these files after restoring them, you don't even need 3rd-party software. Just combine them via the command line!

**Windows Command Prompt:**
```cmd
copy /B awesome_backup.zip.part1+awesome_backup.zip.part2 restored_awesome_backup.zip
```

**Mac / Linux Terminal:**
```bash
cat awesome_backup.zip.part* > restored_awesome_backup.zip
```

You can now extract the zip identically as if it had never been split! And the `Resender` module supports `.part` arrays out of the box.

---

## 📥 Manual Resender Module

If you backed up 50GB of files locally using `python -m src.backup` but chose **not** to auto-resend them, they are stored securely in the `backup_will_be_inside_me` folder along with a cleanly formatted `dump.json` file maintaining their exact file names and caption logic.

You can trigger a background batch-upload to your destination group at any time by running:
```bash
python -m src.resender
```
This module hooks into the exact same parallel `FastTelethon` chunked uploader array to securely transfer the files as quickly as Telegram's data-centers allow.
