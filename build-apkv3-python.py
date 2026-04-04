#!/usr/bin/env python3
"""
APKv3 Package Builder - Pure Python3 Implementation (No External Dependencies)

Builds OpenWrt-compatible APKv3 packages from scratch by directly constructing
the ADB binary format.

Usage:
    python3 build-apkv3-python.py [--arch ARCH] [--revision N] [--output FILE]

Version is derived from the service binary (semver, e.g. 3.1.8) with a
revision suffix (default -r0). For cross-arch builds, the host-arch service
binary is used for version extraction.

This script reads package metadata from control/ directory and file contents
from data/ directory, similar to the existing build.sh workflow.
"""

import argparse
import glob
import hashlib
import os
import platform
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import zlib

# ============================================================================
# ADB Constants (from apk-tools src/adb.h and src/apk_adb.h)
# ============================================================================

# File header
ADB_FORMAT_MAGIC = 0x2e424441  # "ADB."
ADB_SCHEMA_PACKAGE = 0x676b6370  # "pckg"

# Block types
ADB_BLOCK_ADB = 0
ADB_BLOCK_SIG = 1
ADB_BLOCK_DATA = 2
ADB_BLOCK_ALIGNMENT = 8

# Value type codes (high 4 bits of adb_val_t)
ADB_TYPE_SPECIAL = 0x00000000
ADB_TYPE_INT = 0x10000000
ADB_TYPE_INT_32 = 0x20000000
ADB_TYPE_INT_64 = 0x30000000
ADB_TYPE_BLOB_8 = 0x80000000
ADB_TYPE_BLOB_16 = 0x90000000
ADB_TYPE_BLOB_32 = 0xa0000000
ADB_TYPE_ARRAY = 0xd0000000
ADB_TYPE_OBJECT = 0xe0000000
ADB_TYPE_MASK = 0xf0000000
ADB_VALUE_MASK = 0x0fffffff

# Special values
ADB_VAL_NULL = 0x00000000

# Package Info field indices
ADBI_PI_NAME = 0x01
ADBI_PI_VERSION = 0x02
ADBI_PI_HASHES = 0x03
ADBI_PI_DESCRIPTION = 0x04
ADBI_PI_ARCH = 0x05
ADBI_PI_LICENSE = 0x06
ADBI_PI_ORIGIN = 0x07
ADBI_PI_MAINTAINER = 0x08
ADBI_PI_URL = 0x09
ADBI_PI_REPO_COMMIT = 0x0a
ADBI_PI_BUILD_TIME = 0x0b
ADBI_PI_INSTALLED_SIZE = 0x0c
ADBI_PI_FILE_SIZE = 0x0d
ADBI_PI_PROVIDER_PRIORITY = 0x0e
ADBI_PI_DEPENDS = 0x0f
ADBI_PI_PROVIDES = 0x10
ADBI_PI_REPLACES = 0x11
ADBI_PI_INSTALL_IF = 0x12
ADBI_PI_RECOMMENDS = 0x13
ADBI_PI_LAYER = 0x14
ADBI_PI_TAGS = 0x15
ADBI_PI_MAX = 0x16

# Dependency field indices
ADBI_DEP_NAME = 0x01
ADBI_DEP_VERSION = 0x02
ADBI_DEP_MATCH = 0x03
ADBI_DEP_MAX = 0x04

# ACL field indices
ADBI_ACL_MODE = 0x01
ADBI_ACL_USER = 0x02
ADBI_ACL_GROUP = 0x03
ADBI_ACL_XATTRS = 0x04
ADBI_ACL_MAX = 0x05

# File Info field indices
ADBI_FI_NAME = 0x01
ADBI_FI_ACL = 0x02
ADBI_FI_SIZE = 0x03
ADBI_FI_MTIME = 0x04
ADBI_FI_HASHES = 0x05
ADBI_FI_TARGET = 0x06
ADBI_FI_MAX = 0x07

# Directory Info field indices
ADBI_DI_NAME = 0x01
ADBI_DI_ACL = 0x02
ADBI_DI_FILES = 0x03
ADBI_DI_MAX = 0x04

# Script field indices
ADBI_SCRPT_TRIGGER = 0x01
ADBI_SCRPT_PREINST = 0x02
ADBI_SCRPT_POSTINST = 0x03
ADBI_SCRPT_PREDEINST = 0x04
ADBI_SCRPT_POSTDEINST = 0x05
ADBI_SCRPT_PREUPGRADE = 0x06
ADBI_SCRPT_POSTUPGRADE = 0x07
ADBI_SCRPT_MAX = 0x08

# Package field indices
ADBI_PKG_PKGINFO = 0x01
ADBI_PKG_PATHS = 0x02
ADBI_PKG_SCRIPTS = 0x03
ADBI_PKG_TRIGGERS = 0x04
ADBI_PKG_REPLACES_PRIORITY = 0x05
ADBI_PKG_MAX = 0x06

# Version match operators (from apk_version.h)
APK_VERSION_EQUAL = 0
APK_VERSION_LESS = 1
APK_VERSION_GREATER = 2
APK_VERSION_CONFLICT = 16


# ============================================================================
# ADB Writer - Constructs the ADB binary data region
# ============================================================================

