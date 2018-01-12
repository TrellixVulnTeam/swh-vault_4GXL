# Copyright (C) 2016-2017  The Software Heritage developers
# See the AUTHORS file at the top-level directory of this distribution
# License: GNU General Public License version 3, or any later version
# See top-level LICENSE file for more information

import tarfile
import tempfile
from pathlib import Path

from swh.model import hashutil

from .base import BaseVaultCooker, DirectoryBuilder


class RevisionFlatCooker(BaseVaultCooker):
    """Cooker to create a directory bundle """
    CACHE_TYPE_KEY = 'revision_flat'

    def check_exists(self):
        return not list(self.storage.revision_missing([self.obj_id]))

    def prepare_bundle(self):
        """Cook the requested revision into a Bundle

        Returns:
            bytes that correspond to the bundle

        """
        directory_builder = DirectoryBuilder(self.storage)
        with tempfile.TemporaryDirectory(suffix='.cook') as root_tmp:
            root = Path(root_tmp)
            for revision in self.storage.revision_log([self.obj_id]):
                revdir = root / hashutil.hash_to_hex(revision['id'])
                revdir.mkdir()
                directory_builder.build_directory(revision['directory'],
                                                  str(revdir).encode())
            tar = tarfile.open(fileobj=self.fileobj, mode='w')
            tar.add(root_tmp, arcname=hashutil.hash_to_hex(self.obj_id))
