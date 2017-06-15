#!/usr/bin/env python3

from __future__ import print_function

import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Iterable

import click
import structlog
from boltons import fileutils
from eodatasets import verify

from datacube.index._api import Index
from datacube.model import Dataset
from datacube.ui import click as ui
from digitalearthau import paths as path_utils, collections

_LOG = structlog.get_logger()


class CleanConsoleRenderer(structlog.dev.ConsoleRenderer):
    def __init__(self, pad_event=25):
        super().__init__(pad_event)
        # Dim debug messages
        self._level_to_color['debug'] = structlog.dev.DIM


@click.command()
@ui.global_cli_options
@click.option('--dry-run', is_flag=True, default=False)
@click.option('--destination', '-d',
              required=True,
              type=click.Path(exists=True, writable=True))
@click.argument('paths',
                type=click.Path(exists=True, readable=True),
                nargs=-1)
@ui.pass_index('move')
def main(index, dry_run, paths, destination):
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M.%S"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            # Coloured output if to terminal.
            CleanConsoleRenderer() if sys.stdout.isatty() else structlog.processors.JSONRenderer(),
        ],
        context_class=dict,
        cache_logger_on_first_use=True,
    )
    collections.init_nci_collections(None)

    if not path_utils.is_base_directory(destination):
        raise click.BadArgumentUsage(
            'Not a known DEA base directory; {}\nExpected one of:\n\t{}'.format(
                destination,
                '\n\t'.join(path_utils.BASE_DIRECTORIES))
        )

    # TODO: @ui.executor_cli_options
    move_all(
        index,
        (
            # We want to iterate all datasets in the given input folder, so we find collections that exist in
            # that folder and then iterate through all the collection datasets within that folder. Simple :)
            path.absolute()
            for input_path in map(Path, paths)
            for collection in collections.get_collections_in_path(input_path)
            for path in collection.iter_fs_paths_within(input_path)
        ),
        Path(destination),
        dry_run=dry_run,
    )


def move_all(index: Index, paths: Iterable[Path], destination_base_path: Path, dry_run=False):
    for path in paths:
        task = FileMoveTask.evaluate_and_create(index, path, dest_base_path=destination_base_path)
        if not task:
            continue

        task.run(index, dry_run=dry_run)


