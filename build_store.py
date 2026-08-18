import os
import requests
import json
import hashlib
import tarfile
import zipfile
import shutil
import stat
import tempfile

# Your GitHub Pages store URL (set automatically by GitHub Actions)
PAGES_URL = os.environ.get("PAGES_URL", "https://your-username.github.io/custom-decky-store")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")

# Plugin definitions and version toggling (stable vs pre-release)
PLUGINS_CONFIG = [
    {
        "repo": "adv-inn/Deckify",
        "use_prerelease": False,
        "patch_deckify": True  # Enables the custom patch logic
    },
    {
        "repo": "mubaraknumann/unifideck",
        "use_prerelease": True   # Fetches the Dev Build (pre-release)
    },
    {
        "repo": "DeckSettings/decky-game-settings",
        "use_prerelease": False
    }
]

def get_headers():
    headers = {}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"
    return headers

def calculate_sha256(filepath):
    sha256_hash = hashlib.sha256()
    with open(filepath, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()

def process_plugin(config, dist_dir, downloads_dir):
    repo = config["repo"]
    print(f"Processing {repo}...")
    
    # Fetch releases list from GitHub API
    url = f"https://api.github.com/repos/{repo}/releases"
    releases_resp = requests.get(url, headers=get_headers())
    if releases_resp.status_code != 200:
        print(f"Error fetching releases for {repo}: API Rate Limit or repo not found.")
        return None
        
    releases = releases_resp.json()
    
    # Select the appropriate release (stable vs pre-release)
    target_release = None
    for r in releases:
        if not config.get("use_prerelease", False) and r["prerelease"]:
            continue
        target_release = r
        break
        
    if not target_release:
        print(f"No suitable releases found for {repo}.")
        return None
        
    # Clean the version string (remove 'v' prefix if present)
    clean_version = target_release["tag_name"].lstrip('v')
    
    # Find the plugin archive (.tar.gz or .zip)
    asset = None
    for a in target_release["assets"]:
        if a["name"].endswith(".tar.gz") or a["name"].endswith(".zip"):
            asset = a
            break
            
    if not asset:
        print(f"No valid archives (.zip or .tar.gz) found in {repo}.")
        return None

    # Download the archive to a temporary directory
    temp_dir = tempfile.mkdtemp()
    original_asset_path = os.path.join(temp_dir, asset["name"])
    
    r = requests.get(asset["browser_download_url"], stream=True)
    with open(original_asset_path, 'wb') as f:
        shutil.copyfileobj(r.raw, f)
        
    final_path = original_asset_path
    final_download_url = asset["browser_download_url"]

    # CUSTOM DECKIFY PATCHING LOGIC
    if config.get("patch_deckify", False):
        print(f"Patching Deckify to version {clean_version}...")
        extract_dir = os.path.join(temp_dir, "extracted")
        os.makedirs(extract_dir)
        
        # Extract the original archive
        with tarfile.open(original_asset_path, "r:gz") as tar:
            tar.extractall(path=extract_dir)
            
        plugin_root = os.path.join(extract_dir, os.listdir(extract_dir)[0])
        if not os.path.isdir(plugin_root):
            plugin_root = extract_dir
            
        # 1. Fix the version in package.json / plugin.json
        meta_file = "package.json" if os.path.exists(os.path.join(plugin_root, "package.json")) else "plugin.json"
        meta_path = os.path.join(plugin_root, meta_file)
        
        if os.path.exists(meta_path):
            with open(meta_path, "r", encoding="utf-8") as f:
                pkg_data = json.load(f)
            pkg_data["version"] = clean_version
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(pkg_data, f, indent=4)
                
        # 2. Grant execution permissions to librespot (replaces install.sh)
        librespot_path = os.path.join(plugin_root, "bin", "librespot")
        if os.path.exists(librespot_path):
            st = os.stat(librespot_path)
            os.chmod(librespot_path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
            
        # 3. Repackage into a new archive
        patched_filename = f"Deckify-v{clean_version}-patched.tar.gz"
        patched_filepath = os.path.join(downloads_dir, patched_filename)
        
        with tarfile.open(patched_filepath, "w:gz") as tar:
            tar.add(plugin_root, arcname="Deckify")
            
        final_path = patched_filepath
        # Set download URL to your GitHub Pages hosted file
        final_download_url = f"{PAGES_URL.rstrip('/')}/downloads/{patched_filename}"

    # Read metadata for the final plugins.json
    pkg_data = {}
    try:
        if final_path.endswith(".tar.gz"):
            with tarfile.open(final_path, "r:gz") as tar:
                for member in tar.getmembers():
                    if member.name.endswith("package.json") or member.name.endswith("plugin.json"):
                        f = tar.extractfile(member)
                        pkg_data = json.loads(f.read().decode("utf-8"))
                        break
        elif final_path.endswith(".zip"):
            with zipfile.ZipFile(final_path, "r") as z:
                for member in z.namelist():
                    if member.endswith("package.json") or member.endswith("plugin.json"):
                        with z.open(member) as f:
                            pkg_data = json.loads(f.read().decode("utf-8"))
                        break
    except Exception as e:
        print(f"Warning: Failed to read package.json from {repo}: {e}")

    hash_val = calculate_sha256(final_path)
    
    return {
        "name": pkg_data.get("name", repo.split("/")[1]),
        "author": pkg_data.get("author", repo.split("/")[0]),
        "description": pkg_data.get("description", "No description provided"),
        "tags": pkg_data.get("tags", ["custom"]),
        "versions": [
            {
                "version": clean_version,
                "download_url": final_download_url,
                "hash": hash_val,
                "min_loader_version": pkg_data.get("min_loader_version", "v2.0.0")
            }
        ]
    }

def main():
    dist_dir = os.path.join(os.getcwd(), "dist")
    downloads_dir = os.path.join(dist_dir, "downloads")
    
    os.makedirs(dist_dir, exist_ok=True)
    os.makedirs(downloads_dir, exist_ok=True)
    
    store_plugins = []
    
    for config in PLUGINS_CONFIG:
        plugin_data = process_plugin(config, dist_dir, downloads_dir)
        if plugin_data:
            store_plugins.append(plugin_data)
            
    # Save the final plugins.json to the dist/ folder
    store_json_path = os.path.join(dist_dir, "plugins.json")
    with open(store_json_path, "w", encoding="utf-8") as f:
        json.dump(store_plugins, f, indent=4)
        
    print(f"Store successfully generated at {store_json_path}")

if __name__ == "__main__":
    main()