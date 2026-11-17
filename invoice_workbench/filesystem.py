import os
import stat


def is_directory_link(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or (
        os.name == "nt" and info.st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT
    )
