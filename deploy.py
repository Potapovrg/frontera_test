#!/usr/bin/env python3
"""
deploy.py
=========
Deploy this project folder to the OrangePi (which has the Frontera device
attached as /dev/ttyACM0) over SSH/SFTP using paramiko, then run a command
remotely and stream its output.

Run from the WSL side:

    python3 deploy.py                       # upload + run: python3 run_scan.py
    python3 deploy.py --run "python3 run_scan.py --start 100 --stop 6000 --step 1"
    python3 deploy.py --no-run              # upload only
    python3 deploy.py --run "python3 app.py --port 8080" --detach
                                             # upload + start server in the background,
                                             # survives SSH disconnect, logs to server.log

Defaults target host 192.168.10.2 (orangepi/orangepi).
"""
from __future__ import annotations

import argparse
import os
import posixpath
import stat
import sys

import paramiko

# --- deployment config -----------------------------------------------------
HOST = "192.168.10.2"
USER = "orangepi"
PASSWORD = "orangepi"
REMOTE_DIR = "/home/orangepi/frontera_test"
DEFAULT_RUN = "python3 run_scan.py"

# Files/dirs never uploaded.
EXCLUDE = {".claude", ".git", "__pycache__", ".gitignore", ".pytest_cache", "results"}

LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))


def _iter_files(root: str):
    """Yield (local_path, rel_path) for every file to upload."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE]
        for name in filenames:
            if name in EXCLUDE or name.endswith(".pyc"):
                continue
            local = os.path.join(dirpath, name)
            rel = os.path.relpath(local, root)
            yield local, rel.replace(os.sep, "/")


def _sftp_mkdirs(sftp: paramiko.SFTPClient, remote_dir: str) -> None:
    """Recursively create remote_dir (like mkdir -p)."""
    parts = remote_dir.strip("/").split("/")
    cur = ""
    for p in parts:
        cur += "/" + p
        try:
            sftp.stat(cur)
        except FileNotFoundError:
            sftp.mkdir(cur)


def upload(sftp: paramiko.SFTPClient) -> int:
    _sftp_mkdirs(sftp, REMOTE_DIR)
    count = 0
    for local, rel in _iter_files(LOCAL_DIR):
        remote = posixpath.join(REMOTE_DIR, rel)
        _sftp_mkdirs(sftp, posixpath.dirname(remote))
        sftp.put(local, remote)
        # preserve executable bit for .py entry points
        mode = os.stat(local).st_mode
        sftp.chmod(remote, stat.S_IMODE(mode))
        print(f"  ↑ {rel}")
        count += 1
    return count


def run_remote(client: paramiko.SSHClient, command: str) -> int:
    full = f"cd {REMOTE_DIR} && {command}"
    print(f"\n$ ({HOST}) {full}\n" + "-" * 60)
    stdin, stdout, stderr = client.exec_command(full, get_pty=True)
    # stream combined output live
    for line in iter(stdout.readline, ""):
        sys.stdout.write(line)
        sys.stdout.flush()
    err = stderr.read().decode(errors="replace")
    if err.strip():
        sys.stderr.write(err)
    return stdout.channel.recv_exit_status()


def run_remote_detached(client: paramiko.SSHClient, command: str,
                         log_file: str = "server.log") -> None:
    """Start a long-running command on the Pi that survives SSH disconnect."""
    full = (
        f"cd {REMOTE_DIR} && nohup {command} > {log_file} 2>&1 < /dev/null & "
        f"echo STARTED_PID:$!"
    )
    print(f"\n$ ({HOST}) {full}\n" + "-" * 60)
    _stdin, stdout, stderr = client.exec_command(full)
    print(stdout.read().decode(errors="replace").strip())
    err = stderr.read().decode(errors="replace")
    if err.strip():
        sys.stderr.write(err)
    print(f"detached; tail remote {REMOTE_DIR}/{log_file} to see server output")


def main() -> int:
    ap = argparse.ArgumentParser(description="Deploy project to OrangePi and run.")
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--run", default=DEFAULT_RUN, help="remote command to run")
    ap.add_argument("--no-run", action="store_true", help="upload only")
    ap.add_argument("--detach", action="store_true",
                    help="start --run in the background (nohup) and return immediately")
    args = ap.parse_args()

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(args.host, username=USER, password=PASSWORD, timeout=15)
    print(f"connected {USER}@{args.host}")

    try:
        sftp = client.open_sftp()
        print(f"uploading -> {REMOTE_DIR}")
        n = upload(sftp)
        sftp.close()
        print(f"uploaded {n} file(s)")

        if args.no_run:
            return 0
        if args.detach:
            run_remote_detached(client, args.run)
            return 0
        rc = run_remote(client, args.run)
        print("-" * 60 + f"\nremote exit code: {rc}")
        return rc
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
