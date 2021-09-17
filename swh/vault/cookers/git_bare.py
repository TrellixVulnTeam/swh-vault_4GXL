# Copyright (C) 2021  The Software Heritage developers
# See the AUTHORS file at the top-level directory of this distribution
# License: GNU General Public License version 3, or any later version
# See top-level LICENSE file for more information

"""
This cooker creates tarballs containing a bare .git directory,
that can be unpacked and cloned like any git repository.

It works in three steps:

1. Write objects one by one in :file:`.git/objects/`
2. Calls ``git repack`` to pack all these objects into git packfiles.
3. Creates a tarball of the resulting repository

It keeps a set of all written (or about-to-be-written) object hashes in memory
to avoid downloading and writing the same objects twice.
"""

import datetime
import enum
import glob
import logging
import os.path
import re
import subprocess
import tarfile
import tempfile
from typing import Any, Dict, Iterable, Iterator, List, NoReturn, Optional, Set, Tuple
import zlib

from swh.core.api.classes import stream_results_optional
from swh.model import identifiers
from swh.model.hashutil import hash_to_bytehex, hash_to_hex
from swh.model.model import (
    Content,
    DirectoryEntry,
    ObjectType,
    Person,
    Release,
    Revision,
    RevisionType,
    Sha1Git,
    Snapshot,
    SnapshotBranch,
    TargetType,
    TimestampWithTimezone,
)
from swh.storage.algos.revisions_walker import DFSRevisionsWalker
from swh.storage.algos.snapshot import snapshot_get_all_branches
from swh.vault.cookers.base import BaseVaultCooker
from swh.vault.to_disk import HIDDEN_MESSAGE, SKIPPED_MESSAGE

RELEASE_BATCH_SIZE = 10000
REVISION_BATCH_SIZE = 10000
DIRECTORY_BATCH_SIZE = 10000
CONTENT_BATCH_SIZE = 100


logger = logging.getLogger(__name__)


class RootObjectType(enum.Enum):
    DIRECTORY = "directory"
    REVISION = "revision"
    SNAPSHOT = "snapshot"


def assert_never(value: NoReturn, msg) -> NoReturn:
    """mypy makes sure this function is never called, through exhaustive checking
    of ``value`` in the parent function.

    See https://mypy.readthedocs.io/en/latest/literal_types.html#exhaustive-checks
    for details.
    """
    assert False, msg