class ADBWriter:
    """Constructs ADB binary data with deduplication support."""

    def __init__(self):
        # Start with the ADB header (8 bytes)
        # adb_compat_ver=0, adb_ver=0, reserved=0, root=0
        self.data = bytearray(8)
        self._cache = {}  # content hash -> offset for dedup

    def _align_to(self, alignment):
        """Pad data to the given alignment boundary."""
        remainder = len(self.data) % alignment
        if remainder:
            self.data.extend(b'\x00' * (alignment - remainder))

    def _write_raw(self, content, alignment):
        """Write raw bytes with alignment, return offset. Uses dedup cache."""
        cache_key = (bytes(content), alignment)
        if cache_key in self._cache:
            cached_offset = self._cache[cache_key]
            # verify alignment
            if cached_offset % alignment == 0:
                return cached_offset

        self._align_to(alignment)
        offset = len(self.data)
        self.data.extend(content)
        self._cache[cache_key] = offset
        return offset

    def write_blob(self, data):
        """Write a blob value, return adb_val_t."""
        if not data and not isinstance(data, bytes):
            return ADB_VAL_NULL
        if isinstance(data, str):
            data = data.encode('utf-8')
        if len(data) == 0:
            return ADB_VAL_NULL

        size = len(data)
        if size > 0xffff:
            header = struct.pack('<I', size)
            alignment = 4
            type_code = ADB_TYPE_BLOB_32
        elif size > 0xff:
            header = struct.pack('<H', size)
            alignment = 2
            type_code = ADB_TYPE_BLOB_16
        else:
            header = struct.pack('B', size)
            alignment = 1
            type_code = ADB_TYPE_BLOB_8

        content = header + data
        offset = self._write_raw(content, alignment)
        return type_code | (offset & ADB_VALUE_MASK)

    def write_int(self, value):
        """Write an integer value, return adb_val_t."""
        if value == 0:
            return ADB_VAL_NULL
        if value <= ADB_VALUE_MASK:
            return ADB_TYPE_INT | (value & ADB_VALUE_MASK)
        if value <= 0xFFFFFFFF:
            content = struct.pack('<I', value)
            offset = self._write_raw(content, 4)
            return ADB_TYPE_INT_32 | (offset & ADB_VALUE_MASK)
        content = struct.pack('<Q', value)
        offset = self._write_raw(content, 4)  # Int64 aligns to 4, not 8
        return ADB_TYPE_INT_64 | (offset & ADB_VALUE_MASK)

    def write_object(self, slots, max_slots):
        """Write an object, return adb_val_t.

        Args:
            slots: dict of {index: adb_val_t} for non-null fields
            max_slots: maximum number of slots (determines slot count)
        """
        # Find the highest used slot
        if not slots:
            return ADB_VAL_NULL

        max_used = max(slots.keys())
        num_entries = max_used + 1  # include the count slot at index 0

        # Build the slot array
        slot_data = bytearray(num_entries * 4)
        struct.pack_into('<I', slot_data, 0, num_entries)
        for idx, val in slots.items():
            struct.pack_into('<I', slot_data, idx * 4, val)

        offset = self._write_raw(slot_data, 4)
        return ADB_TYPE_OBJECT | (offset & ADB_VALUE_MASK)

    def write_array(self, items):
        """Write an array, return adb_val_t.

        Args:
            items: list of adb_val_t values
        """
        if not items:
            return ADB_VAL_NULL

        num_entries = len(items) + 1  # +1 for the count slot
        slot_data = bytearray(num_entries * 4)
        struct.pack_into('<I', slot_data, 0, num_entries)
        for i, val in enumerate(items):
            struct.pack_into('<I', slot_data, (i + 1) * 4, val)

        offset = self._write_raw(slot_data, 4)
        return ADB_TYPE_ARRAY | (offset & ADB_VALUE_MASK)

    def set_root(self, val):
        """Set the root object value in the ADB header."""
        struct.pack_into('<I', self.data, 4, val)

    def get_data(self):
        """Return the complete ADB data bytes."""
        return bytes(self.data)


# ============================================================================
# Block Writer - Constructs the final APKv3 file
# ============================================================================

def make_block_header(block_type, content_length):
    """Create a block header for the given type and content length.

    Returns (header_bytes, padding_length).
    """
    raw_size = 4 + content_length  # 4 bytes for the header itself
    if raw_size > 0x3FFFFFFF:
        # Extended block header
        header = struct.pack('<II', (0x3 << 30) | block_type, 0)
        header += struct.pack('<Q', 16 + content_length)  # x_size includes full header
        raw_size = 16 + content_length
    else:
        header = struct.pack('<I', (block_type << 30) | raw_size)

    padded = (raw_size + ADB_BLOCK_ALIGNMENT - 1) & ~(ADB_BLOCK_ALIGNMENT - 1)
    padding = padded - raw_size
    return header, padding


def build_file_header(schema_id=ADB_SCHEMA_PACKAGE):
    """Build the 8-byte APKv3 file header."""
    return struct.pack('<II', ADB_FORMAT_MAGIC, schema_id)


