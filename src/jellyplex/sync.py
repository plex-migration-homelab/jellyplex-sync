import errno
import logging
import os
import pathlib
import re
from collections.abc import Generator
from dataclasses import dataclass
import glob as pyglob
from typing import Optional

from .library import (
    ACCEPTED_ASSOCIATED_SUFFIXES,
    ACCEPTED_VIDEO_SUFFIXES,
    MovieInfo,
    VideoInfo,
    MediaLibrary,
)
from .jellyfin import (
    JellyfinLibrary,
)
from .plex import (
    PlexLibrary,
)
from . import utils

# Optional xattr support for MergerFS branch detection
try:
    import xattr
    _XATTR_AVAILABLE = True
except ImportError:
    _XATTR_AVAILABLE = False

log = logging.getLogger(__name__)


# ============================================================================
# MergerFS Support Functions
# ============================================================================

MERGERFS_XATTR_BASEPATH = b"user.mergerfs.basepath"
MERGERFS_XATTR_RELPATH = b"user.mergerfs.relpath"
MERGERFS_XATTR_FULLPATH = b"user.mergerfs.fullpath"


def get_mergerfs_info(filepath: pathlib.Path) -> tuple[str, str] | tuple[None, None]:
    """Returns (branch, relpath) from MergerFS xattrs, or (None, None) if not available.

    Args:
        filepath: Path to a file or directory in the merged filesystem

    Returns:
        Tuple of (branch_path, relative_path) or (None, None) if xattrs unavailable
        or not a MergerFS mount
    """
    if not _XATTR_AVAILABLE:
        return None, None

    try:
        attrs = xattr.xattr(str(filepath))
        branch = attrs.get(MERGERFS_XATTR_BASEPATH)
        relpath = attrs.get(MERGERFS_XATTR_RELPATH)

        # Decode and strip null terminators that MergerFS may include
        branch_str = branch.decode("utf-8").rstrip("\x00")
        relpath_str = relpath.decode("utf-8").rstrip("\x00")

        return branch_str, relpath_str
    except (KeyError, OSError, UnicodeDecodeError):
        # KeyError: attribute doesn't exist (not MergerFS or not configured)
        # OSError: file doesn't exist or no permissions
        # UnicodeDecodeError: unexpected encoding in xattr
        return None, None


def get_mergerfs_fullpath(filepath: pathlib.Path) -> str | None:
    """Returns the full physical path from MergerFS xattrs, or None if not available.

    Args:
        filepath: Path to a file or directory in the merged filesystem

    Returns:
        Full physical path on underlying branch, or None if not available
    """
    if not _XATTR_AVAILABLE:
        return None

    try:
        attrs = xattr.xattr(str(filepath))
        fullpath = attrs.get(MERGERFS_XATTR_FULLPATH)
        return fullpath.decode("utf-8").rstrip("\x00")
    except (KeyError, OSError, UnicodeDecodeError):
        return None


def is_colocated(src: pathlib.Path, dst: pathlib.Path) -> tuple[bool, str | None]:
    """Check if source file and destination directory are on the same MergerFS branch.

    For MergerFS filesystems, returns (True, None) if both paths are on the same
    underlying branch. If on different branches, returns (False, reason_message).

    For non-MergerFS filesystems, returns (True, None) always.

    Args:
        src: Source file path (must exist)
        dst: Destination path (parent directory must exist for checking)

    Returns:
        Tuple of (can_hardlink, reason_if_cannot)
    """
    src_branch, _ = get_mergerfs_info(src)

    # Not a MergerFS mount or xattrs unavailable - assume OK
    if src_branch is None:
        return True, None

    # We have MergerFS - check if destination directory exists and is on same branch
    dst_dir = dst.parent if dst.is_file() or dst.suffix else dst

    if not dst_dir.exists():
        # Destination directory doesn't exist yet - we can't verify colocation
        # This is a case where we need to rely on precreation happening correctly
        # Return True and let the hardlink attempt fail if it would cross branches
        return True, None

    dst_branch, _ = get_mergerfs_info(dst_dir)

    # If we can't determine dst branch, assume it might be OK
    if dst_branch is None:
        return True, None

    if src_branch != dst_branch:
        reason = (
            f"Cross-branch hardlink attempt: source on '{src_branch}', "
            f"destination directory on '{dst_branch}'. "
            f"Target must be on same physical disk as source for hardlinks to work."
        )
        return False, reason

    return True, None


def get_physical_path(filepath: pathlib.Path) -> pathlib.Path:
    """Get the physical path on the underlying branch for a MergerFS path.

    Useful for logging and debugging. Returns the original path if not MergerFS
    or if xattrs are unavailable.

    Args:
        filepath: Path in the merged filesystem

    Returns:
        Path to the file on the underlying physical branch
    """
    fullpath = get_mergerfs_fullpath(filepath)
    if fullpath:
        return pathlib.Path(fullpath)
    return filepath


# ============================================================================
# Minimum number of items expected in source library to proceed with --delete
# Protects against wiping target when source mount fails
MIN_SOURCE_ITEMS_FOR_DELETE = 1


def is_source_empty_or_unmounted(source_path: pathlib.Path) -> bool:
    """Check if source directory appears empty or unmounted.

    This safeguard prevents accidental deletion of target content when the source
    filesystem is not properly mounted (e.g., NFS/CIFS mount failure).

    Returns True if the source directory:
    - Contains no subdirectories at all
    - Is completely empty
    """
    try:
        # Use os.scandir for efficient directory scanning (avoids stat calls)
        with os.scandir(source_path) as entries:
            dir_count = 0
            for entry in entries:
                # Only count directories (movie folders)
                if entry.is_dir(follow_symlinks=False):
                    dir_count += 1
                    if dir_count >= MIN_SOURCE_ITEMS_FOR_DELETE:
                        return False
            return dir_count == 0
    except OSError as e:
        # Permission denied or other access errors suggest mount issues
        log.error("Cannot access source directory '%s': %s", source_path, e)
        return True


