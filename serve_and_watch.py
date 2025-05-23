#!/usr/bin/env python3
import http.server
import socketserver
import os
import time
import threading
import subprocess
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# Configuration
PORT = 8000
DIRECTORY = os.path.dirname(os.path.abspath(__file__))
WATCH_DIR = os.path.join(DIRECTORY, "posts_md")

class PostChangeHandler(FileSystemEventHandler):
    def on_modified(self, event):
        if not event.is_directory and event.src_path.endswith(".md"):
            print(f"Change detected in {event.src_path}")
            rebuild_site()
    
    def on_created(self, event):
        if not event.is_directory and event.src_path.endswith(".md"):
            print(f"New file detected: {event.src_path}")
            rebuild_site()

def rebuild_site():
    print("Rebuilding site...")
    subprocess.run(["python", "site_builder.py"], cwd=DIRECTORY)
    print("Site rebuilt successfully")

def start_http_server():
    os.chdir(DIRECTORY)
    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("", PORT), handler) as httpd:
        print(f"Serving at http://localhost:{PORT}")
        httpd.serve_forever()

def start_file_watcher():
    event_handler = PostChangeHandler()
    observer = Observer()
    observer.schedule(event_handler, WATCH_DIR, recursive=False)
    observer.start()
    print(f"Watching for changes in {WATCH_DIR}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()

if __name__ == "__main__":
    # Start HTTP server in a separate thread
    server_thread = threading.Thread(target=start_http_server)
    server_thread.daemon = True
    server_thread.start()
    
    # Start file watcher in the main thread
    start_file_watcher()