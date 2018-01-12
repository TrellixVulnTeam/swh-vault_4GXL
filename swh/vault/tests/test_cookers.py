# Copyright (C) 2017  The Software Heritage developers
# See the AUTHORS file at the top-level directory of this distribution
# License: GNU General Public License version 3, or any later version
# See top-level LICENSE file for more information

import contextlib
import datetime
import gzip
import io
import os
import pathlib
import subprocess
import tarfile
import tempfile
import unittest
import unittest.mock

import dulwich.fastexport
import dulwich.index
import dulwich.objects
import dulwich.porcelain
import dulwich.repo

from swh.core.tests.db_testing import DbTestFixture
from swh.loader.git.loader import GitLoader
from swh.model import hashutil
from swh.model.from_disk import Directory
from swh.storage.tests.storage_testing import StorageTestFixture
from swh.vault.cookers import DirectoryCooker, RevisionGitfastCooker
from swh.vault.cookers.base import SKIPPED_MESSAGE, HIDDEN_MESSAGE
from swh.vault.tests.vault_testing import VaultTestFixture, hash_content


class TestRepo:
    """A tiny context manager for a test git repository, with some utility
    functions to perform basic git stuff.
    """
    def __enter__(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix='tmp-vault-repo-')
        self.repo_dir = self.tmp_dir.__enter__()
        self.repo = dulwich.repo.Repo.init(self.repo_dir)
        self.author = '"Test Author" <test@softwareheritage.org>'.encode()
        return pathlib.Path(self.repo_dir)

    def __exit__(self, exc, value, tb):
        self.tmp_dir.__exit__(exc, value, tb)

    def checkout(self, rev_sha):
        rev = self.repo[rev_sha]
        dulwich.index.build_index_from_tree(self.repo_dir,
                                            self.repo.index_path(),
                                            self.repo.object_store,
                                            rev.tree)

    def git_shell(self, *cmd, stdout=subprocess.DEVNULL, **kwargs):
        subprocess.check_call(('git', '-C', self.repo_dir) + cmd,
                              stdout=stdout, **kwargs)

    def commit(self, message='Commit test\n', ref=b'HEAD'):
        self.git_shell('add', '.')
        message = message.encode() + b'\n'
        return self.repo.do_commit(message=message, committer=self.author,
                                   ref=ref)

    def merge(self, parent_sha_list, message='Merge branches.'):
        self.git_shell('merge', '--allow-unrelated-histories',
                       '-m', message, *[p.decode() for p in parent_sha_list])
        return self.repo.refs[b'HEAD']

    def print_debug_graph(self, reflog=False):
        args = ['log', '--all', '--graph', '--decorate']
        if reflog:
            args.append('--reflog')
        self.git_shell(*args, stdout=None)


class BaseTestCookers(VaultTestFixture, StorageTestFixture, DbTestFixture):
    """Base class of cookers unit tests"""
    def setUp(self):
        super().setUp()
        self.loader = GitLoader()
        self.loader.storage = self.storage

    def load(self, repo_path):
        """Load a repository in the test storage"""
        self.loader.load('fake_origin', repo_path, datetime.datetime.now())

    @contextlib.contextmanager
    def cook_extract_directory(self, obj_id):
        """Context manager that cooks a directory and extract it."""
        cooker = DirectoryCooker('directory', obj_id)
        cooker.storage = self.storage
        cooker.backend = unittest.mock.MagicMock()
        cooker.fileobj = io.BytesIO()
        assert cooker.check_exists()
        cooker.prepare_bundle()
        cooker.fileobj.seek(0)
        with tempfile.TemporaryDirectory(prefix='tmp-vault-extract-') as td:
            with tarfile.open(fileobj=cooker.fileobj, mode='r') as tar:
                tar.extractall(td)
            yield pathlib.Path(td) / hashutil.hash_to_hex(obj_id)

    @contextlib.contextmanager
    def cook_extract_revision_gitfast(self, obj_id):
        """Context manager that cooks a revision and extract it."""
        cooker = RevisionGitfastCooker('revision_gitfast', obj_id)
        cooker.storage = self.storage
        cooker.backend = unittest.mock.MagicMock()
        cooker.fileobj = io.BytesIO()
        assert cooker.check_exists()
        cooker.prepare_bundle()
        cooker.fileobj.seek(0)
        fastexport_stream = gzip.GzipFile(fileobj=cooker.fileobj)
        test_repo = TestRepo()
        with test_repo as p:
            processor = dulwich.fastexport.GitImportProcessor(test_repo.repo)
            processor.import_stream(fastexport_stream)
            yield test_repo, p