def are_same_filesystem(
    path1: pathlib.Path, path2: pathlib.Path, mergerfs_branches: Optional[list[str]] = None
) -> tuple[bool, bool]:
    """Check if two paths are on the same filesystem with MergerFS awareness.

    For MergerFS filesystems, this function ALWAYS returns True to allow the sync
    to proceed. The actual colocation checks happen at the per-file level in
    is_colocated() and safe_hardlink(). This is necessary because with MergerFS
    create=ff policy, base directories can be on different branches than the
    files within them.

    For regular filesystems, compares st_dev from stat().

    Args:
        path1: First path to check
        path2: Second path to check
        mergerfs_branches: Optional list of known MergerFS branch paths for
            additional validation (e.g., ["/mnt/disk1", "/mnt/disk2"])

    Returns:
        Tuple of (is_same, is_mergerfs):
        - is_same: True if paths are on the same filesystem/branch (always True for MergerFS)
        - is_mergerfs: True if path1 appears to be on a MergerFS mount
    """
    # First try MergerFS detection
    branch1, _ = get_mergerfs_info(path1)
    branch2, _ = get_mergerfs_info(path2)

    if branch1 is not None or branch2 is not None:
        # We're on MergerFS - skip base directory check entirely
        # The per-file colocation checks in safe_hardlink() will handle it
        log.debug(
            "MergerFS detected (branch1=%s, branch2=%s). Skipping base directory check.",
            branch1, branch2
        )
        return True, True

    # Neither is MergerFS (or xattrs unavailable) - fall back to st_dev
    try:
        stat1 = path1.stat()
        stat2 = path2.stat()

        # Additional check: if mergerfs_branches provided, verify neither path
        # is directly under one of the branches (would indicate the caller
        # should be using the merged path instead)
        if mergerfs_branches:
            path1_str = str(path1.resolve())
            path2_str = str(path2.resolve())
            for branch in mergerfs_branches:
                if path1_str.startswith(branch) or path2_str.startswith(branch):
                    log.warning(
                        "Direct branch path detected. Using branch paths directly "
                        "bypasses MergerFS. Consider using the merged path for consistency."
                    )
                    break

        return stat1.st_dev == stat2.st_dev, False
    except OSError:
        # If we can't stat, assume different filesystems to be safe
        return False, False


def _check_library_colocation(
    source_lib: MediaLibrary,
    target_lib: MediaLibrary,
    mergerfs_branches: list[str] | None,
    verbose: bool = False,
) -> bool:
    """Check that sample files from source can be colocated with target directories.

    This preflight check samples a few files from the source library and verifies
    that their corresponding target directories are on the same MergerFS branch.
    Helps catch misconfigured MergerFS precreation early.

    Args:
        source_lib: Source media library
        target_lib: Target media library
        mergerfs_branches: Optional list of known branch paths for detailed logging
        verbose: Whether to print verbose progress messages

    Returns:
        True if all sampled files are properly colocated, False otherwise
    """
    sample_size = min(10, sum(1 for _ in source_lib.scan()))
    if sample_size == 0:
        log.warning("No movies found for colocation check")
        return True

    checked = 0
    failed = 0

    for entry, movie in source_lib.scan():
        if checked >= sample_size:
            break

        target_name = target_lib.movie_name(movie)
        target_path = target_lib.base_dir / target_name

        # Find a sample file in the source entry
        try:
            for f in entry.iterdir():
                if f.is_file():
                    can_link, reason = is_colocated(f, target_path / f.name)
                    if not can_link:
                        log.error(
                            "Colocation check failed for '%s': %s",
                            entry.name, reason
                        )
                        failed += 1
                    elif verbose:
                        src_branch, _ = get_mergerfs_info(f)
                        log.debug(
                            "Colocation OK for '%s' (branch: %s)",
                            entry.name, src_branch or "unknown"
                        )
                    checked += 1
                    break
        except OSError as e:
            log.warning("Cannot check colocation for '%s': %s", entry.name, e)

    if failed > 0:
        log.error(
            "Colocation check: %d/%d samples failed. "
            "Target directories may be on wrong branches.",
            failed, checked
        )
        return False

    if verbose:
        log.info("Colocation check: %d/%d samples OK", checked, sample_size)

    return True


def verify_hardlink(source: pathlib.Path, target: pathlib.Path) -> bool:
    """Verify that target is a valid hard link to source by comparing physical inodes.

    For MergerFS filesystems, resolves physical paths on underlying branches
    before comparing (st_dev, st_ino) tuples. This prevents false negatives
    caused by inode virtualization on the merged mount.

    Returns True if both files share the same physical inode (valid hard link),
    False otherwise.
    """
    try:
        # Resolve physical paths for MergerFS-aware verification
        source_phys = get_physical_path(source)
        target_phys = get_physical_path(target)

        source_stat = source_phys.stat()
        target_stat = target_phys.stat()

        # Compare both device and inode - both must match for a valid hardlink
        return (source_stat.st_dev, source_stat.st_ino) == (target_stat.st_dev, target_stat.st_ino)
    except OSError:
        return False


def repair_hardlink(
    source: pathlib.Path,
    target: pathlib.Path,
    *,
    dry_run: bool = False,
) -> bool:
    """Repair a broken hard link by deleting target and recreating the link.

    Returns True on successful repair, False on failure.
    """
    try:
        source_inode = source.stat().st_ino
        target_inode = target.stat().st_ino
    except OSError as e:
        log.error("Cannot stat files for repair: %s", e)
        return False

    log.warning(
        "Broken hard link detected: %s (inode %d) != %s (inode %d)",
        target, target_inode, source, source_inode
    )

    if dry_run:
        log.info("REPAIR %s", target)
        return True

    try:
        target.unlink()
        if safe_hardlink(source, target):
            log.info("Repaired hard link: %s", target)
            return True
        return False
    except OSError as e:
        log.error("Failed to repair hard link '%s': %s", target, e)
        return False