def compress_stream(data, method='none'):
    """Compress and wrap data with APKv3 compression framing.

    APKv3 compression replaces the standard "ADB." file magic with a
    compression marker, then compresses the entire remaining data
    (file header + blocks) as one stream.

    Format produced:
      - none:    (no wrapping, data returned as-is)
      - deflate: b"ADBd" + raw_deflate(data)
      - zstd:    b"ADBc" + struct{alg=2, level} + zstd(data)

    The caller must include the standard file header ("ADB." + schema)
    at the beginning of `data` when compression is used.

    Args:
        data: raw bytes to compress (including file header when compressed)
        method: 'none', 'deflate', or 'zstd'

    Returns:
        Framed and compressed bytes, or original bytes for 'none'.
    """
    if method == 'none':
        return data

    if method == 'deflate':
        # APKv3 uses raw deflate (no zlib header/trailer), prefixed with "ADBd"
        compressor = zlib.compressobj(level=9, wbits=-15)
        compressed = compressor.compress(data) + compressor.flush()
        return b'ADBd' + compressed

    if method == 'zstd':
        try:
            import zstandard
            level = 9
            cctx = zstandard.ZstdCompressor(level=level)
            compressed = cctx.compress(data)
            # "ADBc" prefix + 2-byte compression spec (alg=2 for zstd, level)
            spec = struct.pack('BB', 2, level)
            return b'ADBc' + spec + compressed
        except ImportError:
            print("Error: zstd compression requested but 'zstandard' module not available.",
                  file=sys.stderr)
            print("Install with: pip3 install zstandard", file=sys.stderr)
            print("Falling back to deflate compression.", file=sys.stderr)
            return compress_stream(data, 'deflate')

    raise ValueError(f"Unknown compression method: {method}")


# ============================================================================
# Dependency Parser
# ============================================================================

def parse_dependency(dep_str):
    """Parse a dependency string like 'luci-compat' or 'pkg>=1.0'.

    Returns (name, version, match_op) or (name, None, None).
    """
    dep_str = dep_str.strip()
    if not dep_str:
        return None

    # Check for conflict prefix
    conflict = False
    if dep_str.startswith('!'):
        conflict = True
        dep_str = dep_str[1:]

    # Find version operator
    for op_str, op_val in [('>=', APK_VERSION_GREATER | APK_VERSION_EQUAL),
                           ('<=', APK_VERSION_LESS | APK_VERSION_EQUAL),
                           ('><', APK_VERSION_LESS | APK_VERSION_GREATER),
                           ('>', APK_VERSION_GREATER),
                           ('<', APK_VERSION_LESS),
                           ('=', APK_VERSION_EQUAL),
                           ('~', APK_VERSION_EQUAL | APK_VERSION_GREATER)]:  # ~ means "compatible with" (>= semver)
        if op_str in dep_str:
            parts = dep_str.split(op_str, 1)
            name = parts[0]
            version = parts[1] if len(parts) > 1 else None
            if conflict:
                op_val |= APK_VERSION_CONFLICT
            return (name, version, op_val)

    return (dep_str, None, None)


# ============================================================================
# Control File Parser
# ============================================================================

