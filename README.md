# [Telegram Backup Tool] TML Documentation

## Overview
This tool allows you to recover deleted messages and media from Telegram channels and groups. It provides features to export all messages, only media files, or only text messages within a specific ID range.

---

## 📦 Setting Up the Environment

### Installing and Configuring a Virtual Environment (venv)
`venv` is an isolated environment for Python projects. It allows you to install dependencies without affecting the global Python installation on your device.

This is especially important when working with multiple projects that require different library versions.

### 💻 How to Create a Virtual Environment

Open the terminal (bash/zsh), which can be done directly inside VS Code.

#### Creating a virtual environment:
```bash
$ python3 -m venv venv
```
Here, `venv` is the directory name that will contain your virtual environment. You can choose any name for this folder.

#### Activating the virtual environment:
**MacOS/Linux:**
```bash
$ source venv/bin/activate
```
**Windows:**
```bash
$ .\venv\Scripts\activate
```

#### Deactivating the virtual environment:
```bash
$ deactivate
```

### 📂 Installing Project Dependencies
Ensure the virtual environment is activated before installing dependencies:
```bash
$ pip3 install -r requirements.txt
```
Once dependencies are installed, you can run the Python scripts.

## 🛠️ Configuration & Environment Variables

To make running the scripts seamless, we use a `.env` file to store your credentials securely.

1. **Rename the example file:** 
   Rename `.env.example` to `.env`.
2. **Fill it out:**
   Open `.env` and fill in the values:
   ```ini
API_ID=YOUR_API_ID
API_HASH=YOUR_API_HASH
SOURCE_GROUP_ID=-100YOUR_SOURCE_GROUP_ID
DESTINATION_GROUP_ID=-100YOUR_DESTINATION_GROUP_ID
BOT_TOKEN=YOUR_BOT_TOKEN
USE_BOT_FOR_DOWNLOAD=false
```

- **`API_ID` & `API_HASH`**: Get these from [my.telegram.org](https://my.telegram.org)
- **`SOURCE_GROUP_ID`**: The channel/group containing the deleted messages.
- **`DESTINATION_GROUP_ID`**: The channel/group where messages will be resent.
- **`BOT_TOKEN` (Optional)**: A Telegram Bot token. Recommended for uploading to prevent your user account from being banned.
- **`USE_BOT_FOR_DOWNLOAD` (Optional)**: Set to `true` to also use the bot for downloading (requires adding the bot as an admin to the Source Group).

### 🔑 Steps to Obtain `API_ID` and `API_HASH`
1. Go to the [Telegram API website](https://my.telegram.org/).
2. Log in with your phone number and confirm authentication.
3. Click on **API development tools**.
4. Your `API_ID` and `API_HASH` will be displayed on the page.

---

## 🚀 Starting the Unified Sync (Backup & Resend)

Run the unified backup module with the following command:
```bash
python -m src.backup
```

**One-Line Execution (Optional CLI Arguments):**
If you want to completely skip all interactive terminal questions, you can pass arguments directly.
```bash
python -m src.backup --mode 1 --min-id 0 --max-id 0 --auto-resend
```

**Available CLI Flags:**
- `--mode`: `1` (All), `2` (Media), `3` (Text)
- `--min-id`: Minimum message ID to export (default: 0)
- `--max-id`: Maximum message ID to export (default: 0)
- `--auto-resend`: If included, automatically streams the downloads to your `DESTINATION_GROUP_ID`.

If you properly filled out your `.env` file, the script will automatically read your credentials.

**First-Time Login:**
The first time you run this, Telegram will ask for your phone number and a 2FA login code. Once completed, a persistent `tg_session.session` file is created, so you will never be asked to login again on future runs.

### 🔄 The Parallel Pipeline
The script will prompt:
`Do you want to automatically resend downloaded messages to a destination group? (y/n):`

If you type `y`, the tool launches into a highly-efficient parallel pipeline:
1. It fetches all deleted messages from the `SOURCE_GROUP_ID` and sorts them chronologically (oldest first).
2. It starts downloading the oldest file.
3. The moment a file finishes downloading, it instantly begins uploading it to the `DESTINATION_GROUP_ID` in the background.
4. Concurrently, while uploading that file, it is already downloading the *next* file in the list.

### 📊 Export Modes
You can choose from the following export modes:
- Export **all messages and media (1)**.
- Export **only media files (2)**.
- Export **only text messages (3)**.

---

## ✂️ 2GB Large File Splitting

Telegram limits individual file uploads to 2GB. 
To ensure your massive backups (e.g., a 5GB `.zip` file) aren't interrupted:
1. The script actively monuments the size of every downloaded file. 
2. If it exceeds 1.95GB, the script automatically uses pure Python byte-splitting to cut the file into `file.zip.part1`, `file.zip.part2`, etc.
3. It uploads these individual chunks to your destination group and deletes the massive original file to save your disk space.

### 🧩 How to Re-Assemble Large `.part` Files (Windows)
Because the tool splits files faithfully at the byte-level, you do not need 7Zip or external apps to put them back together later.
1. Download all the `.part` files from Telegram into a single folder.
2. Open Command Prompt (`cmd`) in that folder.
3. Combine them using the native Windows copy command:
   ```cmd
   copy /B awesome_backup.zip.part1+awesome_backup.zip.part2 restored_awesome_backup.zip
   ```
4. You can now open/extract `restored_awesome_backup.zip` normally!

---

## 📥 Output and Recovery Management
The recovered media files and `dump_unified.json` will be stored in the `backup_will_be_inside_me` folder, which is automatically created in the working directory during recovery.

If you chose NOT to auto-resend during the backup, you can independently re-run the upload process later using the secondary resender script:
```bash
python -m src.resender
```