@dataclass
class VerifyStats:
    """Statistics for hard link verification."""
    files_checked: int = 0
    links_valid: int = 0
    links_broken: int = 0
    links_repaired: int = 0


def _resolve_physical_target(source: pathlib.Path, target: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path] | tuple[None, None]:
    """Resolve physical paths for MergerFS hardlinking.

    Uses xattrs to determine the physical branch where source resides,
    then computes the corresponding physical target path on that same branch.

    Args:
        source: Source file path (on MergerFS merged mount)
        target: Target file path (on MergerFS merged mount)

    Returns:
        Tuple of (physical_source, physical_target) or (None, None) if resolution fails
    """
    # Get physical source path via xattr
    src_phys_str = get_mergerfs_fullpath(source)
    if not src_phys_str:
        log.debug("Cannot resolve physical path for source: %s", source)
        return None, None

    # Get source branch root
    src_branch, src_relpath = get_mergerfs_info(source)
    if not src_branch:
        log.debug("Cannot determine MergerFS branch for source: %s", source)
        return None, None

    try:
        # Simple approach:
        # 1. The xattrs tell us the source relpath from the mergerfs root
        # 2. We can determine the mergerfs mount point by removing relpath from source
        # 3. Apply the same logic to target
        
        # Example:
        #   Source: /mnt/storage/Media/movies/Movie/file.mkv
        #   Src relpath: Media/movies/Movie/file.mkv
        #   Mount point: /mnt/storage (source minus relpath)
        #   
        #   Target: /mnt/storage/Media/jellyfin/movies/Movie/file.mkv
        #   Target relpath from mount: Media/jellyfin/movies/Movie/file.mkv
        #   Physical target: /mnt/storage-base/Media/jellyfin/movies/Movie/file.mkv
        
        source_str = str(source)
        if not src_relpath or not source_str.endswith(src_relpath):
            log.debug("Source path doesn't end with relpath: %s, %s", source, src_relpath)
            return None, None
            
        # Determine mount point by stripping the relpath from source
        mount_point = pathlib.Path(source_str[:-len(src_relpath)].rstrip('/'))
        
        # Get target's relative path from the mount point
        try:
            target_rel = target.relative_to(mount_point)
        except ValueError:
            log.debug("Target not under mount point %s: %s", mount_point, target)
            return None, None
        
        # Construct physical target: branch + target_relative
        phys_target = pathlib.Path(src_branch) / target_rel
        
        return pathlib.Path(src_phys_str), phys_target
            # If source is /mnt/storage/Media/movies/Movie/file.mkv
            # and src_relpath is Media/movies/Movie/file.mkv
            # then the mount point is /mnt/storage
            
            # Find the mount point by removing src_relpath from source
            source_str = str(source)
            if src_relpath and source_str.endswith(src_relpath):
                mount_point = pathlib.Path(source_str[:-len(src_relpath)])
                
                # Now compute target relative to mount point
                try:
                    target_rel = target.relative_to(mount_point)
                    phys_target = pathlib.Path(src_branch) / target_rel
                except ValueError:
                    log.debug("Target not under mount point %s: %s", mount_point, target)
                    return None, None
            else:
                log.debug("Cannot determine mount point from source: %s, relpath: %s", 
                         source, src_relpath)
                return None, None
        
        return pathlib.Path(src_phys_str), phys_target

    except Exception as e:
        log.debug("Failed to resolve physical target: %s", e)
        return None, None


def safe_hardlink(source: pathlib.Path, target: pathlib.Path) -> bool:
    """Create a hardlink with proper error handling and MergerFS awareness.

    For MergerFS filesystems, first attempts hardlink on merged paths.
    On EXDEV (cross-device), resolves physical paths on the underlying branch
    and retries the hardlink on the physical filesystem.

    Returns True on success, False on failure.
    Handles cross-device links (EXDEV) and permission errors (EACCES) gracefully.
    """
    try:
        target.hardlink_to(source)
        return True
    except OSError as e:
        if e.errno == errno.EXDEV:
            # Cross-device link - attempt physical path fallback for MergerFS
            log.debug("EXDEV on merged path '%s' -> '%s', resolving physical paths", source, target)

            phys_source, phys_target = _resolve_physical_target(source, target)

            if not phys_source or not phys_target:
                log.error(
                    "Cannot hardlink '%s' -> '%s': Cross-device link (EXDEV). "
                    "Failed to resolve physical paths. Ensure xattr support is available "
                    "and paths are on a MergerFS mount.",
                    source, target
                )
                return False

            try:
                # Create directory structure on physical branch
                phys_target_parent = phys_target.parent
                phys_target_parent.mkdir(parents=True, exist_ok=True)

                # Create hardlink using physical paths
                phys_target.hardlink_to(phys_source)

                log.info(
                    "Created physical hardlink: %s -> %s (branch: %s)",
                    phys_source, phys_target, phys_target_parent.parts[2] if len(phys_target_parent.parts) > 2 else "unknown"
                )
                return True

            except Exception as phys_err:
                log.error(
                    "Cannot hardlink '%s' -> '%s': Cross-device link (EXDEV). "
                    "Physical fallback failed: %s. "
                    "Source: '%s', Target: '%s'. "
                    "Ensure target directories exist on the same branch as source files.",
                    source, target, phys_err, phys_source, phys_target
                )
                return False

        elif e.errno == errno.EACCES:
            log.error(
                "Permission denied creating hardlink '%s' -> '%s'. "
                "Check file permissions and ownership.",
                source, target
            )
        elif e.errno == errno.EEXIST:
            log.warning("Target file already exists: '%s'", target)
        elif e.errno == errno.ENOENT:
            log.error("Source file not found: '%s'", source)
        else:
            log.error("Failed to create hardlink '%s' -> '%s': %s", source, target, e)
        return False