def parse_control_file(control_path):
    """Parse a Debian-style control file into a dict."""
    info = {}
    with open(control_path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if ':' in line:
                key, _, value = line.partition(':')
                info[key.strip()] = value.strip()
    return info


# ============================================================================
# File Tree Scanner
# ============================================================================

class FileEntry:
    """Represents a file in the package."""
    def __init__(self, name, full_path, file_stat):
        self.name = name
        self.full_path = full_path
        self.stat = file_stat
        self.size = file_stat.st_size if stat.S_ISREG(file_stat.st_mode) else 0
        self.mode = file_stat.st_mode & 0o7777
        self.mtime = int(file_stat.st_mtime)
        self.is_regular = stat.S_ISREG(file_stat.st_mode)
        self.is_symlink = stat.S_ISLNK(file_stat.st_mode)
        self.is_dir = stat.S_ISDIR(file_stat.st_mode)
        self.sha256 = None
        self.symlink_target = None

    def compute_hash(self):
        """Compute SHA256 hash for regular files."""
        if self.is_regular and self.size > 0:
            h = hashlib.sha256()
            with open(self.full_path, 'rb') as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    h.update(chunk)
            self.sha256 = h.digest()

    def read_symlink(self):
        """Read symlink target."""
        if self.is_symlink:
            self.symlink_target = os.readlink(self.full_path)


class DirEntry:
    """Represents a directory in the package."""
    def __init__(self, name, mode=0o755, uid=0, gid=0):
        self.name = name
        self.mode = mode
        self.files = []


def scan_directory(root_path):
    """Scan a directory tree and return sorted list of DirEntry objects."""
    dirs = {}

    for dirpath, dirnames, filenames in os.walk(root_path, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root_path)
        if rel_dir == '.':
            rel_dir = ''

        dirnames.sort()

        dir_stat = os.lstat(dirpath)
        dir_entry = DirEntry(rel_dir, dir_stat.st_mode & 0o7777)
        dirs[rel_dir] = dir_entry

        for fname in sorted(filenames):
            if fname == '.gitkeep':
                continue
            full_path = os.path.join(dirpath, fname)
            try:
                fstat = os.lstat(full_path)
            except OSError:
                continue

            fe = FileEntry(fname, full_path, fstat)
            if fe.is_regular:
                fe.compute_hash()
            elif fe.is_symlink:
                fe.read_symlink()
            else:
                continue

            dir_entry.files.append(fe)

    # Sort directories: root first, then sorted by name
    sorted_dirs = []
    if '' in dirs:
        sorted_dirs.append(dirs[''])
    for name in sorted(dirs.keys()):
        if name != '':
            sorted_dirs.append(dirs[name])

    return sorted_dirs


# ============================================================================
# APKv3 Package Builder
# ============================================================================

class APKv3Builder:
    """Builds an APKv3 package file."""

    def __init__(self):
        self.adb = ADBWriter()

    def build_acl(self, mode, user='root', group='root'):
        """Build an ACL object."""
        slots = {}
        slots[ADBI_ACL_MODE] = self.adb.write_int(mode)
        if user and user != 'root':
            slots[ADBI_ACL_USER] = self.adb.write_blob(user)
        if group and group != 'root':
            slots[ADBI_ACL_GROUP] = self.adb.write_blob(group)
        # Only write ACL if there's non-default data or if we want compat
        if not slots:
            # For compat mode, still write the mode
            slots[ADBI_ACL_MODE] = self.adb.write_int(mode)
        return self.adb.write_object(slots, ADBI_ACL_MAX)

    def build_file(self, file_entry, build_time=None):
        """Build a File object from a FileEntry."""
        slots = {}
        slots[ADBI_FI_NAME] = self.adb.write_blob(file_entry.name)

        acl_val = self.build_acl(file_entry.mode)
        if acl_val != ADB_VAL_NULL:
            slots[ADBI_FI_ACL] = acl_val

        if file_entry.is_regular:
            if file_entry.size > 0:
                slots[ADBI_FI_SIZE] = self.adb.write_int(file_entry.size)
            if file_entry.sha256:
                slots[ADBI_FI_HASHES] = self.adb.write_blob(file_entry.sha256)

        mtime = build_time if build_time is not None else file_entry.mtime
        if mtime > 0:
            slots[ADBI_FI_MTIME] = self.adb.write_int(mtime)

        if file_entry.is_symlink and file_entry.symlink_target:
            # Symlink target format: uint16_le(S_IFLNK) + target_string
            target_data = struct.pack('<H', 0xa000) + file_entry.symlink_target.encode('utf-8')
            slots[ADBI_FI_TARGET] = self.adb.write_blob(target_data)

        return self.adb.write_object(slots, ADBI_FI_MAX)

    def build_dir(self, dir_entry, build_time=None):
        """Build a Directory object from a DirEntry."""
        slots = {}

        if dir_entry.name:
            slots[ADBI_DI_NAME] = self.adb.write_blob(dir_entry.name)

        acl_val = self.build_acl(dir_entry.mode)
        if acl_val != ADB_VAL_NULL:
            slots[ADBI_DI_ACL] = acl_val

        if dir_entry.files:
            file_vals = []
            for f in dir_entry.files:
                file_vals.append(self.build_file(f, build_time))
            slots[ADBI_DI_FILES] = self.adb.write_array(file_vals)

        return self.adb.write_object(slots, ADBI_DI_MAX)

    def build_dependency(self, dep_str):
        """Build a Dependency object from a dependency string."""
        parsed = parse_dependency(dep_str)
        if not parsed:
            return ADB_VAL_NULL

        name, version, match_op = parsed
        slots = {}
        slots[ADBI_DEP_NAME] = self.adb.write_blob(name)
        if version:
            slots[ADBI_DEP_VERSION] = self.adb.write_blob(version)
            if match_op is not None and match_op != APK_VERSION_EQUAL:
                slots[ADBI_DEP_MATCH] = self.adb.write_int(match_op)

        return self.adb.write_object(slots, ADBI_DEP_MAX)

    def build_dependency_array(self, deps_str):
        """Build a dependency array from a comma/space separated string."""
        if not deps_str:
            return ADB_VAL_NULL

        dep_items = []
        # Split by comma or space
        for dep in deps_str.replace(',', ' ').split():
            dep = dep.strip()
            if dep:
                val = self.build_dependency(dep)
                if val != ADB_VAL_NULL:
                    dep_items.append(val)

        if not dep_items:
            return ADB_VAL_NULL
        return self.adb.write_array(dep_items)

    def build_scripts(self, script_files):
        """Build a Scripts object from script file paths.

        Args:
            script_files: dict mapping script type name to file content
        """
        type_map = {
            'trigger': ADBI_SCRPT_TRIGGER,
            'pre-install': ADBI_SCRPT_PREINST,
            'preinst': ADBI_SCRPT_PREINST,
            'post-install': ADBI_SCRPT_POSTINST,
            'postinst': ADBI_SCRPT_POSTINST,
            'pre-deinstall': ADBI_SCRPT_PREDEINST,
            'prerm': ADBI_SCRPT_PREDEINST,
            'post-deinstall': ADBI_SCRPT_POSTDEINST,
            'postrm': ADBI_SCRPT_POSTDEINST,
            'pre-upgrade': ADBI_SCRPT_PREUPGRADE,
            'post-upgrade': ADBI_SCRPT_POSTUPGRADE,
        }

        slots = {}
        for name, content in script_files.items():
            idx = type_map.get(name)
            if idx and content:
                if isinstance(content, str):
                    content = content.encode('utf-8')
                slots[idx] = self.adb.write_blob(content)

        if not slots:
            return ADB_VAL_NULL
        return self.adb.write_object(slots, ADBI_SCRPT_MAX)

    def build_pkginfo(self, control_info, installed_size, build_time):
        """Build the Package Info object."""
        slots = {}

        field_map = {
            'Package': ADBI_PI_NAME,
            'Version': ADBI_PI_VERSION,
            'Description': ADBI_PI_DESCRIPTION,
            'Architecture': ADBI_PI_ARCH,
            'License': ADBI_PI_LICENSE,
            'Maintainer': ADBI_PI_MAINTAINER,
            'Section': None,  # Not an APKv3 field
            'SourceDateEpoch': None,  # Not directly mapped
        }

        for key, idx in field_map.items():
            if idx and key in control_info:
                slots[idx] = self.adb.write_blob(control_info[key])

        # Dependencies
        if 'Depends' in control_info:
            deps_val = self.build_dependency_array(control_info['Depends'])
            if deps_val != ADB_VAL_NULL:
                slots[ADBI_PI_DEPENDS] = deps_val

        # Build time
        if build_time:
            slots[ADBI_PI_BUILD_TIME] = self.adb.write_int(build_time)

        # Installed size
        if installed_size:
            slots[ADBI_PI_INSTALLED_SIZE] = self.adb.write_int(installed_size)

        # Placeholder for hashes - will be filled later
        # SHA256 hash of ADB content, truncated to 20 bytes (SHA1 length)
        hash_placeholder = b'\x00' * 20
        slots[ADBI_PI_HASHES] = self.adb.write_blob(hash_placeholder)

        return self.adb.write_object(slots, ADBI_PI_MAX)

    def build_package(self, control_dir, data_dir, build_time=None,
                      control_overrides=None):
        """Build a complete package.

        Args:
            control_dir: path to control directory
            data_dir: path to data directory
            build_time: UNIX timestamp for build time
            control_overrides: optional dict of control field overrides

        Returns:
            (adb_data, file_list) where file_list is [(path_idx, file_idx, FileEntry)]
        """
        control_info = parse_control_file(os.path.join(control_dir, 'control'))
        if control_overrides:
            control_info.update(control_overrides)

        if build_time is None:
            if 'SourceDateEpoch' in control_info:
                build_time = int(control_info['SourceDateEpoch'])
            else:
                build_time = int(time.time())

        # Scan data directory
        dirs = scan_directory(data_dir) if os.path.isdir(data_dir) else []

        # Calculate installed size
        installed_size = 0
        for d in dirs:
            for f in d.files:
                if f.is_regular:
                    installed_size += f.size
        if installed_size == 0:
            installed_size = 1  # Minimum non-zero for packages with scripts

        # Build paths (directory array)
        dir_vals = []
        for d in dirs:
            dir_vals.append(self.build_dir(d, build_time))

        paths_val = self.adb.write_array(dir_vals) if dir_vals else ADB_VAL_NULL

        # Build scripts
        script_files = {}
        script_map = {
            'postinst-pkg': 'post-install',
            'prerm-pkg': 'pre-deinstall',
        }
        for fname, script_type in script_map.items():
            spath = os.path.join(control_dir, fname)
            if os.path.exists(spath):
                with open(spath, 'r') as sf:
                    content = sf.read()
                if content.strip():
                    script_files[script_type] = content

        scripts_val = self.build_scripts(script_files)

        # Build pkginfo
        pkginfo_val = self.build_pkginfo(control_info, installed_size, build_time)

        # Build root package object
        pkg_slots = {}
        pkg_slots[ADBI_PKG_PKGINFO] = pkginfo_val
        if paths_val != ADB_VAL_NULL:
            pkg_slots[ADBI_PKG_PATHS] = paths_val
        if scripts_val != ADB_VAL_NULL:
            pkg_slots[ADBI_PKG_SCRIPTS] = scripts_val

        root_val = self.adb.write_object(pkg_slots, ADBI_PKG_MAX)
        self.adb.set_root(root_val)

        # Now fix up the hashes field
        # Calculate SHA256 of the ADB data and write first 20 bytes as hash
        adb_data = self.adb.get_data()
        sha256_hash = hashlib.sha256(adb_data).digest()
        uid_hash = sha256_hash[:20]

        # Find the hashes blob in the ADB data and patch it
        adb_data_mut = bytearray(adb_data)
        # The hash blob was written as 20 zero bytes preceded by a length byte
        # We need to find and replace it
        hash_marker = b'\x14' + (b'\x00' * 20)  # Blob8: len=20 + 20 zero bytes
        idx = adb_data_mut.find(hash_marker)
        if idx >= 0:
            adb_data_mut[idx + 1:idx + 21] = uid_hash

        # Build file list for DATA blocks
        file_list = []
        for path_idx, d in enumerate(dirs):
            for file_idx, f in enumerate(d.files):
                if f.is_regular and f.size > 0:
                    file_list.append((path_idx + 1, file_idx + 1, f))

        return bytes(adb_data_mut), file_list

    def build_apk(self, control_dir, data_dir, output_path, build_time=None,
                  control_overrides=None, compression='none'):
        """Build a complete APKv3 .apk file.

        Args:
            control_dir: path to control directory
            data_dir: path to data directory
            output_path: output .apk file path
            build_time: optional UNIX timestamp
            control_overrides: optional dict of control field overrides
            compression: compression method ('none', 'deflate', or 'zstd')
        """
        adb_data, file_list = self.build_package(
            control_dir, data_dir, build_time, control_overrides)

        # Build the block stream (everything after the file header)
        block_stream = bytearray()

        # ADB block
        hdr, padding = make_block_header(ADB_BLOCK_ADB, len(adb_data))
        block_stream.extend(hdr)
        block_stream.extend(adb_data)
        if padding:
            block_stream.extend(b'\x00' * padding)

        # No SIG block for unsigned packages

        # DATA blocks
        for path_idx, file_idx, file_entry in file_list:
            data_hdr = struct.pack('<II', path_idx, file_idx)
            with open(file_entry.full_path, 'rb') as df:
                file_data = df.read()

            block_content_len = len(data_hdr) + len(file_data)
            blk_hdr, blk_padding = make_block_header(ADB_BLOCK_DATA, block_content_len)
            block_stream.extend(blk_hdr)
            block_stream.extend(data_hdr)
            block_stream.extend(file_data)
            if blk_padding:
                block_stream.extend(b'\x00' * blk_padding)

        # Build the complete uncompressed payload (file header + blocks)
        file_header = build_file_header()

        if compression == 'none':
            # No compression: write file header + blocks directly
            with open(output_path, 'wb') as f:
                f.write(file_header)
                f.write(block_stream)
        else:
            # Compression: file header is included INSIDE the compressed stream.
            # compress_stream() prepends the compression marker (ADBd/ADBc).
            payload = file_header + bytes(block_stream)
            uncompressed_size = len(payload)
            output_data = compress_stream(payload, compression)
            compressed_size = len(output_data)

            ratio = (1 - compressed_size / uncompressed_size) * 100 if uncompressed_size > 0 else 0
            print(f"  Compression: {compression}, {uncompressed_size} -> {compressed_size} bytes ({ratio:.1f}% reduction)")

            with open(output_path, 'wb') as f:
                f.write(output_data)

        print(f"Built APKv3 package: {output_path} ({os.path.getsize(output_path)} bytes)")


# ============================================================================
# Binary / Architecture Helpers (matching build.sh behavior)
# ============================================================================

# Binary naming conventions supported:
#   build.sh uses:             natfrp_service_linux_{arch}  (underscore)
#   Actual binary releases use: natfrp-service_linux_{arch}  (hyphen)
# Both are accepted so users don't have to rename their binaries.

_SVC_PATTERNS = ['natfrp_service_linux_', 'natfrp-service_linux_']
_FRPC_PATTERNS = ['frpc_linux_']

# Architecture mapping between binary/Go naming and APKv3/system naming.
# Binary files use Go-style names (amd64, arm64, ...),
# APKv3 packages use system/Alpine-style names (x86_64, aarch64, ...).
_BINARY_TO_PKG_ARCH = {
    'amd64': 'x86_64',
    'arm64': 'aarch64',
    '386': 'x86',
    'arm': 'armv7',
    'mips': 'mips',
    'mipsle': 'mipsel',
    'mips64': 'mips64',
    'mips64le': 'mips64el',
    'riscv64': 'riscv64',
}
_PKG_TO_BINARY_ARCH = {v: k for k, v in _BINARY_TO_PKG_ARCH.items()}


def _find_binary(binary_dir, prefixes, arch):
    """Find a binary file trying multiple naming conventions.

    Args:
        binary_dir: directory containing binaries
        prefixes: list of filename prefixes to try (e.g. ['natfrp_service_linux_', 'natfrp-service_linux_'])
        arch: architecture suffix (e.g. 'amd64')

    Returns:
        Full path to the found binary, or None if not found.
    """
    for prefix in prefixes:
        path = os.path.join(binary_dir, f'{prefix}{arch}')
        if os.path.isfile(path):
            return path
    return None


def binary_arch_to_pkg_arch(arch):
    """Map a binary/Go-style arch name to the APKv3/system arch name.

    E.g. 'amd64' -> 'x86_64', 'arm64' -> 'aarch64'.
    Raises an error if the arch is not in the mapping.
    """
    if arch not in _BINARY_TO_PKG_ARCH:
        raise ValueError(
            f"Unknown binary architecture '{arch}', cannot map to package architecture. "
            f"Known architectures: {', '.join(sorted(_BINARY_TO_PKG_ARCH.keys()))}")
    return _BINARY_TO_PKG_ARCH[arch]


def resolve_to_binary_arch(arch):
    """Resolve an arch name (either binary or package style) to binary/Go-style.

    Accepts both 'amd64' (binary) and 'x86_64' (package) and returns the
    binary-style name (e.g. 'amd64').
    """
    if arch in _PKG_TO_BINARY_ARCH:
        return _PKG_TO_BINARY_ARCH[arch]
    return arch


def discover_architectures(binary_dir):
    """Discover available architectures from binary/ directory.

    Looks for service binary files (both natfrp_service_linux_* and
    natfrp-service_linux_*) and extracts the arch suffix.

    This matches the build.sh pattern:
        for name in binary/natfrp_service_*; do
            build_arch ${name##*linux_}
        done
    """
    archs = []
    seen = set()

    if not os.path.isdir(binary_dir):
        print(f"[DEBUG] binary dir does not exist: {binary_dir}")
        return archs

    all_files = sorted(os.listdir(binary_dir))
    print(f"[DEBUG] scanning binary dir: {binary_dir}")
    print(f"[DEBUG] files in binary dir: {all_files}")

    for prefix in _SVC_PATTERNS:
        pattern = os.path.join(binary_dir, f'{prefix}*')
        print(f"[DEBUG] glob pattern: {pattern}")
        for path in sorted(glob.glob(pattern)):
            basename = os.path.basename(path)
            # Extract arch after "linux_"
            idx = basename.find('linux_')
            if idx >= 0:
                arch = basename[idx + len('linux_'):]
                if arch and arch not in seen:
                    print(f"[DEBUG] discovered arch '{arch}' from file: {basename}")
                    archs.append(arch)
                    seen.add(arch)

    if not archs:
        print(f"[DEBUG] no service binaries matched any pattern")
        print(f"[DEBUG] expected filenames like: natfrp_service_linux_<arch> or natfrp-service_linux_<arch>")

    return archs


def _detect_host_arch():
    """Detect the host machine architecture and map it to binary naming.

    Maps platform.machine() values to the arch suffixes used in binary filenames
    (e.g., x86_64 -> amd64, aarch64 -> arm64).
    """
    machine = platform.machine().lower()
    mapping = {
        'x86_64': 'amd64',
        'amd64': 'amd64',
        'aarch64': 'arm64',
        'arm64': 'arm64',
        'armv7l': 'arm',
        'armv6l': 'arm',
        'mips': 'mips',
        'mipsel': 'mipsle',
        'mips64': 'mips64',
        'mips64el': 'mips64le',
        'riscv64': 'riscv64',
        'i686': '386',
        'i386': '386',
    }
    arch = mapping.get(machine, machine)
    print(f"[DEBUG] host machine={machine!r}, mapped arch={arch!r}")
    return arch


def get_service_version(binary_dir, archs):
    """Extract service version from the executable service binary.

    For cross-arch packaging, we need to run the service binary that matches
    the build machine's architecture. This function:
    1. Detects the host architecture
    2. Tries the host-arch service binary first
    3. Falls back to trying all discovered arches

    Returns the service version string, or None if no binary can be executed.
    """
    host_arch = _detect_host_arch()

    # Order: try host arch first, then all others
    try_order = []
    if host_arch in archs:
        try_order.append(host_arch)
    for a in archs:
        if a not in try_order:
            try_order.append(a)

    print(f"[DEBUG] version extraction order: {try_order} (host_arch={host_arch})")

    for arch in try_order:
        svc_bin = _find_binary(binary_dir, _SVC_PATTERNS, arch)
        if not svc_bin:
            print(f"[DEBUG] no service binary found for arch {arch}")
            continue

        try:
            os.chmod(svc_bin, 0o755)
            result = subprocess.run(
                [svc_bin, '-v'], capture_output=True, text=True, timeout=10)
            version = result.stdout.strip()
            print(f"[DEBUG] ran {svc_bin} -v: stdout={result.stdout!r} stderr={result.stderr!r} rc={result.returncode}")
            if result.returncode == 0 and version:
                print(f"[DEBUG] using service version from {arch} binary: {version}")
                return version
            else:
                print(f"[DEBUG] binary {svc_bin} returned rc={result.returncode} or empty output, trying next")
        except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError) as e:
            print(f"[DEBUG] failed to execute {svc_bin}: {e}")
            continue

    print(f"[DEBUG] could not extract service version from any binary")
    return None