class FileMoveTask:
    def __init__(self, source_path: Path, dest_path: Path, source_metadata_path: Path, dataset: Dataset):
        self.source_path = source_path
        self.source_metadata_path = source_metadata_path
        self.dest_path = dest_path
        self.dataset = dataset

    @property
    def source_uri(self):
        return self.source_path.as_uri()

    @property
    def log(self):
        return _LOG.bind(source_path=self.source_path)

    @classmethod
    def evaluate_and_create(cls, index: Index, path: Path, dest_base_path: Path):
        """
        Create a move task if this path is movable.
        """
        path = path.absolute()
        log = _LOG.bind(path=path)

        metadata_path = path_utils.get_metadata_path(path)
        log.debug("found.metadata_path", metadata_path=metadata_path)

        dataset_path, dest_path = cls._source_dest_paths(log, metadata_path, dest_base_path)
        if dest_path.exists():
            log.info("skip.exists", dest_path=dest_path)
            return None

        dataset_id = path_utils.get_path_dataset_id(metadata_path)
        log = log.bind(dataset_id=dataset_id)
        log.debug("found.dataset_id")

        dataset = index.datasets.get(dataset_id)
        log.debug('found.is_indexed', is_indexed=dataset is not None)
        # If it's not indexed in the cube yet, skip it. It's probably a new arrival.
        if not dataset:
            log.warn("skip.not_indexed")
            return None

        return FileMoveTask(
            source_path=dataset_path,
            dest_path=dest_path,
            source_metadata_path=metadata_path,
            dataset=dataset
        )

    def run(self, index, dry_run=True):
        """
        :type index: datacube.index._api.Index
        :type self: FileMoveTask
        :type dry_run: bool
        """
        dest_uri = self._do_copy(dry_run=dry_run)
        if not dest_uri:
            self.log.debug("index.skip")
            return

        # Record destination location in index
        if not dry_run:
            index.datasets.add_location(self.dataset.id, uri=dest_uri)
        self.log.info('index.dest.added', uri=dest_uri)

        # Archive source file in index (for deletion soon)
        if not dry_run:
            index.datasets.archive_location(self.dataset.id, self.source_uri)

        self.log.info('index.source.archived', uri=self.source_uri)

    @staticmethod
    def _source_dest_paths(log, source_metadata_path, destination_base_path):
        dataset_path, all_files = path_utils.get_dataset_paths(source_metadata_path)
        _, file_offset = path_utils.split_path_from_base(dataset_path)
        new_dataset_location = destination_base_path.joinpath(file_offset)

        # We currently assume all files are contained in the dataset directory/path:
        # we write the single dataset path atomically.
        if not all(str(f).startswith(str(dataset_path)) for f in all_files):
            raise NotImplementedError("Some dataset files are not contained in the dataset path. "
                                      "Situation not yet implemented. %s" % dataset_path)

        return dataset_path, new_dataset_location

    def _do_copy(self, dry_run=True):
        log = self.log
        dest_path = self.dest_path
        dataset_path = self.source_path

        successful_checksum = _verify_checksum(self.log, self.source_metadata_path,
                                               dry_run=dry_run)
        self.log.info("checksum.complete", passes_checksum=successful_checksum)
        if not successful_checksum:
            raise RuntimeError("Checksum failure on " + str(self.source_metadata_path))

        if dataset_path.is_dir():
            log.debug("copy.mkdir", dest=dest_path.parent)
            fileutils.mkdir_p(str(dest_path.parent))

            # We don't want to risk partially-copied packaged left on disk, so we copy to a tmp dir in same
            # folder and then atomically rename into place.
            tmp_dir = Path(tempfile.mkdtemp(prefix='.dea-mv-', dir=str(dest_path.parent)))
            try:
                tmp_package = tmp_dir.joinpath(dataset_path.name)
                log.info("copy.put", src=dataset_path, tmp_dest=tmp_package)
                if not dry_run:
                    shutil.copytree(str(dataset_path), str(tmp_package))
                    log.debug("copy.put.done")
                    os.rename(str(tmp_package), str(dest_path))
                    log.debug("copy.rename.done")
            finally:
                log.debug("tmp_dir.rm", tmp_dir=tmp_dir)
                shutil.rmtree(str(tmp_dir), ignore_errors=True)
        else:
            # .nc files and sibling files
            raise NotImplementedError("TODO: dataset files not yet supported")

        return dest_path.as_uri()


def _verify_checksum(log, metadata_path, dry_run=True):
    dataset_path, all_files = path_utils.get_dataset_paths(metadata_path)
    checksum_file = _expected_checksum_path(dataset_path)
    if not checksum_file.exists():
        # Ingested data doesn't currently have them, so it's only a warning.
        log.warning("checksum.missing", checksum_file=checksum_file)
        return None

    ch = verify.PackageChecksum()
    ch.read(checksum_file)
    if not dry_run:
        for file, successful in ch.iteratively_verify():
            if successful:
                log.debug("checksum.pass", file=file)
            else:
                log.error("checksum.failure", file=file)
                return False

    log.debug("copy.verify", file_count=len(all_files))
    return True


def _expected_checksum_path(dataset_path):
    """
    :type dataset_path: pathlib.Path
    :rtype: pathlib.Path

    >>> import tempfile
    >>> _expected_checksum_path(Path(tempfile.mkdtemp())).name == 'package.sha1'
    True
    >>> file_ = Path(tempfile.mktemp(suffix='-dataset-file.tif'))
    >>> file_.open('a').close()
    >>> file_chk = _expected_checksum_path(file_)
    >>> str(file_chk).endswith('-dataset-file.tif.sha1')
    True
    >>> file_chk.parent == file_.parent
    True
    """
    if dataset_path.is_dir():
        return dataset_path.joinpath('package.sha1')

    return dataset_path.parent.joinpath(dataset_path.name + '.sha1')


if __name__ == '__main__':
    main()
