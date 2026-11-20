
import argparse
import base64
import configparser
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import sys
import tempfile


@dataclass
class Change:
    path: Path
    before: bytes | None
    after: bytes
    reason: str
    before_link: str | None = None


def read_state(path):
    link = os.readlink(path) if path.is_symlink() else None
    return (path.read_bytes() if path.exists() else None, link)


def desktop_entry(data):
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    parser.read_string(data.decode("utf-8"))
    return dict(parser["Desktop Entry"])


def add_desktop_keys(data, additions):
    """Keep the exported Exec, translations, comments and other groups intact."""
    lines = data.decode("utf-8").splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.strip() == "[Desktop Entry]")
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].lstrip().startswith("[")), len(lines))
    if end and not lines[end - 1].endswith("\n"):
        lines[end - 1] += "\n"
    lines[end:end] = [f"{key}={value}\n" for key, value in additions.items()]
    return "".join(lines).encode("utf-8")


def plan_applets(data_home, flatpak_roots):
    changes, seen = [], set()
    for root in flatpak_roots:  # User installation wins over system installation.
        for inner in sorted((root / "app").glob(
                "*/current/active/files/share/applications/*.desktop")):
            original = desktop_entry(inner.read_bytes())
            if original.get("X-CosmicApplet") != "true" or inner.name in seen:
                continue
            exported = root / "exports/share/applications" / inner.name
            if not exported.is_file():
                continue
            seen.add(inner.name)
            export_data = exported.read_bytes()  # Flatpak's export is normally a symlink.
            export_entry = desktop_entry(export_data)
            app_id = inner.relative_to(root / "app").parts[0]
            if export_entry.get("X-Flatpak") != app_id:
                raise ValueError(f"Export does not identify installed Flatpak {app_id}: {exported}")
            shadow = data_home / "applications" / inner.name
            if shadow.exists() or shadow.is_symlink():
                raise ValueError(f"A local override shadows the Flatpak launch path: {shadow}. "
                                 "Undo the previous local-copy workaround with its backup manifest first.")
            destination = exported
            before, before_link = read_state(destination)
            source = export_data
            entry = export_entry
            additions = {key: value for key, value in original.items()
                         if (key.startswith("X-Cosmic") or key in {
                             "X-HostWaylandDisplay", "X-OverflowMinSize",
                             "X-OverflowPriority", "X-NotificationsApplet"})
                         and key not in entry}
            if not additions:
                continue
            if not os.access(destination.parent, os.W_OK):
                raise ValueError(f"Flatpak export directory is not writable as your user: {destination.parent}. "
                                 "This workaround requires a user-installed applet.")
            after = add_desktop_keys(source, additions)
            assert desktop_entry(after).get("Exec") == entry.get("Exec")
            changes.append(Change(destination, before, after,
                                  f"{original.get('Name', app_id)}: restore " + ", ".join(additions),
                                  before_link))
    return changes


def digest(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def write_atomic(path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".cosmic-repair-", delete=False) as f:
            temporary = Path(f.name)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        temporary.chmod(mode)
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def write_symlink_atomic(path, target):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=path.parent, prefix=".cosmic-repair-link-") as directory:
        temporary = Path(directory) / "entry"
        temporary.symlink_to(target)
        temporary.replace(path)


def apply_changes(changes, backup_root):
    if not changes:
        return None
    records = []
    for change in changes:
        if read_state(change.path) != (change.before, change.before_link):
            raise ValueError(f"File changed after preview: {change.path}")
        records.append({
            "path": str(change.path), "reason": change.reason,
            "before": base64.b64encode(change.before).decode() if change.before is not None else None,
            "before_link": change.before_link,
            "after_sha256": digest(change.after),
            "mode": stat.S_IMODE(change.path.stat().st_mode) if change.before is not None else 0o644,
        })
    backup_root.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-"),
                                      dir=backup_root))
    directory.chmod(0o700)
    manifest = directory / "manifest.json"
    write_atomic(manifest, json.dumps({"version": 2, "files": records}, indent=2).encode(), 0o600)
    print(f"Backup manifest: {manifest}", flush=True)
    print(f"Undo: python3 {shlex.quote(str(Path(__file__).resolve()))} --restore {shlex.quote(str(manifest))} --apply",
          flush=True)
    for change, record in zip(changes, records):
        if read_state(change.path) != (change.before, change.before_link):
            raise ValueError(f"File changed during apply; use the backup manifest: {change.path}")
        write_atomic(change.path, change.after, record["mode"])
    return manifest


def restore(manifest, apply):
    payload = json.loads(manifest.read_text())
    if payload.get("version") not in (1, 2):
        raise ValueError("Unsupported backup manifest")
    pending = []
    for record in payload["files"]:
        path = Path(record["path"])
        before = base64.b64decode(record["before"], validate=True) if record["before"] is not None else None
        before_link = record.get("before_link")
        current, current_link = read_state(path)
        if (current_link == before_link and
                (current == before or before_link is not None)):
            continue  # Also handles an interrupted apply that never wrote this file.
        if current_link is not None or digest(current) != record["after_sha256"]:
            raise ValueError(f"File changed since the workaround; refusing to overwrite: {path}")
        pending.append((path, before, before_link, record["mode"], current))
    for path, before, before_link, mode, expected_current in reversed(pending):
        print(f"Restore: {path}")
        if apply:
            if read_state(path) != (expected_current, None):
                raise ValueError(f"File changed during restoration: {path}")
            if before_link is not None:
                write_symlink_atomic(path, before_link)
            elif before is None:
                path.unlink()
            else:
                write_atomic(path, before, mode)
    print(f"{'Restored' if apply else 'Would restore'} {len(pending)} file(s).")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--applets", action="store_true", help="Restore missing metadata at the Flatpak export paths (default)")
    parser.add_argument("--apply", action="store_true", help="Write the previewed changes, with backups; otherwise preview only")
    parser.add_argument("--restore", type=Path, metavar="MANIFEST", help="Preview restoration of a backup; add --apply to restore")
    parser.add_argument("--backup-dir", type=Path, default=Path(__file__).resolve().parent / "backups")
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error("Run this as your desktop user, without sudo.")
    if args.restore:
        if args.applets:
            parser.error("Choose restore or repairs, not both.")
        restore(args.restore.expanduser().resolve(), args.apply)
        return
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share").expanduser().absolute()
    changes = plan_applets(data_home, [data_home / "flatpak", Path("/var/lib/flatpak")])
    for change in changes:
        print(f"{change.reason}\n  {change.path}")
    print(f"{'Applying' if args.apply else 'Preview only:'} {len(changes)} change(s).")
    if args.apply and changes:
        apply_changes(changes, args.backup_dir.expanduser().resolve())
        print("Applied. Close and reopen COSMIC Settings. Log out and back in if the panel needs a refresh.")
        print("Temporary export repairs may be overwritten by Flatpak updates; retain the backup manifest.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, configparser.Error, KeyError) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