def prepare_data_dir_with_binaries(data_dir, binary_dir, arch):
    """Create a temporary copy of data/ with binaries from binary/ copied in.

    This matches build.sh behavior:
        cp binary/frpc_linux_$arch data/usr/bin/natfrp-frpc
        cp binary/natfrp_service_linux_$arch data/usr/bin/natfrp-service
        chmod 755 data/usr/bin/natfrp-*

    Supports both natfrp_service and natfrp-service naming.

    Returns the path to the temporary directory (caller must clean up).
    """
    tmp_dir = tempfile.mkdtemp(prefix='apkv3-build-')
    tmp_data = os.path.join(tmp_dir, 'data')
    print(f"[DEBUG] staging data to: {tmp_data}")

    # Copy original data directory
    shutil.copytree(data_dir, tmp_data, symlinks=True)

    # Remove .gitkeep files
    for root, dirs, files in os.walk(tmp_data):
        for f in files:
            if f == '.gitkeep':
                os.unlink(os.path.join(root, f))

    # Copy binaries
    usr_bin = os.path.join(tmp_data, 'usr', 'bin')
    os.makedirs(usr_bin, exist_ok=True)

    frpc_src = _find_binary(binary_dir, _FRPC_PATTERNS, arch)
    svc_src = _find_binary(binary_dir, _SVC_PATTERNS, arch)

    if frpc_src:
        dst = os.path.join(usr_bin, 'natfrp-frpc')
        print(f"[DEBUG] copy {frpc_src} -> {dst}")
        shutil.copy2(frpc_src, dst)
        os.chmod(dst, 0o755)
    else:
        print(f"[DEBUG] no frpc binary found for arch {arch}, skipping")

    if svc_src:
        dst = os.path.join(usr_bin, 'natfrp-service')
        print(f"[DEBUG] copy {svc_src} -> {dst}")
        shutil.copy2(svc_src, dst)
        os.chmod(dst, 0o755)
    else:
        print(f"[DEBUG] no service binary found for arch {arch}, skipping")

    # Log final staged file tree
    staged_files = []
    for root, dirs, files in os.walk(tmp_data):
        for f in files:
            fp = os.path.join(root, f)
            rel = os.path.relpath(fp, tmp_data)
            sz = os.path.getsize(fp)
            mode = oct(os.stat(fp).st_mode & 0o7777)
            staged_files.append(f"  {rel}  ({sz} bytes, {mode})")
    print(f"[DEBUG] staged file tree ({len(staged_files)} files):")
    for line in staged_files:
        print(line)

    return tmp_dir


