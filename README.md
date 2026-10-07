# Sync2Drive

Sync2Drive is a lightweight, robust, and asynchronous local-to-Google-Drive folder synchronization tool. Built with Python, it monitors local file system changes in real-time and mirrors them to a specified Google Drive folder.

## Features

- **Asynchronous Queue Processing:** Uses a dedicated background worker thread to process uploads, ensuring the file system watcher (`watchdog`) never freezes during heavy I/O operations.
- **Smart Rename Tracking:** Renaming a file locally updates the metadata on Google Drive instantly without requiring a re-upload, saving bandwidth and time.
- **Local State Database:** Utilizes a lightweight SQLite database (`sync_state.db`) to map local file paths to their respective Google Drive File IDs.
- **Custom Exclusions (`.ignorepath`):** Supports a custom exclusion file similar to `.gitignore` to prevent syncing of temporary files, builds, or sensitive data.
- **Auto-Directory Mapping:** Automatically preserves and creates your local directory hierarchy inside the target Google Drive folder.

## 🛠️ Prerequisites

Ensure you have Python 3.8+ installed. Install the required dependencies:

```bash
pip install -r requirements.txt
```

You will also need a **Google Drive API OAuth 2.0 Client ID**. Download the JSON file and rename it to `credentials.json`.

## Configuration

Create the necessary configuration files in the `sync2drive/` directory (or wherever the script resides).

### 1. Environment Variables (`.env`)

Create a `.env` file to define your target folders:

```env
# The ID of the target folder in Google Drive (found in the URL)
DRIVE_FOLDER_ID=1A2B3C4D5E6F7G8H9I0J

# (Optional) Target local directory to sync. 
# Defaults to two levels up from the script location if not specified.
TARGET_SYNC_DIR="YOUR-PATH"

```

### 2. Ignore Paths (`.ignorepath`)

Define any folders or files you want to exclude from the sync process. **Note: Write exact folder names, without slashes.**

```ignorepath
# Ignore version control
.git

# Ignore Python cache
__pycache__

# Ignore specific folders
scripts
.vscode

```

## Usage

Run the script from your project's root directory:

```bash
python sync2drive/main.py

```

Upon first run:

1. A browser window will open asking for Google Drive authorization.
2. Once authorized, a `token.json` file will be generated automatically to handle future sessions.
3. An SQLite database (`sync_state.db`) will be created to track file IDs.

## Recommended Project Structure

```text
sync2drive\
	├── .env
	├── .ignorepath
	├── credentials.json
	├── sync2drive.py
	├── sync_state.db       # Auto-generated
	└── token.json          # Auto-generated

```