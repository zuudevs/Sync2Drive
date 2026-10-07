import os
import time
import logging
import sqlite3
import threading
import queue
from pathlib import Path
from dotenv import load_dotenv
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("Sync2Drive")

SCOPES = ['https://www.googleapis.com/auth/drive.file']
SCRIPT_DIR = Path(__file__).resolve().parent

class SyncStateDB:
    def __init__(self, db_path):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS file_map (
                rel_path TEXT PRIMARY KEY,
                drive_file_id TEXT NOT NULL
            )
        ''')
        self.conn.commit()

    def get_id(self, rel_path):
        cur = self.conn.execute('SELECT drive_file_id FROM file_map WHERE rel_path = ?', (rel_path,))
        row = cur.fetchone()
        return row[0] if row else None

    def save_id(self, rel_path, drive_file_id):
        self.conn.execute('INSERT OR REPLACE INTO file_map (rel_path, drive_file_id) VALUES (?, ?)', (rel_path, drive_file_id))
        self.conn.commit()

    def update_path(self, old_rel_path, new_rel_path):
        self.conn.execute('UPDATE file_map SET rel_path = ? WHERE rel_path = ?', (new_rel_path, old_rel_path))
        self.conn.commit()

def load_ignore_paths(ignore_file):
    ignores = set()
    if os.path.exists(ignore_file):
        with open(ignore_file, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    ignores.add(line)
    return ignores

def is_ignored(filepath, ignores):
    parts = Path(filepath).parts
    for ignore in ignores:
        if ignore in parts:
            return True
    return False

def get_drive_service(creds_path, token_path):
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, 'w') as token:
            token.write(creds.to_json())
            
    return build('drive', 'v3', credentials=creds)

class DriveSyncEngine:
    def __init__(self, service, root_folder_id, db):
        self.service = service
        self.root_folder_id = root_folder_id
        self.db = db
        self.folder_cache = {".": root_folder_id, "": root_folder_id}

    def get_or_create_drive_folder(self, rel_dir):
        if rel_dir in self.folder_cache:
            return self.folder_cache[rel_dir]

        parts = Path(rel_dir).parts
        current_parent_id = self.root_folder_id
        current_rel_path = Path("")

        for part in parts:
            current_rel_path = current_rel_path / part
            path_str = str(current_rel_path).replace("\\", "/")

            if path_str in self.folder_cache:
                current_parent_id = self.folder_cache[path_str]
                continue

            query = f"name='{part}' and '{current_parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
            results = self.service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
            items = results.get('files', [])

            if not items:
                metadata = {'name': part, 'parents': [current_parent_id], 'mimeType': 'application/vnd.google-apps.folder'}
                folder = self.service.files().create(body=metadata, fields='id').execute()
                current_parent_id = folder.get('id')
                logger.info(f"Direktori baru dibuat di Drive: {path_str}")
            else:
                current_parent_id = items[0]['id']

            self.folder_cache[path_str] = current_parent_id

        return current_parent_id

    def process_sync(self, local_path, target_dir):
        """Menangani Create & Modify"""
        if not os.path.exists(local_path):
            return

        filename = os.path.basename(local_path)
        rel_path = os.path.relpath(local_path, target_dir).replace("\\", "/")
        rel_dir = os.path.dirname(rel_path).replace("\\", "/")
        
        parent_id = self.get_or_create_drive_folder(rel_dir)
        drive_file_id = self.db.get_id(rel_path)
        media = MediaFileUpload(local_path, resumable=True)

        if not drive_file_id:
            file_metadata = {'name': filename, 'parents': [parent_id]}
            file_obj = self.service.files().create(body=file_metadata, media_body=media, fields='id').execute()
            self.db.save_id(rel_path, file_obj['id'])
            logger.info(f"Berhasil mengunggah file baru: {rel_path}")
        else:
            self.service.files().update(fileId=drive_file_id, media_body=media).execute()
            logger.info(f"Berhasil memperbarui isi file: {rel_path}")

    def process_rename(self, src_path, dest_path, target_dir):
        """Menangani Rename: Ganti nama via metadata, BUKAN hapus-upload!"""
        old_rel_path = os.path.relpath(src_path, target_dir).replace("\\", "/")
        new_rel_path = os.path.relpath(dest_path, target_dir).replace("\\", "/")
        new_filename = os.path.basename(dest_path)

        drive_file_id = self.db.get_id(old_rel_path)
        
        if drive_file_id:
            # Update metadata nama di Google Drive
            file_metadata = {'name': new_filename}
            self.service.files().update(fileId=drive_file_id, body=file_metadata).execute()
            
            # Update path di lokal Database
            self.db.update_path(old_rel_path, new_rel_path)
            logger.info(f"Berhasil mengganti nama file di Drive: {old_rel_path} -> {new_rel_path}")
        else:
            # Kalau di DB nggak ada, fallback anggap sebagai file baru
            logger.warning(f"File lama tidak ditemukan di DB. Mengunggah sebagai file baru: {new_rel_path}")
            self.process_sync(dest_path, target_dir)

task_queue = queue.Queue()

class SyncHandler(FileSystemEventHandler):
    def __init__(self, target_dir, ignores):
        self.target_dir = target_dir
        self.ignores = ignores

    def on_modified(self, event):
        if event.is_directory or is_ignored(event.src_path, self.ignores): return
        task_queue.put({'action': 'sync', 'path': event.src_path})
        logger.info(f"Antrian [Sync]: {os.path.relpath(event.src_path, self.target_dir)}")

    def on_created(self, event):
        if event.is_directory or is_ignored(event.src_path, self.ignores): return
        task_queue.put({'action': 'sync', 'path': event.src_path})
        logger.info(f"Antrian [Baru]: {os.path.relpath(event.src_path, self.target_dir)}")

    def on_moved(self, event):
        if event.is_directory or is_ignored(event.dest_path, self.ignores): return
        task_queue.put({
            'action': 'rename', 
            'src_path': event.src_path, 
            'dest_path': event.dest_path
        })
        logger.info(f"Antrian [Rename]: {os.path.basename(event.src_path)} -> {os.path.basename(event.dest_path)}")

def sync_worker(engine, target_dir):
    """Background Thread untuk mengeksekusi antrian Google Drive API"""
    while True:
        task = task_queue.get()
        if task is None: 
            break
            
        try:
            time.sleep(1)
            if task['action'] == 'sync':
                engine.process_sync(task['path'], target_dir)
            elif task['action'] == 'rename':
                engine.process_rename(task['src_path'], task['dest_path'], target_dir)
        except Exception as e:
            logger.error(f"Gagal memproses task {task}: {str(e)}")
        finally:
            task_queue.task_done()

def main():
    load_dotenv(SCRIPT_DIR / ".env")
    
    FOLDER_ID = os.getenv("DRIVE_FOLDER_ID")
    CREDS_PATH = str(SCRIPT_DIR / "credentials.json")
    TOKEN_PATH = str(SCRIPT_DIR / "token.json")
    DB_PATH = str(SCRIPT_DIR / "sync_state.db")
    
    TARGET_DIR = os.getenv("TARGET_SYNC_DIR", str(SCRIPT_DIR.parent.parent))
    target_dir_abs = os.path.abspath(TARGET_DIR)
    IGNORE_FILE = str(SCRIPT_DIR / ".ignorepath")
    
    if not FOLDER_ID:
        logger.error("Kredensial DRIVE_FOLDER_ID tidak ditemukan pada berkas konfigurasi .env.")
        return

    logger.info("Memulai layanan Sync2Drive (Mode Tahan Banting + SQLite).")
    ignores = load_ignore_paths(IGNORE_FILE)
    
    try:
        service = get_drive_service(CREDS_PATH, TOKEN_PATH)
        db = SyncStateDB(DB_PATH)
        engine = DriveSyncEngine(service, FOLDER_ID, db)
        logger.info("Otentikasi & Database berhasil diinisiasi.")
    except Exception as e:
        logger.error(f"Gagal inisialisasi: {str(e)}")
        return

    worker_thread = threading.Thread(target=sync_worker, args=(engine, target_dir_abs), daemon=True)
    worker_thread.start()
    
    event_handler = SyncHandler(target_dir_abs, ignores)
    observer = Observer()
    observer.schedule(event_handler, target_dir_abs, recursive=True)
    observer.start()
    
    logger.info(f"Sistem pemantauan aktif pada: {target_dir_abs}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Sinyal interupsi diterima. Menyelesaikan sisa antrian...")
        observer.stop()
        task_queue.put(None)
        worker_thread.join()
        logger.info("Layanan Sync2Drive dihentikan dengan aman.")
    
    observer.join()

if __name__ == '__main__':
    main()