@dataclass
class LibraryStats:
    movies_total: int = 0
    movies_processed: int = 0
    items_removed: int = 0


def resolve_movie_folder(source_lib: MediaLibrary, partial_path: str) -> pathlib.Path | None:
    """Resolves a partial path to a valid folder in the source library."""
    if not partial_path:
        return None
    path = pathlib.Path(partial_path)

    # If path exists and is absolute or relative to cwd
    if path.exists() and path.is_dir():
        # Check if it is inside source_lib
        try:
            # This checks if path is subpath of source_lib.base_dir
            # We resolve to handle symlinks or relative paths
            if source_lib.base_dir.resolve() in path.resolve().parents or source_lib.base_dir.resolve() == path.resolve():
                return path
        except Exception:
            pass

    # Try matching by folder name
    # This handles Docker path remapping
    folder_name = path.name
    candidate = source_lib.base_dir / folder_name
    if candidate.exists() and candidate.is_dir():
        return candidate

    return None


def scan_media_library(
    source: MediaLibrary,
    target: MediaLibrary,
    *,
    dry_run: bool = False,
    delete: bool = False,
    stats: LibraryStats | None = None,
) -> Generator[tuple[pathlib.Path, pathlib.Path, MovieInfo], None, None]:
    """Iterate over the source library and determine all movie folders.
    Yields a tuple for each movie folder:
        (source: pathlib.Path, destination: pathlib.Path, movie: MovieInfo)
    """
    if source is target or source.base_dir == target.base_dir:
        raise ValueError("Can not transfer library into itself")

    stats = stats or LibraryStats()
    movies_to_sync: dict[str, tuple[pathlib.Path, MovieInfo] | None] = {}
    conflicting_source_dirs: dict[str, list[str]] = {}

    # Inspect source libary for movie folders to sync
    for entry, movie in source.scan():
        target_name = target.movie_name(movie)
        if target_name in movies_to_sync:
            if target_name not in conflicting_source_dirs:
                item = movies_to_sync[target_name]
                conflicting_source_dirs[target_name] = [item[0].name] if item else []
            conflicting_source_dirs[target_name].append(entry.name)
            movies_to_sync[target_name] = None
        else:
            movies_to_sync[target_name] = (entry, movie)
        stats.movies_total += 1

    # If there are any conflicts we bail out now
    if conflicting_source_dirs:
        for dst, src in conflicting_source_dirs.items():
            quoted = [f"'{s}'" for s in src]
            log.error(f"Conflicting folders: {', '.join(quoted)} → '{dst}'")
        log.info("You have to solve the conflicts first to proceed")
        return

    # Yield items for sync
    for target_name, item in movies_to_sync.items():
        if not item:
            continue
        stats.movies_processed += 1
        yield item[0], target.base_dir / target_name, item[1]

    # Remove stray items in target library
    for entry in target.base_dir.iterdir():
        if entry.name not in movies_to_sync:
            if delete:
                if dry_run:
                    log.info("DELETE %s", entry)
                else:
                    log.info("Removing stray item '%s' in target library", entry.name)
                    utils.remove(entry)
                stats.items_removed += 1
            else:
                if not dry_run:
                    log.info("Stray item '%s' found", entry.name)


@dataclass
class AssetStats:
    files_total: int = 0
    files_linked: int = 0
    items_removed: int = 0
    links_verified: int = 0
    links_broken: int = 0
    links_repaired: int = 0


def process_assets_folder(
    source_path: pathlib.Path,
    target_path: pathlib.Path,
    *,
    dry_run: bool = False,
    delete: bool = False,
    verbose: bool = False,
    verify_only: bool = False,
    skip_verify: bool = False,
    stats: AssetStats | None = None,
) -> AssetStats:
    if not source_path.is_dir():
        raise ValueError(f"{source_path!s} is not a folder")

    # In verify_only mode, skip creating directories
    if not target_path.exists():
        if verify_only:
            # Nothing to verify if target doesn't exist
            return stats if stats else AssetStats()
        if dry_run:
            log.info("MKDIR  %s", target_path)
        else:
            target_path.mkdir(parents=True, exist_ok=True)

    stats = stats if stats else AssetStats()
    synced_items = {}

    # Hardlink missing files and dive into subfolders
    for entry in source_path.iterdir():
        # Skip symlinks to avoid unexpected behavior
        if entry.is_symlink():
            log.debug("Skipping symlink '%s'", entry.name)
            continue

        dest = target_path / entry.name
        if entry.is_dir():
            process_assets_folder(
                entry, dest,
                verbose=verbose,
                stats=stats,
                dry_run=dry_run,
                delete=delete,
                verify_only=verify_only,
                skip_verify=skip_verify,
            )
        elif entry.is_file():
            # Skip zero-byte files (likely placeholders or corrupt)
            try:
                if entry.stat().st_size == 0:
                    log.debug("Skipping zero-byte file '%s'", entry.name)
                    continue
            except OSError:
                continue

            if dest.exists():
                try:
                    if dest.samefile(entry):
                        # Files match by samefile(), now verify inode if not skipping
                        if not skip_verify:
                            stats.links_verified += 1
                            if not verify_hardlink(entry, dest):
                                stats.links_broken += 1
                                if verify_only:
                                    log.warning(
                                        "Broken hard link: %s -> %s (would repair)",
                                        dest, entry
                                    )
                                elif repair_hardlink(entry, dest, dry_run=dry_run):
                                    stats.links_repaired += 1
                        if verbose:
                            log.debug("Target file '%s' already exists, skipping", entry.name)
                    else:
                        # Target exists but is not a hardlink to source - relink
                        if verify_only:
                            log.warning("Target '%s' exists but is not linked to source", dest)
                            stats.links_broken += 1
                        elif dry_run:
                            log.info("RELINK %s", entry)
                        else:
                            dest.unlink()
                            if safe_hardlink(entry, dest):
                                stats.files_linked += 1
                except OSError as e:
                    log.warning("Cannot check file '%s': %s", dest, e)
                    continue
            else:
                # Target doesn't exist - create link (unless verify_only)
                if verify_only:
                    # Nothing to verify if target doesn't exist
                    pass
                elif dry_run:
                    log.info("LINK   %s", dest)
                    # Do not increment stats.files_linked in dry-run mode
                elif safe_hardlink(entry, dest):
                    stats.files_linked += 1
            stats.files_total += 1
        synced_items[entry.name] = dest

    if delete and not verify_only and target_path.is_dir():
        # Remove stray items
        for entry in target_path.iterdir():
            if entry.name in synced_items:
                continue
            log.info("Removing stray item '%s' in target folder", entry.name)
            if dry_run:
                log.info("DELETE %s", entry.name)
            else:
                utils.remove(entry)
            stats.items_removed += 1

    return stats