TEST_CONTENT = ("   test content\n"
                "and unicode \N{BLACK HEART SUIT}\n"
                " and trailing spaces   ")
TEST_EXECUTABLE = b'\x42\x40\x00\x00\x05'


class TestDirectoryCooker(BaseTestCookers, unittest.TestCase):
    def test_directory_simple(self):
        repo = TestRepo()
        with repo as rp:
            (rp / 'file').write_text(TEST_CONTENT)
            (rp / 'executable').write_bytes(TEST_EXECUTABLE)
            (rp / 'executable').chmod(0o755)
            (rp / 'link').symlink_to('file')
            (rp / 'dir1/dir2').mkdir(parents=True)
            (rp / 'dir1/dir2/file').write_text(TEST_CONTENT)
            c = repo.commit()
            self.load(str(rp))

            obj_id_hex = repo.repo[c].tree.decode()
            obj_id = hashutil.hash_to_bytes(obj_id_hex)

        with self.cook_extract_directory(obj_id) as p:
            self.assertEqual((p / 'file').stat().st_mode, 0o100644)
            self.assertEqual((p / 'file').read_text(), TEST_CONTENT)
            self.assertEqual((p / 'executable').stat().st_mode, 0o100755)
            self.assertEqual((p / 'executable').read_bytes(), TEST_EXECUTABLE)
            self.assertTrue((p / 'link').is_symlink)
            self.assertEqual(os.readlink(str(p / 'link')), 'file')
            self.assertEqual((p / 'dir1/dir2/file').stat().st_mode, 0o100644)
            self.assertEqual((p / 'dir1/dir2/file').read_text(), TEST_CONTENT)

            directory = Directory.from_disk(path=bytes(p))
            self.assertEqual(obj_id_hex, hashutil.hash_to_hex(directory.hash))

    def test_directory_filtered_objects(self):
        repo = TestRepo()
        with repo as rp:
            file_1, id_1 = hash_content(b'test1')
            file_2, id_2 = hash_content(b'test2')
            file_3, id_3 = hash_content(b'test3')

            (rp / 'file').write_bytes(file_1)
            (rp / 'hidden_file').write_bytes(file_2)
            (rp / 'absent_file').write_bytes(file_3)

            c = repo.commit()
            self.load(str(rp))

            obj_id_hex = repo.repo[c].tree.decode()
            obj_id = hashutil.hash_to_bytes(obj_id_hex)

        # FIXME: storage.content_update() should be changed to allow things
        # like that
        cur = self.storage.db._cursor(None)
        cur.execute("""update content set status = 'visible'
                       where sha1 = %s""", (id_1,))
        cur.execute("""update content set status = 'hidden'
                       where sha1 = %s""", (id_2,))
        cur.execute("""update content set status = 'absent'
                       where sha1 = %s""", (id_3,))
        cur.close()

        with self.cook_extract_directory(obj_id) as p:
            self.assertEqual((p / 'file').read_bytes(), b'test1')
            self.assertEqual((p / 'hidden_file').read_bytes(), HIDDEN_MESSAGE)
            self.assertEqual((p / 'absent_file').read_bytes(), SKIPPED_MESSAGE)

    def test_directory_bogus_perms(self):
        # Some early git repositories have 664/775 permissions... let's check
        # if all the weird modes are properly normalized in the directory
        # cooker.
        repo = TestRepo()
        with repo as rp:
            (rp / 'file').write_text(TEST_CONTENT)
            (rp / 'file').chmod(0o664)
            (rp / 'executable').write_bytes(TEST_EXECUTABLE)
            (rp / 'executable').chmod(0o775)
            (rp / 'wat').write_text(TEST_CONTENT)
            (rp / 'wat').chmod(0o604)
            c = repo.commit()
            self.load(str(rp))

            obj_id_hex = repo.repo[c].tree.decode()
            obj_id = hashutil.hash_to_bytes(obj_id_hex)

        with self.cook_extract_directory(obj_id) as p:
            self.assertEqual((p / 'file').stat().st_mode, 0o100644)
            self.assertEqual((p / 'executable').stat().st_mode, 0o100755)
            self.assertEqual((p / 'wat').stat().st_mode, 0o100644)