class GitBareCooker(BaseVaultCooker):
    BUNDLE_TYPE = "git_bare"
    SUPPORTED_OBJECT_TYPES = {
        identifiers.ObjectType[obj_type.name] for obj_type in RootObjectType
    }

    use_fsck = True

    obj_type: RootObjectType

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.obj_type = RootObjectType[self.swhid.object_type.name]

    def check_exists(self) -> bool:
        if self.obj_type is RootObjectType.REVISION:
            return not list(self.storage.revision_missing([self.obj_id]))
        elif self.obj_type is RootObjectType.DIRECTORY:
            return not list(self.storage.directory_missing([self.obj_id]))
        elif self.obj_type is RootObjectType.SNAPSHOT:
            return not list(self.storage.snapshot_missing([self.obj_id]))
        else:
            assert_never(self.obj_type, f"Unexpected root object type: {self.obj_type}")

    def _push(self, stack: List[Sha1Git], obj_ids: Iterable[Sha1Git]) -> None:
        assert not isinstance(obj_ids, bytes)
        revision_ids = [id_ for id_ in obj_ids if id_ not in self._seen]
        self._seen.update(revision_ids)
        stack.extend(revision_ids)

    def _pop(self, stack: List[Sha1Git], n: int) -> List[Sha1Git]:
        obj_ids = stack[-n:]
        stack[-n:] = []
        return obj_ids

    def prepare_bundle(self):
        # Objects we will visit soon:
        self._rel_stack: List[Sha1Git] = []
        self._rev_stack: List[Sha1Git] = []
        self._dir_stack: List[Sha1Git] = []
        self._cnt_stack: List[Sha1Git] = []

        # Set of objects already in any of the stacks:
        self._seen: Set[Sha1Git] = set()
        self._walker_state: Optional[Any] = None

        # Set of errors we expect git-fsck to raise at the end:
        self._expected_fsck_errors = set()

        with tempfile.TemporaryDirectory(prefix="swh-vault-gitbare-") as workdir:
            # Initialize a Git directory
            self.workdir = workdir
            self.gitdir = os.path.join(workdir, "clone.git")
            os.mkdir(self.gitdir)
            self.init_git()

            # Add the root object to the stack of objects to visit
            self.push_subgraph(self.obj_type, self.obj_id)

            # Load and write all the objects to disk
            self.load_objects()

            # Write the root object as a ref (this step is skipped if it's a snapshot)
            # This must be done before repacking; git-repack ignores orphan objects.
            self.write_refs()

            if self.use_fsck:
                self.git_fsck()

            self.repack()

            self.write_archive()

    def init_git(self) -> None:
        subprocess.run(["git", "-C", self.gitdir, "init", "--bare"], check=True)
        self.create_object_dirs()

        # Remove example hooks; they take ~40KB and we don't use them
        for filename in glob.glob(os.path.join(self.gitdir, "hooks", "*.sample")):
            os.unlink(filename)

    def create_object_dirs(self) -> None:
        # Create all possible dirs ahead of time, so we don't have to check for
        # existence every time.
        for byte in range(256):
            try:
                os.mkdir(os.path.join(self.gitdir, "objects", f"{byte:02x}"))
            except FileExistsError:
                pass

    def repack(self) -> None:
        # Add objects we wrote in a pack
        try:
            subprocess.run(["git", "-C", self.gitdir, "repack", "-d"], check=True)
        except subprocess.CalledProcessError:
            logging.exception("git-repack failed with:")

        # Remove their non-packed originals
        subprocess.run(["git", "-C", self.gitdir, "prune-packed"], check=True)

    def git_fsck(self) -> None:
        proc = subprocess.run(
            ["git", "-C", self.gitdir, "fsck"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env={"LANG": "C.utf8"},
        )

        # Split on newlines not followed by a space
        errors = re.split("\n(?! )", proc.stdout.decode())

        errors = [
            error for error in errors if error and not error.startswith("warning ")
        ]

        unexpected_errors = set(errors) - self._expected_fsck_errors
        if unexpected_errors:
            logging.error(
                "Unexpected errors from git-fsck after cooking %s: %s",
                self.swhid,
                "\n".join(sorted(unexpected_errors)),
            )

    def write_refs(self, snapshot=None):
        refs: Dict[bytes, bytes]  # ref name -> target
        if self.obj_type == RootObjectType.DIRECTORY:
            # We need a synthetic revision pointing to the directory
            author = Person.from_fullname(
                b"swh-vault, git-bare cooker <robot@softwareheritage.org>"
            )
            dt = datetime.datetime.now(tz=datetime.timezone.utc)
            dt = dt.replace(microsecond=0)  # not supported by git
            date = TimestampWithTimezone.from_datetime(dt)
            revision = Revision(
                author=author,
                committer=author,
                date=date,
                committer_date=date,
                message=b"Initial commit",
                type=RevisionType.GIT,
                directory=self.obj_id,
                synthetic=True,
            )
            self.write_revision_node(revision.to_dict())
            refs = {b"refs/heads/master": hash_to_bytehex(revision.id)}
        elif self.obj_type == RootObjectType.REVISION:
            refs = {b"refs/heads/master": hash_to_bytehex(self.obj_id)}
        elif self.obj_type == RootObjectType.SNAPSHOT:
            if snapshot is None:
                # refs were already written in a previous step
                return
            branches = []
            for (branch_name, branch) in snapshot.branches.items():
                if branch is None:
                    logging.error(
                        "%s has dangling branch: %r", snapshot.swhid(), branch_name
                    )
                else:
                    branches.append((branch_name, branch))
            refs = {
                branch_name: (
                    b"ref: " + branch.target
                    if branch.target_type == TargetType.ALIAS
                    else hash_to_bytehex(branch.target)
                )
                for (branch_name, branch) in branches
            }
        else:
            assert_never(self.obj_type, f"Unexpected root object type: {self.obj_type}")

        for (ref_name, ref_target) in refs.items():
            path = os.path.join(self.gitdir.encode(), ref_name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fd:
                fd.write(ref_target)

    def write_archive(self):
        with tarfile.TarFile(mode="w", fileobj=self.fileobj) as tf:
            tf.add(self.gitdir, arcname=f"{self.swhid}.git", recursive=True)

    def _obj_path(self, obj_id: Sha1Git):
        return os.path.join(self.gitdir, self._obj_relative_path(obj_id))

    def _obj_relative_path(self, obj_id: Sha1Git):
        obj_id_hex = hash_to_hex(obj_id)
        directory = obj_id_hex[0:2]
        filename = obj_id_hex[2:]
        return os.path.join("objects", directory, filename)

    def object_exists(self, obj_id: Sha1Git) -> bool:
        return os.path.exists(self._obj_path(obj_id))

    def write_object(self, obj_id: Sha1Git, obj: bytes) -> bool:
        """Writes a git object on disk.

        Returns whether it was already written."""
        # Git requires objects to be zlib-compressed; but repacking decompresses and
        # removes them, so we don't need to compress them too much.
        data = zlib.compress(obj, level=1)

        with open(self._obj_path(obj_id), "wb") as fd:
            fd.write(data)
        return True

    def push_subgraph(self, obj_type: RootObjectType, obj_id) -> None:
        if self.obj_type is RootObjectType.REVISION:
            self.push_revision_subgraph(obj_id)
        elif self.obj_type is RootObjectType.DIRECTORY:
            self._push(self._dir_stack, [obj_id])
        elif self.obj_type is RootObjectType.SNAPSHOT:
            self.push_snapshot_subgraph(obj_id)
        else:
            assert_never(self.obj_type, f"Unexpected root object type: {self.obj_type}")

    def load_objects(self) -> None:
        while self._rel_stack or self._rev_stack or self._dir_stack or self._cnt_stack:
            release_ids = self._pop(self._rel_stack, RELEASE_BATCH_SIZE)
            if release_ids:
                self.load_releases(release_ids)

            revision_ids = self._pop(self._rev_stack, REVISION_BATCH_SIZE)
            if revision_ids:
                self.load_revisions(revision_ids)

            directory_ids = self._pop(self._dir_stack, DIRECTORY_BATCH_SIZE)
            if directory_ids:
                self.load_directories(directory_ids)

            content_ids = self._pop(self._cnt_stack, CONTENT_BATCH_SIZE)
            if content_ids:
                self.load_contents(content_ids)

    def push_revision_subgraph(self, obj_id: Sha1Git) -> None:
        """Fetches a revision and all its children, and writes them to disk"""
        loaded_from_graph = False

        if self.graph:
            from swh.graph.client import GraphArgumentException

            # First, try to cook using swh-graph, as it is more efficient than
            # swh-storage for querying the history
            obj_swhid = identifiers.CoreSWHID(
                object_type=identifiers.ObjectType.REVISION, object_id=obj_id,
            )
            try:
                revision_ids = (
                    swhid.object_id
                    for swhid in map(
                        identifiers.CoreSWHID.from_string,
                        self.graph.visit_nodes(str(obj_swhid), edges="rev:rev"),
                    )
                )
                self._push(self._rev_stack, revision_ids)
            except GraphArgumentException as e:
                logger.info(
                    "Revision %s not found in swh-graph, falling back to fetching "
                    "history using swh-storage. %s",
                    hash_to_hex(obj_id),
                    e.args[0],
                )
            else:
                loaded_from_graph = True

        if not loaded_from_graph:
            # If swh-graph is not available, or the revision is not yet in
            # swh-graph, fall back to self.storage.revision_log.
            # self.storage.revision_log also gives us the full revisions,
            # so we load them right now instead of just pushing them on the stack.
            walker = DFSRevisionsWalker(self.storage, obj_id, state=self._walker_state)
            for revision in walker:
                self.write_revision_node(revision)
                self._push(self._dir_stack, [revision["directory"]])
            # Save the state, so the next call to the walker won't return the same
            # revisions
            self._walker_state = walker.export_state()

    def push_snapshot_subgraph(self, obj_id: Sha1Git) -> None:
        """Fetches a snapshot and all its children, and writes them to disk"""
        loaded_from_graph = False

        if self.graph:
            revision_ids = []
            release_ids = []
            directory_ids = []
            content_ids = []

            from swh.graph.client import GraphArgumentException

            # First, try to cook using swh-graph, as it is more efficient than
            # swh-storage for querying the history
            obj_swhid = identifiers.CoreSWHID(
                object_type=identifiers.ObjectType.SNAPSHOT, object_id=obj_id,
            )
            try:
                swhids: Iterable[identifiers.CoreSWHID] = map(
                    identifiers.CoreSWHID.from_string,
                    self.graph.visit_nodes(str(obj_swhid), edges="snp:*,rel:*,rev:rev"),
                )
                for swhid in swhids:
                    if swhid.object_type is identifiers.ObjectType.REVISION:
                        revision_ids.append(swhid.object_id)
                    elif swhid.object_type is identifiers.ObjectType.RELEASE:
                        release_ids.append(swhid.object_id)
                    elif swhid.object_type is identifiers.ObjectType.DIRECTORY:
                        directory_ids.append(swhid.object_id)
                    elif swhid.object_type is identifiers.ObjectType.CONTENT:
                        content_ids.append(swhid.object_id)
                    elif swhid.object_type is identifiers.ObjectType.SNAPSHOT:
                        assert (
                            swhid.object_id == obj_id
                        ), f"Snapshot {obj_id.hex()} references a different snapshot"
                    else:
                        assert_never(
                            swhid.object_type, f"Unexpected SWHID object type: {swhid}"
                        )
            except GraphArgumentException as e:
                logger.info(
                    "Snapshot %s not found in swh-graph, falling back to fetching "
                    "history for each branch. %s",
                    hash_to_hex(obj_id),
                    e.args[0],
                )
            else:
                self._push(self._rev_stack, revision_ids)
                self._push(self._rel_stack, release_ids)
                self._push(self._dir_stack, directory_ids)
                self._push(self._cnt_stack, content_ids)
                loaded_from_graph = True

        # TODO: when self.graph is available and supports edge labels, use it
        # directly to get branch names.
        snapshot: Optional[Snapshot] = snapshot_get_all_branches(self.storage, obj_id)
        assert snapshot, "Unknown snapshot"  # should have been caught by check_exists()
        for branch in snapshot.branches.values():
            if not loaded_from_graph:
                if branch is None:
                    logging.warning("Dangling branch: %r", branch)
                    continue
                assert isinstance(branch, SnapshotBranch)  # for mypy
                if branch.target_type is TargetType.REVISION:
                    self.push_revision_subgraph(branch.target)
                elif branch.target_type is TargetType.RELEASE:
                    self.push_releases_subgraphs([branch.target])
                elif branch.target_type is TargetType.ALIAS:
                    # Nothing to do, this for loop also iterates on the target branch
                    # (if it exists)
                    pass
                elif branch.target_type is TargetType.DIRECTORY:
                    self._push(self._dir_stack, [branch.target])
                elif branch.target_type is TargetType.CONTENT:
                    self._push(self._cnt_stack, [branch.target])
                elif branch.target_type is TargetType.SNAPSHOT:
                    if swhid.object_id != obj_id:
                        raise NotImplementedError(
                            f"{swhid} has a snapshot as a branch."
                        )
                else:
                    assert_never(
                        branch.target_type, f"Unexpected target type: {self.obj_type}"
                    )

        self.write_refs(snapshot=snapshot)

    def load_revisions(self, obj_ids: List[Sha1Git]) -> None:
        """Given a list of revision ids, loads these revisions and their directories;
        but not their parent revisions."""
        ret: List[Optional[Revision]] = self.storage.revision_get(obj_ids)

        revisions: List[Revision] = list(filter(None, ret))
        if len(ret) != len(revisions):
            logger.error("Missing revision(s), ignoring them.")

        for revision in revisions:
            self.write_revision_node(revision.to_dict())
        self._push(self._dir_stack, (rev.directory for rev in revisions))

    def write_revision_node(self, revision: Dict[str, Any]) -> bool:
        """Writes a revision object to disk"""
        git_object = identifiers.revision_git_object(revision)
        return self.write_object(revision["id"], git_object)

    def load_releases(self, obj_ids: List[Sha1Git]) -> List[Release]:
        """Loads release objects, and returns them."""
        ret = self.storage.release_get(obj_ids)

        releases = list(filter(None, ret))
        if len(ret) != len(releases):
            logger.error("Missing release(s), ignoring them.")

        for release in releases:
            self.write_release_node(release.to_dict())

        return releases

    def push_releases_subgraphs(self, obj_ids: List[Sha1Git]) -> None:
        """Given a list of release ids, loads these releases and adds their
        target to the list of objects to visit"""
        for release in self.load_releases(obj_ids):
            assert release.target, "{release.swhid(}) has no target"
            if release.target_type is ObjectType.REVISION:
                self.push_revision_subgraph(release.target)
            elif release.target_type is ObjectType.DIRECTORY:
                self._push(self._dir_stack, [release.target])
            elif release.target_type is ObjectType.CONTENT:
                self._push(self._cnt_stack, [release.target])
            elif release.target_type is ObjectType.RELEASE:
                self.push_releases_subgraphs([release.target])
            elif release.target_type is ObjectType.SNAPSHOT:
                raise NotImplementedError(
                    f"{release.swhid()} targets a snapshot: {release.target!r}"
                )
            else:
                assert_never(
                    release.target_type,
                    f"Unexpected release target type: {release.target_type}",
                )

    def write_release_node(self, release: Dict[str, Any]) -> bool:
        """Writes a release object to disk"""
        git_object = identifiers.release_git_object(release)
        return self.write_object(release["id"], git_object)

    def load_directories(self, obj_ids: List[Sha1Git]) -> None:
        for obj_id in obj_ids:
            self.load_directory(obj_id)

    def load_directory(self, obj_id: Sha1Git) -> None:
        # Load the directory
        entries_it: Optional[Iterable[DirectoryEntry]] = stream_results_optional(
            self.storage.directory_get_entries, obj_id
        )

        if entries_it is None:
            logger.error("Missing swh:1:dir:%s, ignoring.", hash_to_hex(obj_id))
            return

        entries = [entry.to_dict() for entry in entries_it]
        directory = {"id": obj_id, "entries": entries}
        git_object = identifiers.directory_git_object(directory)
        self.write_object(obj_id, git_object)

        # Add children to the stack
        entry_loaders: Dict[str, Optional[List[Sha1Git]]] = {
            "file": self._cnt_stack,
            "dir": self._dir_stack,
            "rev": None,  # Do not include submodule targets (rejected by git-fsck)
        }
        for entry in directory["entries"]:
            stack = entry_loaders[entry["type"]]
            if stack is not None:
                self._push(stack, [entry["target"]])

    def load_contents(self, obj_ids: List[Sha1Git]) -> None:
        # TODO: add support of filtered objects, somehow?
        # It's tricky, because, by definition, we can't write a git object with
        # the expected hash, so git-fsck *will* choke on it.
        contents = self.storage.content_get(obj_ids, "sha1_git")

        visible_contents = []
        for (obj_id, content) in zip(obj_ids, contents):
            if content is None:
                # FIXME: this may also happen for missing content
                self.write_content(obj_id, SKIPPED_MESSAGE)
                self._expect_mismatched_object_error(obj_id)
            elif content.status == "visible":
                visible_contents.append(content)
            elif content.status == "hidden":
                self.write_content(obj_id, HIDDEN_MESSAGE)
                self._expect_mismatched_object_error(obj_id)
            elif content.status == "absent":
                assert False, f"content_get returned absent content {content.swhid()}"
            else:
                # TODO: When content.status will have type Literal, replace this with
                # assert_never
                assert False, f"{content.swhid} has status: {content.status!r}"

        contents_and_data: Iterator[Tuple[Content, Optional[bytes]]]
        if self.objstorage is None:
            contents_and_data = (
                (content, self.storage.content_get_data(content.sha1))
                for content in visible_contents
            )
        else:
            contents_and_data = zip(
                visible_contents,
                self.objstorage.get_batch(c.sha1 for c in visible_contents),
            )

        for (content, datum) in contents_and_data:
            if datum is None:
                logger.error(
                    "{content.swhid()} is visible, but is missing data. Skipping."
                )
                continue
            self.write_content(content.sha1_git, datum)

    def write_content(self, obj_id: Sha1Git, content: bytes) -> None:
        header = identifiers.git_object_header("blob", len(content))
        self.write_object(obj_id, header + content)

    def _expect_mismatched_object_error(self, obj_id):
        obj_id_hex = hash_to_hex(obj_id)
        obj_path = self._obj_relative_path(obj_id)

        # For Git < 2.21:
        self._expected_fsck_errors.add(
            f"error: sha1 mismatch for ./{obj_path} (expected {obj_id_hex})"
        )
        # For Git >= 2.21:
        self._expected_fsck_errors.add(
            f"error: hash mismatch for ./{obj_path} (expected {obj_id_hex})"
        )

        self._expected_fsck_errors.add(
            f"error: {obj_id_hex}: object corrupt or missing: ./{obj_path}"
        )
        self._expected_fsck_errors.add(f"missing blob {obj_id_hex}")