@dataclass
class MovieStats:
    videos_total: int = 0
    videos_linked: int = 0
    items_removed: int = 0
    asset_items_total: int = 0
    asset_items_linked: int = 0
    asset_items_removed: int = 0
    links_verified: int = 0
    links_broken: int = 0
    links_repaired: int = 0


def process_movie(
    source: MediaLibrary,
    target: MediaLibrary,
    source_path: pathlib.Path,
    movie: MovieInfo,
    *,
    dry_run: bool = False,
    delete: bool = False,
    verbose: bool = False,
    update_filenames: bool = False,
    verify_only: bool = False,
    skip_verify: bool = False,
) -> MovieStats:
    target_path = target.movie_path(movie)

    if verbose:
        log.info(f"Processing '{source_path.name}' → '{target_path.name}'")

    stats = MovieStats()

    videos_to_sync: dict[str, tuple[pathlib.Path, pathlib.Path]] = {}
    assets_to_sync: dict[str, tuple[pathlib.Path, pathlib.Path]] = {}

    # Scan for video files and assets using os.scandir for efficiency
    try:
        entries_list = list(os.scandir(source_path))
    except OSError as e:
        log.error("Failed to scan movie folder '%s': %s", source_path, e)
        return MovieStats()

    for dir_entry in entries_list:
        try:
            entry = pathlib.Path(dir_entry.path)
            is_file = dir_entry.is_file(follow_symlinks=False)
            is_dir = dir_entry.is_dir(follow_symlinks=False)
        except OSError:
            continue

        if is_file and entry.suffix.lower() in ACCEPTED_VIDEO_SUFFIXES:
            video = source.parse_video_path(entry)
            video_path = target.video_path(movie, video or VideoInfo(extension=entry.suffix.lower()))
            video_name = video_path.name
            if video_name in videos_to_sync:
                log.error("Conflicting video file '%s'. Aborting.", entry.name)
                return MovieStats()
            videos_to_sync[video_name] = (entry, video_path)
            stats.videos_total += 1

            # Find associated files
            base_stem = entry.stem
            target_stem = video_path.stem
            # Use glob.escape because filename may contain brackets which are special chars in glob
            for associated_entry in source_path.glob(f"{pyglob.escape(base_stem)}.*"):
                if associated_entry == entry:
                    continue
                # Associated extensions we want to sync
                if associated_entry.suffix.lower() not in ACCEPTED_ASSOCIATED_SUFFIXES:
                    continue

                # Construct target name: replace base_stem with target_stem
                # Example: Movie.mkv -> Movie.en.srt
                # Target:  Movie-Edition.mkv -> Movie-Edition.en.srt
                suffix_part = associated_entry.name[len(base_stem):]
                target_associated_name = f"{target_stem}{suffix_part}"
                target_associated_path = target_path / target_associated_name

                if target_associated_name in assets_to_sync:
                    # Should not happen usually given unique mapping
                    continue

                assets_to_sync[target_associated_name] = (associated_entry, target_associated_path)

        elif is_dir:
            dir_name = entry.name
            # Skip hidden directories and symlinks
            if dir_name.startswith("."):
                log.debug("Ignoring hidden folder '%s'", dir_name)
                continue
            assets_to_sync[dir_name] = (entry, target_path / dir_name)

    # In verify_only mode, skip creating directories
    if not target_path.exists():
        if verify_only:
            # Nothing to verify if target doesn't exist
            return stats
        if dry_run:
            log.info("MKDIR  %s", target_path)
        else:
            target_path.mkdir(parents=True, exist_ok=True)

    # Pre-scan target directory to build a map of existing inodes
    # This optimizes stale candidate detection by avoiding repeated directory scans
    existing_inodes: dict[int, pathlib.Path] = {}
    # Track stale files that should be preserved (not renamed but still valid hardlinks)
    preserved_stale_files: set[str] = set()

    if target_path.exists():
        for candidate in target_path.iterdir():
            if not candidate.is_file():
                continue
            if candidate.suffix.lower() not in ACCEPTED_VIDEO_SUFFIXES:
                continue
            try:
                existing_inodes[candidate.stat().st_ino] = candidate
            except OSError:
                # File might have been deleted or permission denied
                pass

    # Hardlink missing video files
    for _video_name, item in videos_to_sync.items():
        if item[1].exists():
            if item[1].samefile(item[0]):
                # Files match by samefile(), now verify inode if not skipping
                if not skip_verify:
                    stats.links_verified += 1
                    if not verify_hardlink(item[0], item[1]):
                        stats.links_broken += 1
                        if verify_only:
                            log.warning(
                                "Broken hard link: %s -> %s (would repair)",
                                item[1], item[0]
                            )
                        elif repair_hardlink(item[0], item[1], dry_run=dry_run):
                            stats.links_repaired += 1
                if verbose:
                    log.info("Target video file '%s' already exists", item[1].name)
                continue
            else:
                # Target exists but is not a hardlink to source - relink
                if verify_only:
                    log.warning("Target '%s' exists but is not linked to source", item[1])
                    stats.links_broken += 1
                    continue
                log.info("Replacing video file '%s' → '%s'", item[0].name, item[1].name)
                if dry_run:
                    log.info("DELETE %s", item[1])
                else:
                    item[1].unlink()
        else:
            # Check if any existing file in the target folder is a hardlink to the source file
            # This happens if the filename has changed (e.g. edition added)
            stale_candidate: pathlib.Path | None = None
            try:
                source_inode = item[0].stat().st_ino
                if source_inode in existing_inodes:
                    stale_candidate = existing_inodes[source_inode]
            except OSError:
                 # Source file might have been removed during processing
                 pass

            if stale_candidate:
                # We found a file that is hardlinked to source but has wrong name
                # Verify if editions match
                intended_video = target.parse_video_path(item[1])
                candidate_video = target.parse_video_path(stale_candidate)

                # Relaxed check: trust the inode (it's the same physical file) if update_filenames is requested.
                # When update_filenames is True, we trust the inode match and rename regardless of edition compatibility,
                # which may change edition tags or rename files even if editions don't match or can't be parsed.
                # Otherwise, only rename if the editions match exactly.
                editions_match = intended_video and candidate_video and intended_video.edition == candidate_video.edition

                if update_filenames or editions_match:
                    if update_filenames:
                        if dry_run:
                            log.info("RENAME %s -> %s", stale_candidate.name, item[1].name)
                        else:
                            log.info("Renamed '%s' -> '%s'", stale_candidate.name, item[1].name)
                            try:
                                stale_candidate.rename(item[1])
                            except OSError as e:
                                log.error("Failed to rename video file '%s': %s", stale_candidate, e)
                                continue

                        # Rename associated files
                        stale_stem = stale_candidate.stem
                        target_stem = item[1].stem
                        for assoc_file in item[1].parent.iterdir():
                            if assoc_file == stale_candidate or assoc_file == item[1]:
                                continue
                            if not assoc_file.is_file():
                                continue
                            if assoc_file.suffix.lower() not in ACCEPTED_ASSOCIATED_SUFFIXES:
                                continue

                            # Match by stem prefix
                            if assoc_file.name.startswith(stale_stem + "."):
                                suffix_part = assoc_file.name[len(stale_stem):]
                                new_assoc_name = f"{target_stem}{suffix_part}"
                                new_assoc_path = item[1].parent / new_assoc_name

                                if dry_run:
                                    log.info("RENAME %s -> %s", assoc_file.name, new_assoc_name)
                                else:
                                    log.info("Renamed '%s' -> '%s'", assoc_file.name, new_assoc_name)
                                    try:
                                        assoc_file.rename(new_assoc_path)
                                    except OSError as e:
                                        log.warning("Failed to rename associated file '%s': %s", assoc_file.name, e)

                        # Remove from inode map to avoid processing again
                        if source_inode in existing_inodes:
                            del existing_inodes[source_inode]
                        continue
                    else:
                        log.warning("Stale hardlink '%s' should be '%s'. Use --update-filenames to fix.", stale_candidate.name, item[1].name)
                        # Preserve the stale file so it isn't deleted during cleanup
                        # (it's still a valid hardlink to the source, just with wrong name)
                        preserved_stale_files.add(stale_candidate.name)
                        # Also preserve any associated files with the stale name
                        stale_stem = stale_candidate.stem
                        for assoc in target_path.iterdir():
                            if assoc.is_file() and assoc.name.startswith(stale_stem + "."):
                                if assoc.suffix.lower() in ACCEPTED_ASSOCIATED_SUFFIXES:
                                    preserved_stale_files.add(assoc.name)
                        continue

        # Create new link (unless verify_only)
        if verify_only:
            # Nothing to verify if target doesn't exist
            pass
        elif dry_run:
            log.info("LINK   %s", item[1])
            stats.videos_linked += 1
        else:
            log.info("Linking video file '%s' → '%s'", item[0].name, item[1].name)
            if safe_hardlink(item[0], item[1]):
                stats.videos_linked += 1

    if delete and not verify_only and target_path.is_dir():
        # Remove stray items (but preserve stale files that are still valid hardlinks)
        for entry in target_path.iterdir():
            if entry.name in videos_to_sync or entry.name in assets_to_sync:
                continue
            # Don't delete preserved stale files (valid hardlinks with outdated names)
            if entry.name in preserved_stale_files:
                continue
            if dry_run:
                log.info("DELETE %s", entry)
            else:
                log.info(
                    "Removing stray item '%s' in movie folder '%s'",
                    entry.name,
                    target_path.relative_to(target.base_dir),
                )
                utils.remove(entry)
            stats.items_removed += 1

    # Sync assets folders and associated files
    for _, item in assets_to_sync.items():
        # Skip symlinks
        if item[0].is_symlink():
            log.debug("Skipping symlink '%s'", item[0].name)
            continue

        if item[0].is_dir():
            s = process_assets_folder(
                item[0], item[1],
                delete=delete,
                verbose=verbose,
                dry_run=dry_run,
                verify_only=verify_only,
                skip_verify=skip_verify,
            )
            stats.asset_items_total += s.files_total
            stats.asset_items_linked += s.files_linked
            stats.asset_items_removed += s.items_removed
            stats.links_verified += s.links_verified
            stats.links_broken += s.links_broken
            stats.links_repaired += s.links_repaired
        elif item[0].is_file():
            # Skip zero-byte files
            try:
                if item[0].stat().st_size == 0:
                    log.debug("Skipping zero-byte associated file '%s'", item[0].name)
                    continue
            except OSError:
                continue

            # Handle associated files
            if item[1].exists():
                try:
                    if item[1].samefile(item[0]):
                        # Files match by samefile(), now verify inode if not skipping
                        if not skip_verify:
                            stats.links_verified += 1
                            if not verify_hardlink(item[0], item[1]):
                                stats.links_broken += 1
                                if verify_only:
                                    log.warning(
                                        "Broken hard link: %s -> %s (would repair)",
                                        item[1], item[0]
                                    )
                                elif repair_hardlink(item[0], item[1], dry_run=dry_run):
                                    stats.links_repaired += 1
                        if verbose:
                            log.debug("Target asset file '%s' already exists, skipping", item[1].name)
                    else:
                        # Target exists but is not a hardlink to source - relink
                        if verify_only:
                            log.warning("Target '%s' exists but is not linked to source", item[1])
                            stats.links_broken += 1
                        elif dry_run:
                            log.info("RELINK %s", item[0])
                            stats.asset_items_linked += 1
                        else:
                            item[1].unlink()
                            if safe_hardlink(item[0], item[1]):
                                stats.asset_items_linked += 1
                except OSError as e:
                    log.warning("Cannot check associated file '%s': %s", item[1], e)
                    continue
            else:
                # Target doesn't exist - create link (unless verify_only)
                if verify_only:
                    # Nothing to verify if target doesn't exist
                    pass
                elif dry_run:
                    log.info("LINK   %s", item[1])
                    stats.asset_items_linked += 1
                elif safe_hardlink(item[0], item[1]):
                    stats.asset_items_linked += 1
            stats.asset_items_total += 1

    return stats