class TestRevisionGitfastCooker(BaseTestCookers, unittest.TestCase):
    def test_revision_simple(self):
        #
        #     1--2--3--4--5--6--7
        #
        repo = TestRepo()
        with repo as rp:
            (rp / 'file1').write_text(TEST_CONTENT)
            repo.commit('add file1')
            (rp / 'file2').write_text(TEST_CONTENT)
            repo.commit('add file2')
            (rp / 'dir1/dir2').mkdir(parents=True)
            (rp / 'dir1/dir2/file').write_text(TEST_CONTENT)
            repo.commit('add dir1/dir2/file')
            (rp / 'bin1').write_bytes(TEST_EXECUTABLE)
            (rp / 'bin1').chmod(0o755)
            repo.commit('add bin1')
            (rp / 'link1').symlink_to('file1')
            repo.commit('link link1 to file1')
            (rp / 'file2').unlink()
            repo.commit('remove file2')
            (rp / 'bin1').rename(rp / 'bin')
            repo.commit('rename bin1 to bin')
            self.load(str(rp))
            obj_id_hex = repo.repo.refs[b'HEAD'].decode()
            obj_id = hashutil.hash_to_bytes(obj_id_hex)

        with self.cook_extract_revision_gitfast(obj_id) as (ert, p):
            ert.checkout(b'HEAD')
            self.assertEqual((p / 'file1').stat().st_mode, 0o100644)
            self.assertEqual((p / 'file1').read_text(), TEST_CONTENT)
            self.assertTrue((p / 'link1').is_symlink)
            self.assertEqual(os.readlink(str(p / 'link1')), 'file1')
            self.assertEqual((p / 'bin').stat().st_mode, 0o100755)
            self.assertEqual((p / 'bin').read_bytes(), TEST_EXECUTABLE)
            self.assertEqual((p / 'dir1/dir2/file').read_text(), TEST_CONTENT)
            self.assertEqual((p / 'dir1/dir2/file').stat().st_mode, 0o100644)
            self.assertEqual(ert.repo.refs[b'HEAD'].decode(), obj_id_hex)

    def test_revision_two_roots(self):
        #
        #    1----3---4
        #        /
        #   2----
        #
        repo = TestRepo()
        with repo as rp:
            (rp / 'file1').write_text(TEST_CONTENT)
            c1 = repo.commit('Add file1')
            del repo.repo.refs[b'refs/heads/master']  # git update-ref -d HEAD
            (rp / 'file2').write_text(TEST_CONTENT)
            repo.commit('Add file2')
            repo.merge([c1])
            (rp / 'file3').write_text(TEST_CONTENT)
            repo.commit('add file3')
            obj_id_hex = repo.repo.refs[b'HEAD'].decode()
            obj_id = hashutil.hash_to_bytes(obj_id_hex)
            self.load(str(rp))

        with self.cook_extract_revision_gitfast(obj_id) as (ert, p):
            self.assertEqual(ert.repo.refs[b'HEAD'].decode(), obj_id_hex)

    def test_revision_two_double_fork_merge(self):
        #
        #     2---4---6
        #    /   /   /
        #   1---3---5
        #
        repo = TestRepo()
        with repo as rp:
            (rp / 'file1').write_text(TEST_CONTENT)
            c1 = repo.commit('Add file1')
            repo.repo.refs[b'refs/heads/c1'] = c1

            (rp / 'file2').write_text(TEST_CONTENT)
            repo.commit('Add file2')

            (rp / 'file3').write_text(TEST_CONTENT)
            c3 = repo.commit('Add file3', ref=b'refs/heads/c1')
            repo.repo.refs[b'refs/heads/c3'] = c3

            repo.merge([c3])

            (rp / 'file5').write_text(TEST_CONTENT)
            c5 = repo.commit('Add file3', ref=b'refs/heads/c3')

            repo.merge([c5])

            obj_id_hex = repo.repo.refs[b'HEAD'].decode()
            obj_id = hashutil.hash_to_bytes(obj_id_hex)
            self.load(str(rp))

        with self.cook_extract_revision_gitfast(obj_id) as (ert, p):
            self.assertEqual(ert.repo.refs[b'HEAD'].decode(), obj_id_hex)

    def test_revision_triple_merge(self):
        #
        #       .---.---5
        #      /   /   /
        #     2   3   4
        #    /   /   /
        #   1---.---.
        #
        repo = TestRepo()
        with repo as rp:
            (rp / 'file1').write_text(TEST_CONTENT)
            c1 = repo.commit('Commit 1')
            repo.repo.refs[b'refs/heads/b1'] = c1
            repo.repo.refs[b'refs/heads/b2'] = c1

            repo.commit('Commit 2')
            c3 = repo.commit('Commit 3', ref=b'refs/heads/b1')
            c4 = repo.commit('Commit 4', ref=b'refs/heads/b2')
            repo.merge([c3, c4])

            obj_id_hex = repo.repo.refs[b'HEAD'].decode()
            obj_id = hashutil.hash_to_bytes(obj_id_hex)
            self.load(str(rp))

        with self.cook_extract_revision_gitfast(obj_id) as (ert, p):
            self.assertEqual(ert.repo.refs[b'HEAD'].decode(), obj_id_hex)

    def test_revision_filtered_objects(self):
        repo = TestRepo()
        with repo as rp:
            file_1, id_1 = hash_content(b'test1')
            file_2, id_2 = hash_content(b'test2')
            file_3, id_3 = hash_content(b'test3')

            (rp / 'file').write_bytes(file_1)
            (rp / 'hidden_file').write_bytes(file_2)
            (rp / 'absent_file').write_bytes(file_3)

            repo.commit()
            obj_id_hex = repo.repo.refs[b'HEAD'].decode()
            obj_id = hashutil.hash_to_bytes(obj_id_hex)
            self.load(str(rp))

        # FIXME: storage.content_update() should be changed to allow things
        # like that
        cur = self.storage.db._cursor(None)
        cur.execute("""update content set status = 'visible'
                       where sha1 = %s""", (id_1,))
        cur.execute("""update content set status = 'hidden'
                       where sha1 = %s""", (id_2,))
        cur.execute("""update content set status = 'absent'
                       where sha1 = %s""", (id_3,))
        cur.close()

        with self.cook_extract_revision_gitfast(obj_id) as (ert, p):
            ert.checkout(b'HEAD')
            self.assertEqual((p / 'file').read_bytes(), b'test1')
            self.assertEqual((p / 'hidden_file').read_bytes(), HIDDEN_MESSAGE)
            self.assertEqual((p / 'absent_file').read_bytes(), SKIPPED_MESSAGE)

    def test_revision_bogus_perms(self):
        # Some early git repositories have 664/775 permissions... let's check
        # if all the weird modes are properly normalized in the revision
        # cooker.
        repo = TestRepo()
        with repo as rp:
            (rp / 'file').write_text(TEST_CONTENT)
            (rp / 'file').chmod(0o664)
            (rp / 'executable').write_bytes(TEST_EXECUTABLE)
            (rp / 'executable').chmod(0o775)
            (rp / 'wat').write_text(TEST_CONTENT)
            (rp / 'wat').chmod(0o604)
            repo.commit('initial commit')
            self.load(str(rp))
            obj_id_hex = repo.repo.refs[b'HEAD'].decode()
            obj_id = hashutil.hash_to_bytes(obj_id_hex)

        with self.cook_extract_revision_gitfast(obj_id) as (ert, p):
            ert.checkout(b'HEAD')
            self.assertEqual((p / 'file').stat().st_mode, 0o100644)
            self.assertEqual((p / 'executable').stat().st_mode, 0o100755)
            self.assertEqual((p / 'wat').stat().st_mode, 0o100644)

    def test_revision_null_fields(self):
        # Our schema doesn't enforce a lot of non-null revision fields. We need
        # to check these cases don't break the cooker.
        repo = TestRepo()
        with repo as rp:
            (rp / 'file').write_text(TEST_CONTENT)
            c = repo.commit('initial commit')
            self.load(str(rp))
            repo.repo.refs[b'HEAD'].decode()
            dir_id_hex = repo.repo[c].tree.decode()
            dir_id = hashutil.hash_to_bytes(dir_id_hex)

        test_id = b'56789012345678901234'
        test_revision = {
            'id': test_id,
            'message': None,
            'author': {'name': None, 'email': None, 'fullname': ''},
            'date': None,
            'committer': {'name': None, 'email': None, 'fullname': ''},
            'committer_date': None,
            'parents': [],
            'type': 'git',
            'directory': dir_id,
            'metadata': {},
            'synthetic': True
        }

        self.storage.revision_add([test_revision])

        with self.cook_extract_revision_gitfast(test_id) as (ert, p):
            ert.checkout(b'HEAD')
            self.assertEqual((p / 'file').stat().st_mode, 0o100644)
