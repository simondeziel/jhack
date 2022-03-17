import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import List

from logger import logger


def update_charm(charm: Path,
                 src: List[Path] = ('./src', './lib'),
                 dst: List[Path] = ('src', 'lib'),
                 dry_run: bool = False):
    """
    Force-push into a local .charm file one or more directories.

    E.g. `jhack charm update my_charm.charm --src ./foo --dst bar` will grab
    ./src/* and copy it to [the charm's root]/src/*.

    >>> update_charm('./my_local_charm-amd64.charm',
    ...              ['./src', './lib'],
    ...              ['src', 'lib'])
    """
    src = tuple(map(Path, src))
    dst = tuple(map(Path, dst))

    assert charm.exists() and charm.is_file()
    for dir_ in src:
        assert dir_.exists() and dir_.is_dir()
    assert len(dst) == len(src)

    logger.info(f"updating charm with args:, {charm}, {src, dst}, {dry_run}")

    build_dir = Path(tempfile.mkdtemp())
    prefix_len = len(os.getcwd()) + 1

    try:
        # extract charm to build directory
        with zipfile.ZipFile(charm, 'r') as zip_read:
            zip_read.extractall(build_dir)
            logger.info(
                f'Extracted {len(zip_read.filelist)} files to build folder.'
            )

        # remove src and lib
        for source, destination in zip(src, dst):

            build_dst = build_dir / destination
            # ensure the destination is **gone**
            if dry_run:
                logger.info(f'Would remove {build_dst}...')
                if source.exists():
                    logger.info(f'...and replace it with {source}')
                continue

            # ensure the build_dst is clear
            shutil.rmtree(build_dst, ignore_errors=True)

            if not source.exists():
                continue

            shutil.copytree(source, build_dst)
            if not dry_run:
                logger.info(f'Copy: {source} --> {build_dst}.')

        if dry_run:
            logger.info(
                f'Would unlink {charm} and replace it with {build_dir}.'
            )
            return

        # remove old charm
        os.unlink(charm)
        # replace it by zipping the build dir
        shutil.make_archive(str(charm)[:-4], 'zip', build_dir)

    finally:
        shutil.rmtree(build_dir)