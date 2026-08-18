import os
import requests
import json
import hashlib
import tarfile
import zipfile
import shutil
import stat
import tempfile
import tomllib
import re

# Your GitHub Pages store URL (set automatically by GitHub Actions)
PAGES_URL = os.environ.get("PAGES_URL", "https://your-username.github.io/custom-decky-store")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
CONFIG_FILE = "plugins_config.toml"

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

def process_plugin(config, plugin_id, dist_dir, downloads_dir):
    repo = config.get("repo")
    if not repo:
        print("Error: Missing 'repo' in config entry.")
        return None
        
    force_version = config.get("force_version")
    print(f"Processing {repo}...")
    
    # Fetch releases list from GitHub API
    url = f"https://api.github.com/repos/{repo}/releases"
    releases_resp = requests.get(url, headers=get_headers())
    if releases_resp.status_code != 200:
        print(f"Error fetching releases for {repo}: API Rate Limit or repo not found.")
        return None
        
    releases = releases_resp.json()
    target_release = None
    
    # Select release based on config
    if force_version:
        for r in releases:
            if r["tag_name"] == force_version:
                target_release = r
                break
        if not target_release:
            print(f"Error: Release with tag '{force_version}' not found for {repo}.")
            return None
    else:
        for r in releases:
            if not r.get("prerelease", False):
                target_release = r
                break
        if not target_release and releases:
            target_release = releases[0]
            
    if not target_release:
        print(f"No suitable releases found for {repo}.")
        return None
        
    # Find the plugin archive
    asset = None
    for a in target_release.get("assets", []):
        if a["name"].endswith(".tar.gz") or a["name"].endswith(".zip"):
            asset = a
            break
            
    if not asset:
        print(f"No valid archives (.zip or .tar.gz) found in {repo}.")
        return None

    # Force Semantic Versioning (SemVer) extraction to prevent store crashes
    # It tries to find a pattern like 0.7.4 in the asset name or the tag name.
    semver_match = re.search(r'(\d+\.\d+(?:\.\d+)?)', asset["name"]) or re.search(r'(\d+\.\d+(?:\.\d+)?)', target_release["tag_name"])
    clean_version = semver_match.group(1) if semver_match else target_release["tag_name"].lstrip('v')

    # Download the original archive
    temp_dir = tempfile.mkdtemp()
    original_asset_path = os.path.join(temp_dir, asset["name"])
    
    r = requests.get(asset["browser_download_url"], stream=True)
    with open(original_asset_path, 'wb') as f:
        shutil.copyfileobj(r.raw, f)

    # Extract EVERYTHING to a uniform directory
    extract_dir = os.path.join(temp_dir, "extracted")
    os.makedirs(extract_dir)
    
    if original_asset_path.endswith(".tar.gz"):
        with tarfile.open(original_asset_path, "r:gz") as tar:
            tar.extractall(path=extract_dir)
    elif original_asset_path.endswith(".zip"):
        with zipfile.ZipFile(original_asset_path, "r") as z:
            z.extractall(path=extract_dir)

    # Locate the actual plugin root (the folder containing package.json)
    plugin_root = extract_dir
    for item in os.listdir(extract_dir):
        item_path = os.path.join(extract_dir, item)
        if os.path.isdir(item_path) and ("package.json" in os.path.listdir(item_path) or "plugin.json" in os.path.listdir(item_path)):
            plugin_root = item_path
            break

    # Read and enforce semver in package.json and/or plugin.json
    pkg_data = {}
    for meta_file in ["package.json", "plugin.json"]:
        meta_path = os.path.join(plugin_root, meta_file)
        if os.path.exists(meta_path):
            with open(meta_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["version"] = clean_version # Overwrite with clean SemVer
            if not pkg_data: 
                pkg_data = data
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)

    # CUSTOM DECKIFY PATCHING (Grant execution rights)
    if config.get("patch_deckify", False):
        print(f"Applying custom patch for Deckify...")
        librespot_path = os.path.join(plugin_root, "bin", "librespot")
        if os.path.exists(librespot_path):
            st = os.stat(librespot_path)
            os.chmod(librespot_path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    # ALWAYS repackage as .ZIP while preserving Linux file execution permissions
    plugin_name = pkg_data.get("name", repo.split("/")[1])
    zip_filename = f"{plugin_name}-v{clean_version}.zip".replace(" ", "_")
    zip_filepath = os.path.join(downloads_dir, zip_filename)

    with zipfile.ZipFile(zip_filepath, "w", zipfile.ZIP_DEFLATED) as zf:
        for root_dir, _, files in os.walk(plugin_root):
            for file in files:
                file_path = os.path.join(root_dir, file)
                arcname = os.path.join(plugin_name, os.path.relpath(file_path, plugin_root))
                
                # Crucial: Preserve file permissions (chmod +x) inside the zip
                z_info = zipfile.ZipInfo.from_file(file_path, arcname)
                st = os.stat(file_path)
                z_info.external_attr = (st.st_mode & 0xFFFF) << 16 
                
                with open(file_path, "rb") as src:
                    zf.writestr(z_info, src.read())

    # We now serve the newly generated ZIP file
    final_path = zip_filepath
    final_artifact_url = f"{PAGES_URL.rstrip('/')}/downloads/{zip_filename}"
    hash_val = calculate_sha256(final_path)

    # Output strictly following the lightweight schema
    return {
        "id": plugin_id,
        "name": plugin_name,
        "author": pkg_data.get("author", repo.split("/")[0]),
        "description": pkg_data.get("description", "No description provided"),
        "tags": pkg_data.get("tags", ["utilities"]),
        "image_url": f"https://github.com/{repo.split('/')[0]}.png", 
        "versions": [
            {
                "name": clean_version,
                "hash": hash_val,
                "artifact": final_artifact_url
            }
        ]
    }

def main():
    dist_dir = os.path.join(os.getcwd(), "dist")
    downloads_dir = os.path.join(dist_dir, "downloads")
    
    os.makedirs(dist_dir, exist_ok=True)
    os.makedirs(downloads_dir, exist_ok=True)
    
    if not os.path.exists(CONFIG_FILE):
        print(f"Error: {CONFIG_FILE} not found!")
        return
        
    with open(CONFIG_FILE, "rb") as f:
        try:
            config_data = tomllib.load(f)
            plugins_config = config_data.get("plugin", [])
        except Exception as e:
            print(f"Error parsing {CONFIG_FILE}: {e}")
            return
    
    store_plugins = []
    
    for idx, config in enumerate(plugins_config, start=1):
        plugin_data = process_plugin(config, idx, dist_dir, downloads_dir)
        if plugin_data:
            store_plugins.append(plugin_data)
            
    store_json_path = os.path.join(dist_dir, "plugins.json")
    with open(store_json_path, "w", encoding="utf-8") as f:
        json.dump(store_plugins, f, indent=2)
        
    print(f"Store successfully generated at {store_json_path}")

if __name__ == "__main__":
    main()