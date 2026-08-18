import os
import re
import json
import stat
import shutil
import hashlib
import tarfile
import zipfile
import tempfile
import tomllib
import requests

# ---------------------------------------------------------------------------
# Config / environment
# ---------------------------------------------------------------------------
PAGES_URL = os.environ.get("PAGES_URL", "https://your-username.github.io/custom-decky-store")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
CONFIG_FILE = "plugins_config.toml"

SESSION = requests.Session()
SEMVER_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


def get_headers():
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "custom-decky-store",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def calculate_sha256(filepath):
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for block in iter(lambda: f.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Version handling
# ---------------------------------------------------------------------------
def normalize_version(asset_name, tag_name):
    """Return a valid SemVer string so the store never chokes on the version.

    1) First real X.Y[.Z] found in the asset name, then the tag.
    2) Otherwise a dev prerelease derived from the tag, e.g.
       'Dev-20260812-215318-e24d3ab' -> '0.0.0-dev.20260812.215318.e24d3ab'.
       Prerelease keeps chronological ordering so updates still resolve.
    """
    for candidate in (asset_name, tag_name):
        if not candidate:
            continue
        m = SEMVER_RE.search(candidate)
        if m:
            major, minor, patch = m.group(1), m.group(2), m.group(3) or "0"
            return f"{major}.{minor}.{patch}"

    tag = (tag_name or "").lstrip("vV")
    ident = re.sub(r"[^0-9A-Za-z]+", ".", tag).strip(".").lower()
    return f"0.0.0-dev.{ident}" if ident else "0.0.0"


# ---------------------------------------------------------------------------
# Download / extract helpers
# ---------------------------------------------------------------------------
def download(url, dest):
    with SESSION.get(url, headers=get_headers(), stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                if chunk:
                    f.write(chunk)


def extract_archive(archive_path, dest_dir):
    if archive_path.endswith(".tar.gz") or archive_path.endswith(".tgz"):
        with tarfile.open(archive_path, "r:gz") as tar:
            tar.extractall(path=dest_dir)
    elif archive_path.endswith(".zip"):
        with zipfile.ZipFile(archive_path, "r") as z:
            z.extractall(path=dest_dir)
    else:
        raise ValueError(f"Unsupported archive: {archive_path}")


def find_plugin_root(base):
    """Locate the folder that actually holds the plugin (plugin.json wins)."""
    fallback = None
    for root, _dirs, files in os.walk(base):
        if "plugin.json" in files:
            return root
        if "package.json" in files and fallback is None:
            fallback = root
    return fallback or base


def load_metadata(plugin_root):
    """Merge package.json + plugin.json. plugin.json is authoritative for the
    fields Decky matches on (name/author), which is what makes the store show
    'Installed' correctly."""
    meta = {}
    pkg_path = os.path.join(plugin_root, "package.json")
    plug_path = os.path.join(plugin_root, "plugin.json")

    if os.path.exists(pkg_path):
        with open(pkg_path, encoding="utf-8") as f:
            meta.update(json.load(f))

    if os.path.exists(plug_path):
        with open(plug_path, encoding="utf-8") as f:
            plug = json.load(f)
        meta.update({k: v for k, v in plug.items() if v not in (None, "")})

    return meta, pkg_path, plug_path


def stamp_version(path, version):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    data["version"] = version
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)


def make_executable(path):
    st = os.stat(path)
    os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def repackage_zip(plugin_root, folder_name, out_path):
    """Rebuild a clean .zip: single top-level folder == plugin name, with Linux
    file permissions preserved (executables stay +x, otherwise the backend can
    fail to load and the plugin only appears after a full restart)."""
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(plugin_root):
            for name in files:
                fp = os.path.join(root, name)
                rel = os.path.relpath(fp, plugin_root)
                arc = f"{folder_name}/{rel}".replace(os.sep, "/")

                zi = zipfile.ZipInfo.from_file(fp, arc)
                zi.compress_type = zipfile.ZIP_DEFLATED
                zi.external_attr = (os.stat(fp).st_mode & 0xFFFF) << 16

                with open(fp, "rb") as src:
                    zf.writestr(zi, src.read())


# ---------------------------------------------------------------------------
# Per-plugin pipeline
# ---------------------------------------------------------------------------
def pick_release(releases, force_version):
    if force_version:
        for r in releases:
            if r["tag_name"] == force_version:
                return r
        return None
    for r in releases:
        if not r.get("prerelease", False) and not r.get("draft", False):
            return r
    return releases[0] if releases else None


def pick_asset(release):
    prefer = [a for a in release.get("assets", []) if a["name"].endswith(".zip")]
    other = [
        a for a in release.get("assets", [])
        if a["name"].endswith(".tar.gz") or a["name"].endswith(".tgz")
    ]
    picked = prefer or other
    return picked[0] if picked else None


def process_plugin(config, plugin_id, downloads_dir):
    repo = config.get("repo")
    if not repo:
        print("  ! Skipping entry with no 'repo'.")
        return None

    print(f"Processing {repo} ...")
    resp = SESSION.get(
        f"https://api.github.com/repos/{repo}/releases",
        headers=get_headers(),
        timeout=60,
    )
    if resp.status_code == 403 and "rate limit" in resp.text.lower():
        print(f"  ! GitHub rate limit hit for {repo} (set GITHUB_TOKEN).")
        return None
    if resp.status_code != 200:
        print(f"  ! Cannot fetch releases for {repo} (HTTP {resp.status_code}).")
        return None

    release = pick_release(resp.json(), config.get("force_version"))
    if not release:
        print(f"  ! No matching release for {repo}.")
        return None

    asset = pick_asset(release)
    if not asset:
        print(f"  ! No .zip/.tar.gz asset in {repo} release {release['tag_name']}.")
        return None

    version = normalize_version(asset["name"], release["tag_name"])
    temp_dir = tempfile.mkdtemp()
    try:
        archive_path = os.path.join(temp_dir, asset["name"])
        download(asset["browser_download_url"], archive_path)

        extract_dir = os.path.join(temp_dir, "extracted")
        os.makedirs(extract_dir)
        extract_archive(archive_path, extract_dir)

        plugin_root = find_plugin_root(extract_dir)
        meta, pkg_path, plug_path = load_metadata(plugin_root)

        # Enforce the clean SemVer everywhere the store may read it.
        stamp_version(pkg_path, version)
        stamp_version(plug_path, version)

        # The store name MUST equal plugin.json's name, or Decky won't match the
        # installed plugin to this store entry.
        plugin_name = meta.get("name") or repo.split("/")[1]

        # Optional chmod +x for specific files (generalised patch_deckify).
        exec_files = list(config.get("exec_files", []))
        if config.get("patch_deckify", False):
            exec_files.append("bin/librespot")
        for rel in exec_files:
            target = os.path.join(plugin_root, rel)
            if os.path.exists(target):
                make_executable(target)
                print(f"  + chmod +x {rel}")

        zip_name = f"{plugin_name}-v{version}.zip".replace(" ", "_")
        zip_path = os.path.join(downloads_dir, zip_name)
        repackage_zip(plugin_root, plugin_name, zip_path)

        entry = {
            "id": plugin_id,
            "name": plugin_name,
            "author": meta.get("author") or repo.split("/")[0],
            "description": meta.get("description") or "No description provided",
            "tags": meta.get("tags") or ["utilities"],
            "image_url": meta.get("image_url")
            or f"https://github.com/{repo.split('/')[0]}.png",
            "versions": [
                {
                    "name": version,
                    "hash": calculate_sha256(zip_path),
                    "artifact": f"{PAGES_URL.rstrip('/')}/downloads/{zip_name}",
                }
            ],
        }
        print(f"  ok {plugin_name} {version}")
        return entry
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    dist_dir = os.path.join(os.getcwd(), "dist")
    downloads_dir = os.path.join(dist_dir, "downloads")
    os.makedirs(downloads_dir, exist_ok=True)

    if not os.path.exists(CONFIG_FILE):
        print(f"Error: {CONFIG_FILE} not found!")
        return

    with open(CONFIG_FILE, "rb") as f:
        plugins_config = tomllib.load(f).get("plugin", [])

    store = []
    for idx, cfg in enumerate(plugins_config, start=1):
        try:
            entry = process_plugin(cfg, idx, downloads_dir)
            if entry:
                store.append(entry)
        except Exception as e:  # keep one bad repo from killing the whole build
            print(f"  ! {cfg.get('repo', '??')} failed: {e}")

    out_path = os.path.join(dist_dir, "plugins.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2)

    print(f"\nDone: {len(store)} plugin(s) -> {out_path}")


if __name__ == "__main__":
    main()