# ============================================================================
# Main
# ============================================================================

def build_one(control_dir, data_dir, binary_dir, arch, output_path,
              build_time=None, version_override=None, compression='none'):
    """Build a single APKv3 package for one architecture.

    Args:
        arch: binary/Go-style architecture (e.g. 'amd64', 'arm64') or 'noarch'.
              Mapped to APKv3 package arch (e.g. 'x86_64', 'aarch64') for metadata.
        compression: compression method ('none', 'deflate', or 'zstd')

    If binary_dir has binaries for the given arch, they are copied into a
    temporary data directory for packaging (matching build.sh behavior).
    """
    tmp_dir = None
    effective_data_dir = data_dir

    # Map binary arch to package arch for APKv3 metadata
    pkg_arch = binary_arch_to_pkg_arch(arch) if arch != 'noarch' else 'noarch'

    # Check if binaries exist for this arch
    svc_bin = _find_binary(binary_dir, _SVC_PATTERNS, arch)
    frpc_bin = _find_binary(binary_dir, _FRPC_PATTERNS, arch)
    has_binaries = svc_bin is not None or frpc_bin is not None

    print(f"[DEBUG] build_one: arch={arch}, pkg_arch={pkg_arch}, svc_bin={svc_bin}, frpc_bin={frpc_bin}, has_binaries={has_binaries}")

    if has_binaries:
        tmp_dir = prepare_data_dir_with_binaries(data_dir, binary_dir, arch)
        effective_data_dir = os.path.join(tmp_dir, 'data')
        print(f"  Binaries for {arch} found, staging to temp directory")

    # Build control overrides
    control_overrides = {}
    control_overrides['Architecture'] = pkg_arch
    if version_override:
        control_overrides['Version'] = version_override

    print(f"[DEBUG] building with data_dir={effective_data_dir}, version={version_override}, pkg_arch={pkg_arch}")

    try:
        builder = APKv3Builder()
        builder.build_apk(control_dir, effective_data_dir, output_path,
                          build_time, control_overrides=control_overrides,
                          compression=compression)
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(
        description='Build APKv3 packages from scratch (pure Python3)')
    parser.add_argument('--control-dir', default='control',
                        help='Path to control directory (default: control)')
    parser.add_argument('--data-dir', default='data',
                        help='Path to data directory (default: data)')
    parser.add_argument('--binary-dir', default='binary',
                        help='Path to binary directory (default: binary)')
    parser.add_argument('--arch', default=None,
                        help='Build only for this architecture. Accepts both '
                             'binary-style (amd64, arm64) and package-style '
                             '(x86_64, aarch64) names. If omitted, builds for '
                             'all arches found in binary/, or a single noarch '
                             'package if no binaries exist.')
    parser.add_argument('--revision', '-r', default='0',
                        help='Package revision number (default: 0). '
                             'Appended as -r<N> to the service version. '
                             'Final version looks like: 3.1.8-r0')
    parser.add_argument('--output', '-o', default=None,
                        help='Output file path. Only valid with --arch.')
    parser.add_argument('--build-time', type=int, default=None,
                        help='Build timestamp (UNIX epoch)')
    parser.add_argument('--compress', '-c', default='deflate',
                        choices=['none', 'deflate', 'zstd'],
                        help='Compression method (default: deflate). '
                             'deflate uses Python built-in zlib. '
                             'zstd requires the zstandard pip package.')
    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    control_dir = os.path.join(script_dir, args.control_dir)
    data_dir = os.path.join(script_dir, args.data_dir)
    binary_dir = os.path.join(script_dir, args.binary_dir)
    release_dir = os.path.join(script_dir, 'release')

    print(f"[DEBUG] script_dir={script_dir}")
    print(f"[DEBUG] control_dir={control_dir}")
    print(f"[DEBUG] data_dir={data_dir}")
    print(f"[DEBUG] binary_dir={binary_dir}")

    if not os.path.isdir(control_dir):
        print(f"Error: Control directory not found: {control_dir}",
              file=sys.stderr)
        sys.exit(1)

    os.makedirs(release_dir, exist_ok=True)

    control_info = parse_control_file(os.path.join(control_dir, 'control'))
    pkg_name = control_info.get('Package', 'unknown')
    print(f"[DEBUG] package name: {pkg_name}")
    print(f"[DEBUG] control info: {control_info}")

    # Determine architectures to build (always resolved to binary-style names
    # since binary filenames use Go-style arch: amd64, arm64, etc.)
    if args.arch:
        # Accept both binary-style (amd64) and package-style (x86_64) names
        binary_arch = resolve_to_binary_arch(args.arch)
        archs = [binary_arch]
        print(f"[DEBUG] user-specified arch: {args.arch} -> binary arch: {binary_arch}")
    else:
        archs = discover_architectures(binary_dir)
        print(f"[DEBUG] discovered architectures: {archs}")

    if not archs:
        # No binaries, no --arch: build a single noarch package
        # (same as the old behavior)
        version = control_info.get('Version', '0')
        if '-r' not in version:
            version += f'-r{args.revision}'
        output_path = args.output or os.path.join(
            release_dir, f"{pkg_name}-{version}.apk")

        print(f"No binaries found, building noarch package...")
        print(f"[DEBUG] noarch version: {version}")
        build_one(control_dir, data_dir, binary_dir, 'noarch', output_path,
                  args.build_time, version_override=version,
                  compression=args.compress)
        return

    # Extract version from service binary.
    # For cross-arch packaging, we try the host-arch binary first (since only
    # the native binary is executable), then fall back to other arches.
    print(f"[DEBUG] extracting service version (revision={args.revision})")
    version_svc = get_service_version(binary_dir, archs)

    if version_svc:
        version = f"{version_svc}-r{args.revision}"
        print(f"Service version: {version_svc}")
    else:
        version = control_info.get('Version', '0')
        if '-r' not in version:
            version += f'-r{args.revision}'
        print(f"Could not extract version from binaries, using: {version}")

    print(f"[DEBUG] final package version: {version}")

    # Build for each architecture
    for arch in archs:
        pkg_arch = binary_arch_to_pkg_arch(arch)
        if args.output and len(archs) == 1:
            output_path = args.output
        else:
            output_path = os.path.join(
                release_dir, f"{pkg_name}-{version}-{pkg_arch}.apk")

        print(f"Building {arch} (pkg_arch={pkg_arch})...")
        build_one(control_dir, data_dir, binary_dir, arch, output_path,
                  args.build_time, version_override=version,
                  compression=args.compress)

    print(f"[DEBUG] build complete, all architectures: {archs}")


if __name__ == '__main__':
    main()
