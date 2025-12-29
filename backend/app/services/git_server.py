"""
Git server service - manages bare repos and HTTP smart protocol.
"""

import os
import shutil
from pathlib import Path
from io import BytesIO

from dulwich.repo import Repo as DulwichRepo


# Storage directory for bare repos
GIT_REPOS_DIR = Path(__file__).parent.parent.parent / "git_repos"


class GitRepoManager:
    """Manages bare git repositories for LazyAF."""

    def __init__(self, repos_dir: Path = GIT_REPOS_DIR):
        self.repos_dir = repos_dir
        self.repos_dir.mkdir(parents=True, exist_ok=True)

    def get_repo_path(self, repo_id: str) -> Path:
        """Get the path to a bare repo."""
        return self.repos_dir / f"{repo_id}.git"

    def repo_exists(self, repo_id: str) -> bool:
        """Check if a repo exists."""
        return self.get_repo_path(repo_id).exists()

    def create_bare_repo(self, repo_id: str) -> Path:
        """Create a new bare git repository."""
        repo_path = self.get_repo_path(repo_id)
        if repo_path.exists():
            raise ValueError(f"Repository {repo_id} already exists")

        # Create the directory first - dulwich init_bare on Windows
        # needs the directory to exist before initializing
        repo_path.mkdir(parents=True, exist_ok=True)

        # Create bare repo using dulwich
        DulwichRepo.init_bare(str(repo_path))
        return repo_path

    def delete_repo(self, repo_id: str) -> bool:
        """Delete a repository."""
        repo_path = self.get_repo_path(repo_id)
        if not repo_path.exists():
            return False
        shutil.rmtree(repo_path)
        return True

    def get_repo(self, repo_id: str) -> DulwichRepo | None:
        """Get a dulwich Repo object."""
        repo_path = self.get_repo_path(repo_id)
        if not repo_path.exists():
            return None
        return DulwichRepo(str(repo_path))

    def list_repos(self) -> list[str]:
        """List all repository IDs."""
        repos = []
        for path in self.repos_dir.iterdir():
            if path.is_dir() and path.suffix == ".git":
                repos.append(path.stem)
        return repos

    def get_refs(self, repo_id: str) -> dict[bytes, bytes]:
        """Get all refs for a repo."""
        repo = self.get_repo(repo_id)
        if not repo:
            return {}
        return repo.get_refs()

    def get_default_branch(self, repo_id: str) -> str | None:
        """Get the default branch (HEAD) for a repo."""
        repo = self.get_repo(repo_id)
        if not repo:
            return None
        try:
            head_ref = repo.refs.read_ref(b"HEAD")
            if head_ref and head_ref.startswith(b"ref: refs/heads/"):
                return head_ref[16:].decode("utf-8")
        except Exception:
            pass
        return None

    def list_branches(self, repo_id: str) -> list[str]:
        """List all branches in a repo."""
        refs = self.get_refs(repo_id)
        branches = []
        for ref_name in refs.keys():
            if ref_name.startswith(b"refs/heads/"):
                branch_name = ref_name[11:].decode("utf-8")  # Strip "refs/heads/"
                branches.append(branch_name)
        return sorted(branches)

    def get_branch_commit(self, repo_id: str, branch_name: str) -> str | None:
        """Get the commit SHA for a branch."""
        refs = self.get_refs(repo_id)
        ref_key = f"refs/heads/{branch_name}".encode()
        if ref_key in refs:
            # refs values are already 40-byte hex strings in dulwich
            return refs[ref_key].decode("ascii")
        return None