def _scan_for_video_files(path: pathlib.Path, max_files: int = 100) -> Generator[pathlib.Path, None, None]:
    """Efficiently scan for video files using os.scandir to avoid unnecessary stat calls.

    Limits scanning to max_files to prevent performance issues on large libraries.
    Uses breadth-first traversal to sample from multiple movie folders.
    """
    files_found = 0
    dirs_to_scan = [path]

    while dirs_to_scan and files_found < max_files:
        current_dir = dirs_to_scan.pop(0)
        try:
            with os.scandir(current_dir) as entries:
                for entry in entries:
                    if files_found >= max_files:
                        return
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            dirs_to_scan.append(pathlib.Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            entry_path = pathlib.Path(entry.path)
                            if entry_path.suffix.lower() in ACCEPTED_VIDEO_SUFFIXES:
                                yield entry_path
                                files_found += 1
                    except OSError:
                        # Skip entries we can't access
                        continue
        except OSError:
            # Skip directories we can't access
            continue


def determine_library_type(path: pathlib.Path) -> type[MediaLibrary] | None:
    """Determine library type by sampling video files.

    Uses efficient os.scandir-based traversal instead of rglob to avoid
    stat calls on large libraries. Samples up to 100 files for detection.
    """
    plex_hints: int = 0
    jellyfin_hints: int = 0

    for entry in _scan_for_video_files(path, max_files=100):
        fname = entry.stem
        # Check for provider id - definitive markers
        if re.search(r"\[[a-z]+id-[^\]]+\]", fname, flags=re.IGNORECASE):
            return JellyfinLibrary
        if re.search(r"\{[a-z]+-[^\}]+\}", fname, flags=re.IGNORECASE):
            return PlexLibrary
        # Check for Plex edition - definitive marker
        if re.search(r"\{edition-[^\}]+\}", fname, flags=re.IGNORECASE):
            return PlexLibrary
        # Check for hints
        variant = fname.split(" - ")
        if len(variant) > 1 and re.search(r"\(\d{4}\)", variant[-1]) is None:
            jellyfin_hints += 1
        if re.search(r"\[\d{3,4}[pi]\]", fname, flags=re.IGNORECASE):
            plex_hints += 1
        if re.search(r"\[[a-z0-9\.\,]+\]", fname, flags=re.IGNORECASE):
            plex_hints += 1

    if plex_hints > jellyfin_hints:
        return PlexLibrary
    elif jellyfin_hints > plex_hints:
        return JellyfinLibrary
    return None


def sync(
    source: str,
    target: str,
    *,
    dry_run: bool = False,
    delete: bool = False,
    create: bool = False,
    verbose: bool = False,
    debug: bool = False,
    convert_to: str | None = None,
    update_filenames: bool = False,
    partial_path: str | None = None,
    verify_only: bool = False,
    skip_verify: bool = False,
    mergerfs_branches: list[str] | None = None,
    check_colocation: bool = False,
) -> int:
    if debug:
        logging.getLogger().setLevel(logging.DEBUG)

    source_path = pathlib.Path(source)
    target_path = pathlib.Path(target)

    if not convert_to or convert_to == "auto":
        source_type = determine_library_type(source_path)
        if not source_type:
            log.error("Unable to determine source library type, please provide --convert-to option")
            return 1
        target_type = PlexLibrary if source_type == JellyfinLibrary else JellyfinLibrary
    elif convert_to in (JellyfinLibrary.shortname(), PlexLibrary.shortname()):
        target_type = PlexLibrary if convert_to == PlexLibrary.shortname() else JellyfinLibrary
        source_type = PlexLibrary if target_type == JellyfinLibrary else JellyfinLibrary
    else:
        raise ValueError("Unknown value for parameter 'convert_to'")

    source_lib = source_type(source_path)
    target_lib = target_type(target_path)

    if dry_run:
        log.info("SOURCE %s", source_lib.base_dir)
        log.info("TARGET %s", target_lib.base_dir)
        log.info("CONVERTING %s TO %s", source_lib.shortname().capitalize(), target_lib.shortname().capitalize())
    else:
        log.info("Syncing '%s' (%s) to '%s' (%s)",
            source_lib.base_dir,
            source_lib.shortname().capitalize(),
            target_lib.base_dir,
            target_lib.shortname().capitalize(),
        )

    if not source_lib.base_dir.is_dir():
        log.error("Source directory '%s' does not exist", source_lib.base_dir)
        return 1

    # Safeguard: Check if source directory appears empty (possible mount failure)
    # This prevents accidentally wiping the target when --delete is used
    if delete and not partial_path and is_source_empty_or_unmounted(source_lib.base_dir):
        log.error(
            "Source directory '%s' appears empty or unmounted. "
            "Aborting to prevent accidental deletion of target content. "
            "Please verify the source filesystem is properly mounted.",
            source_lib.base_dir
        )
        return 1

    if not target_lib.base_dir.is_dir():
        if create:
            target_lib.base_dir.mkdir(parents=True)
        else:
            log.error("Target directory '%s' does not exist", target_lib.base_dir)
            return 1

    # Check if source and target are on the same filesystem (required for hard links)
    same_fs, is_mergerfs = are_same_filesystem(source_lib.base_dir, target_lib.base_dir)
    if not same_fs:
        if is_mergerfs:
            log.error(
                "Source '%s' and target '%s' are on different MergerFS branches. "
                "Hard links require both paths to be on the same underlying filesystem branch. "
                "Ensure target directories are pre-created on the same disk as source files. "
                "See the --mergerfs-branches option for branch validation.",
                source_lib.base_dir, target_lib.base_dir
            )
        else:
            log.error(
                "Source '%s' and target '%s' are on different filesystems. "
                "Hard links require both paths to be on the same filesystem. "
                "Consider using a symbolic link or copy-based sync instead.",
                source_lib.base_dir, target_lib.base_dir
            )
        return 1

    # Optional colocation check: verify sample files can be hardlinked to their
    # corresponding target directories (catches misconfigured MergerFS early)
    if check_colocation and is_mergerfs:
        log.info("Running colocation check on sample files...")
        colocation_ok = _check_library_colocation(
            source_lib, target_lib, mergerfs_branches, verbose=verbose
        )
        if not colocation_ok:
            log.error(
                "Colocation check failed. Some source files cannot be hardlinked to "
                "target directories because they are on different MergerFS branches. "
                "Ensure target directories are pre-created on the same disk as source files."
            )
            return 1
        log.info("Colocation check passed: all sample files are properly colocated")

    if verify_only:
        log.info("VERIFY-ONLY mode: Checking existing hard links without making changes")

    stat_movies: int = 0
    stat_items_linked: int = 0
    stat_items_removed: int = 0
    stat_links_verified: int = 0
    stat_links_broken: int = 0
    stat_links_repaired: int = 0
    lib_stats = LibraryStats()

    if partial_path:
        # Partial sync logic
        movie_folder = resolve_movie_folder(source_lib, partial_path)
        if not movie_folder:
            log.error(f"Could not resolve movie folder for partial path: {partial_path}")
            return 1

        movie_info = source_lib.parse_movie_path(movie_folder)
        if not movie_info:
            log.warning(f"Could not parse movie info from folder: {movie_folder}")
            # We skip this as we cannot process it without parsed info
            return 1
        else:
            s = process_movie(
                source_lib,
                target_lib,
                movie_folder,
                movie_info,
                delete=delete,
                verbose=verbose,
                dry_run=dry_run,
                update_filenames=update_filenames,
                verify_only=verify_only,
                skip_verify=skip_verify,
            )
            stat_movies += 1
            stat_items_linked += s.asset_items_linked + s.videos_linked
            stat_items_removed += s.asset_items_removed + s.items_removed
            stat_links_verified += s.links_verified
            stat_links_broken += s.links_broken
            stat_links_repaired += s.links_repaired
    else:
        for src, _, movie in scan_media_library(source_lib, target_lib, delete=delete, dry_run=dry_run, stats=lib_stats):
            s = process_movie(
                source_lib,
                target_lib,
                src,
                movie,
                delete=delete,
                verbose=verbose,
                dry_run=dry_run,
                update_filenames=update_filenames,
                verify_only=verify_only,
                skip_verify=skip_verify,
            )
            stat_movies += 1
            stat_items_linked += s.asset_items_linked + s.videos_linked
            stat_items_removed += s.asset_items_removed + s.items_removed
            stat_links_verified += s.links_verified
            stat_links_broken += s.links_broken
            stat_links_repaired += s.links_repaired

        stat_items_removed += lib_stats.items_removed

    # Build summary message
    if verify_only:
        summary = (
            f"Verification complete: {stat_movies} movies checked, "
            f"{stat_links_verified} links verified, "
            f"{stat_links_broken} broken links found."
        )
    elif skip_verify:
        summary = (
            f"Summary: {stat_movies} movies found, "
            f"{stat_items_linked} files updated, "
            f"{stat_items_removed} files removed. "
            "(inode verification skipped)"
        )
    else:
        summary_parts = [
            f"Summary: {stat_movies} movies found",
            f"{stat_items_linked} files updated",
            f"{stat_items_removed} files removed",
        ]
        if stat_links_verified > 0:
            summary_parts.append(f"{stat_links_verified} links verified")
        if stat_links_broken > 0:
            summary_parts.append(f"{stat_links_broken} broken links found")
        if stat_links_repaired > 0:
            summary_parts.append(f"{stat_links_repaired} links repaired")
        summary = ", ".join(summary_parts) + "."
    logging.info(summary)

    return 0