class HTTPGitBackend:
    """HTTP smart protocol handler for git operations."""

    def __init__(self, repo_manager: GitRepoManager):
        self.repo_manager = repo_manager

    def get_info_refs(self, repo_id: str, service: str) -> tuple[bytes, str]:
        """
        Handle GET /info/refs?service=git-upload-pack or git-receive-pack
        Returns (content, content_type)
        """
        repo = self.repo_manager.get_repo(repo_id)
        if not repo:
            raise ValueError(f"Repository {repo_id} not found")

        if service not in ("git-upload-pack", "git-receive-pack"):
            raise ValueError(f"Invalid service: {service}")

        content_type = f"application/x-{service}-advertisement"

        # Build packet-line response
        output = BytesIO()

        # Service announcement
        service_line = f"# service={service}\n".encode()
        output.write(pkt_line(service_line))
        output.write(b"0000")  # Flush packet

        # Get refs
        refs = repo.get_refs()

        if not refs:
            # Empty repo - send capabilities with zero-id
            if service == "git-upload-pack":
                caps = b"multi_ack multi_ack_detailed thin-pack side-band side-band-64k ofs-delta shallow no-progress no-done"
            else:
                # No side-band for receive-pack - simpler response handling
                caps = b"report-status delete-refs ofs-delta"
            zero_id = b"0" * 40
            output.write(pkt_line(zero_id + b" capabilities^{}\x00" + caps + b"\n"))
        else:
            # Send refs with capabilities on first line
            first = True
            if service == "git-upload-pack":
                caps = b"multi_ack multi_ack_detailed thin-pack side-band side-band-64k ofs-delta shallow no-progress include-tag allow-tip-sha1-in-want allow-reachable-sha1-in-want no-done"
            else:
                # No side-band for receive-pack - simpler response handling
                caps = b"report-status delete-refs ofs-delta"

            # HEAD first if it exists
            if b"HEAD" in refs:
                # refs values are already 40-byte hex strings in dulwich
                ref_line = refs[b"HEAD"] + b" HEAD"
                if first:
                    ref_line += b"\x00" + caps
                    first = False
                output.write(pkt_line(ref_line + b"\n"))

            # Then all other refs
            for ref_name, sha in sorted(refs.items()):
                if ref_name == b"HEAD":
                    continue
                # refs values are already 40-byte hex strings in dulwich
                ref_line = sha + b" " + ref_name
                if first:
                    ref_line += b"\x00" + caps
                    first = False
                output.write(pkt_line(ref_line + b"\n"))

        output.write(b"0000")  # Final flush
        return output.getvalue(), content_type

    def handle_upload_pack(self, repo_id: str, input_data: bytes) -> bytes:
        """
        Handle POST git-upload-pack (client wants to clone/fetch).

        Uses dulwich's pack generation to create pack data for requested objects.
        """
        import struct

        repo = self.repo_manager.get_repo(repo_id)
        if not repo:
            raise ValueError(f"Repository {repo_id} not found")

        print(f"[git_server] upload-pack: got {len(input_data)} bytes")

        try:
            input_stream = BytesIO(input_data)
            wants = []
            haves = []
            capabilities = []

            # Parse pkt-lines for wants and haves
            while True:
                len_bytes = input_stream.read(4)
                if len(len_bytes) < 4:
                    break

                pkt_len = int(len_bytes, 16)
                if pkt_len == 0:
                    # Flush packet - check if there's more (haves section)
                    continue

                if pkt_len == 1:
                    # Delimiter packet
                    continue

                line = input_stream.read(pkt_len - 4)
                if line.endswith(b'\n'):
                    line = line[:-1]

                print(f"[git_server] pkt-line: {line[:80]}")

                if line.startswith(b'want '):
                    # Parse: want <sha> [capabilities]
                    parts = line[5:].split(b' ')
                    sha = parts[0]
                    wants.append(sha)
                    if len(parts) > 1:
                        # First want line has capabilities
                        caps = b' '.join(parts[1:]).split(b' ')
                        capabilities.extend(c for c in caps if c)
                elif line.startswith(b'have '):
                    sha = line[5:].strip()
                    haves.append(sha)
                elif line == b'done':
                    break

            print(f"[git_server] wants: {[w[:8].decode() if isinstance(w, bytes) else w[:8] for w in wants]}")
            print(f"[git_server] haves: {len(haves)} objects")
            print(f"[git_server] caps: {capabilities}")

            # Debug: check pack directory
            pack_dir = Path(repo.object_store.path) / "pack"
            if pack_dir.exists():
                pack_files = list(pack_dir.glob("*.pack"))
                idx_files = list(pack_dir.glob("*.idx"))
                print(f"[git_server] pack directory has {len(pack_files)} pack files, {len(idx_files)} idx files")
                for pf in pack_files:
                    print(f"[git_server]   - {pf.name} ({pf.stat().st_size} bytes)")
            else:
                print(f"[git_server] no pack directory yet")

            # Force fresh repo to ensure packs are loaded
            repo = self.repo_manager.get_repo(repo_id)
            packs = list(repo.object_store.packs)
            print(f"[git_server] object store has {len(packs)} loaded packs")

            use_sideband = b'side-band-64k' in capabilities or b'side-band' in capabilities

            # Build pack with requested objects
            output = BytesIO()

            if wants:
                # dulwich 0.25+ expects hex SHA for object_store lookups
                def normalize_sha(sha):
                    """Ensure SHA is hex bytes format for object_store lookup."""
                    if isinstance(sha, str):
                        return sha.encode('ascii')
                    if isinstance(sha, bytes) and len(sha) == 20:
                        # Binary SHA - convert to hex
                        return sha.hex().encode('ascii')
                    return sha  # Already hex bytes

                # Collect all objects needed (use hex SHAs for dulwich 0.25+)
                object_ids = set()
                pending = list(wants)  # wants are already hex bytes
                have_set = set(haves)

                while pending:
                    sha_hex = normalize_sha(pending.pop())
                    if sha_hex in object_ids or sha_hex in have_set:
                        continue
                    object_ids.add(sha_hex)

                    try:
                        obj = repo.object_store[sha_hex]
                        # Add parent objects for commits/trees
                        if obj.type_name == b'commit':
                            pending.append(normalize_sha(obj.tree))
                            pending.extend(normalize_sha(p) for p in obj.parents)
                        elif obj.type_name == b'tree':
                            for entry in obj.items():
                                pending.append(normalize_sha(entry.sha))
                    except KeyError as e:
                        print(f"[git_server] object not found: {sha_hex[:16].decode()}")

                print(f"[git_server] packing {len(object_ids)} objects")

                import hashlib

                # Get objects for packing
                def get_objects():
                    for sha_hex in object_ids:
                        try:
                            obj = repo.object_store[sha_hex]
                            yield obj.type_num, obj.as_raw_string()
                        except KeyError as e:
                            print(f"[git_server] skip object {sha_hex[:16].decode()}: {e}")

                # Generate pack
                pack_data = BytesIO()

                # Use dulwich's write_pack_data for correct format
                entries = list(get_objects())
                print(f"[git_server] writing {len(entries)} objects to pack")

                # Write pack header manually for control
                pack_data.write(b'PACK')
                pack_data.write(struct.pack('>I', 2))  # Version 2
                pack_data.write(struct.pack('>I', len(entries)))  # Object count

                # Write objects using proper pack object format
                for type_num, data in entries:
                    self._write_pack_object_v2(pack_data, type_num, data)

                # Get pack content and calculate checksum
                pack_content = pack_data.getvalue()
                checksum = hashlib.sha1(pack_content).digest()

                pack_bytes = pack_content + checksum
                print(f"[git_server] pack size: {len(pack_bytes)} bytes")

                # Send NAK (we don't do proper negotiation for simplicity)
                if use_sideband:
                    # Sideband: band 1 = pack data, band 2 = progress
                    output.write(pkt_line(b"NAK\n"))

                    # Send pack data through sideband
                    # Max pkt-line = 65520 (0xfff0), minus 4 for length, minus 1 for band = 65515
                    chunk_size = 65515
                    offset = 0
                    while offset < len(pack_bytes):
                        chunk = pack_bytes[offset:offset + chunk_size]
                        # Band 1 prefix (pack data)
                        sideband_pkt = bytes([1]) + chunk
                        output.write(pkt_line(sideband_pkt))
                        offset += chunk_size

                    output.write(b"0000")  # Flush
                else:
                    # No sideband - just send NAK and pack data directly
                    output.write(pkt_line(b"NAK\n"))
                    output.write(pack_bytes)
                    output.write(b"0000")

            return output.getvalue()

        except Exception as e:
            import traceback
            print(f"[git_server] upload-pack error: {e}")
            traceback.print_exc()
            raise

    def _write_pack_object_v2(self, output: BytesIO, type_num: int, data: bytes) -> None:
        """Write a single object to pack format.

        Pack object format:
        - Header: variable-length encoded type (3 bits) + size
        - Data: zlib compressed object data
        """
        import zlib

        size = len(data)

        # First byte: type (bits 4-6) + size bits 0-3
        c = (type_num << 4) | (size & 0x0f)
        size >>= 4

        # Continue with size if more bits needed
        while size:
            output.write(bytes([c | 0x80]))
            c = size & 0x7f
            size >>= 7
        output.write(bytes([c]))

        # Write zlib compressed data (git uses default compression)
        compressed = zlib.compress(data)
        output.write(compressed)

    def handle_receive_pack(self, repo_id: str, input_data: bytes) -> bytes:
        """
        Handle POST git-receive-pack (client wants to push).

        Stores the pack file directly in objects/pack/ with an index file.
        This is more robust than unpacking to loose objects, especially
        for packs with delta objects.
        """
        import struct

        repo = self.repo_manager.get_repo(repo_id)
        if not repo:
            raise ValueError(f"Repository {repo_id} not found")

        print(f"[git_server] receive-pack: got {len(input_data)} bytes")

        try:
            # Parse the incoming data
            # Format: [ref-update-lines] [PACK data]
            # Each ref update: old-sha new-sha ref-name\n
            # Ends with flush (0000) then PACK

            input_stream = BytesIO(input_data)
            output_lines = []

            # Read pkt-lines for ref updates
            ref_updates = []
            while True:
                # Read 4-byte length
                len_bytes = input_stream.read(4)
                if len(len_bytes) < 4:
                    break

                pkt_len = int(len_bytes, 16)
                if pkt_len == 0:
                    # Flush packet - end of ref updates
                    break

                # Read the line (pkt_len includes the 4 length bytes)
                line = input_stream.read(pkt_len - 4)
                if line.endswith(b'\n'):
                    line = line[:-1]

                # Parse: old-sha new-sha ref-name[\0capabilities]
                parts = line.split(b'\x00')[0].split(b' ')
                if len(parts) >= 3:
                    old_sha, new_sha, ref_name = parts[0], parts[1], parts[2]
                    ref_updates.append((old_sha, new_sha, ref_name))
                    print(f"[git_server] ref update: {ref_name.decode()} {old_sha[:8].decode()}..{new_sha[:8].decode()}")

            # Rest is PACK data
            pack_data = input_stream.read()
            print(f"[git_server] pack data: {len(pack_data)} bytes")

            # Track if pack import succeeded - only update refs if it did
            pack_import_success = False
            pack_import_error = None

            if pack_data and pack_data.startswith(b'PACK'):
                print(f"[git_server] importing pack ({len(pack_data)} bytes)")

                # Parse pack header for logging
                assert pack_data[:4] == b'PACK'
                version = struct.unpack('>I', pack_data[4:8])[0]
                num_objects = struct.unpack('>I', pack_data[8:12])[0]
                print(f"[git_server] pack version {version}, {num_objects} objects")

                try:
                    # Use dulwich's add_thin_pack which handles delta resolution
                    # and stores the pack properly in the object store
                    pack_stream = BytesIO(pack_data)

                    def read_all(size):
                        return pack_stream.read(size)

                    def read_some(size):
                        return pack_stream.read(size)

                    print(f"[git_server] importing thin pack via object store...")
                    pack = repo.object_store.add_thin_pack(read_all, read_some)
                    print(f"[git_server] pack imported successfully")

                    # Verify first ref update object is accessible
                    test_sha = ref_updates[0][1] if ref_updates else None
                    if test_sha and test_sha != b'0' * 40:
                        try:
                            obj = repo.object_store[test_sha]
                            print(f"[git_server] verified: {test_sha[:8].decode()} is {obj.type_name.decode()}")
                            pack_import_success = True
                        except KeyError:
                            pack_import_error = f"Object {test_sha[:8].decode()} not accessible after import"
                            print(f"[git_server] ERROR: {pack_import_error}")
                    else:
                        # No objects to verify (delete-only push or empty)
                        pack_import_success = True

                except Exception as e:
                    import traceback
                    pack_import_error = str(e)
                    print(f"[git_server] pack import error: {e}")
                    traceback.print_exc()
            else:
                # No pack data (delete-only push)
                pack_import_success = True

            # Only apply ref updates if pack import succeeded
            if not pack_import_success:
                # Return error response
                output = BytesIO()
                output.write(pkt_line(f"unpack error: {pack_import_error or 'unknown error'}\n".encode()))
                output.write(b"0000")
                return output.getvalue()

            # Apply ref updates
            for old_sha, new_sha, ref_name in ref_updates:
                try:
                    # Convert hex to bytes if needed
                    if new_sha != b'0' * 40:
                        repo.refs[ref_name] = new_sha
                        print(f"[git_server] updated ref {ref_name.decode()}")
                        output_lines.append(f"ok {ref_name.decode()}")
                    else:
                        # Delete ref
                        del repo.refs[ref_name]
                        output_lines.append(f"ok {ref_name.decode()}")
                except Exception as e:
                    print(f"[git_server] ref update error: {e}")
                    output_lines.append(f"ng {ref_name.decode()} {e}")

            # Build response in pkt-line format
            output = BytesIO()
            output.write(pkt_line(b"unpack ok\n"))
            for line in output_lines:
                output.write(pkt_line(f"{line}\n".encode()))
            output.write(b"0000")  # Flush packet

            return output.getvalue()

        except Exception as e:
            import traceback
            print(f"[git_server] receive-pack error: {e}")
            traceback.print_exc()
            raise


def pkt_line(data: bytes) -> bytes:
    """Encode data as a git pkt-line."""
    length = len(data) + 4  # +4 for the length prefix itself
    return f"{length:04x}".encode() + data


# Singleton instances
git_repo_manager = GitRepoManager()
git_backend = HTTPGitBackend(git_repo_manager)
