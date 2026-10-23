"""WinFsp callbacks for the PMF mount (optional Windows-only dependency)."""
from __future__ import annotations

import csv
import errno
import os
from pathlib import Path
import re
import subprocess
import logging
import threading
from functools import wraps

from winfspy import (BaseFileSystemOperations, FileSystem, NTStatusError,
                    NTStatusObjectNameNotFound, NTStatusObjectNameCollision,
                    NTStatusNotADirectory, NTStatusAccessDenied,
                    NTStatusMediaWriteProtected, NTStatusDirectoryNotEmpty,
                    NTStatusEndOfFile)
from winfspy.plumbing import ffi
from winfspy.plumbing.security_descriptor import SecurityDescriptor
from winfspy.plumbing.status import NTSTATUS

from pmf_mount_store import MountStore, Node
from pmfview import FILE_ATTR_DIRECTORY, PmfError


def operation(function):
    @wraps(function)
    def run(self, *args, **kwargs):
        with self.lock:
            try:
                return function(self, *args, **kwargs)
            except NTStatusError:
                raise
            except FileNotFoundError as exc:
                raise NTStatusObjectNameNotFound() from exc
            except FileExistsError as exc:
                raise NTStatusObjectNameCollision() from exc
            except NotADirectoryError as exc:
                raise NTStatusNotADirectory() from exc
            except IsADirectoryError as exc:
                raise NTStatusError(NTSTATUS.STATUS_FILE_IS_A_DIRECTORY) from exc
            except PermissionError as exc:
                if exc.errno == errno.EROFS:
                    raise NTStatusMediaWriteProtected() from exc
                raise NTStatusAccessDenied() from exc
            except (OSError, PmfError) as exc:
                if isinstance(exc, OSError) and exc.errno == errno.ENOTEMPTY:
                    raise NTStatusDirectoryNotEmpty() from exc
                if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
                    raise NTStatusError(NTSTATUS.STATUS_DISK_FULL) from exc
                logging.exception('PMF mount %s failed', function.__name__)
                self.store.error = str(exc)
                self.store._save_pending()
                raise NTStatusError(NTSTATUS.STATUS_IO_DEVICE_ERROR) from exc
    return run


class PmfOperations(BaseFileSystemOperations):
    def __init__(self, store: MountStore):
        super().__init__()
        self.store = store
        self.lock = threading.RLock()
        self.closing = False
        # The mount is a file-content view, not an NTFS ACL implementation.
        executable = Path(os.environ['SystemRoot']) / 'System32' / 'whoami.exe'
        identity = subprocess.check_output([str(executable), '/user', '/fo', 'csv', '/nh'],
                                           text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        sid = next(csv.reader(identity.splitlines()))[1]
        if not re.fullmatch(r'S-1-(?:[0-9]+-)+[0-9]+', sid):
            raise PmfError('Could not determine the mounting Windows user SID')
        self.security = SecurityDescriptor.from_string(
            f'O:{sid}G:{sid}D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;{sid})')

    def writable(self):
        if self.closing:
            raise NTStatusAccessDenied()
        self.store.writable()

    @staticmethod
    def info(node: Node) -> dict:
        e = node.entry
        attributes = (e.attr_flags & 0x27) | (0x10 if e.is_dir else 0)
        return {'file_attributes': attributes or 0x80,
                'allocation_size': (e.size + 4095) // 4096 * 4096,
                'file_size': e.size, 'creation_time': e.crtime,
                'last_access_time': e.mtime, 'last_write_time': e.mtime,
                'change_time': e.mtime, 'index_number': e.recno}

    @operation
    def get_volume_info(self):
        return self.store.volume_info()

    @operation
    def set_volume_label(self, volume_label):
        raise NTStatusError(NTSTATUS.STATUS_NOT_SUPPORTED)

    @operation
    def get_security_by_name(self, file_name):
        node = self.store.lookup(file_name)
        return self.info(node)['file_attributes'], self.security.handle, self.security.size

    @operation
    def get_security(self, file_context):
        return self.security

    @operation
    def set_security(self, file_context, security_information, modification_descriptor):
        raise NTStatusError(NTSTATUS.STATUS_NOT_SUPPORTED)

    @operation
    def open(self, file_name, create_options, granted_access):
        if self.closing:
            raise NTStatusAccessDenied()
        node = self.store.lookup(file_name)
        if create_options & 1 and not node.entry.is_dir:
            raise NTStatusNotADirectory()
        if create_options & 0x40 and node.entry.is_dir:
            raise NTStatusError(NTSTATUS.STATUS_FILE_IS_A_DIRECTORY)
        node.handles += 1
        return node

    @operation
    def create(self, file_name, create_options, granted_access, file_attributes,
               security_descriptor, allocation_size):
        self.writable()
        node = self.store.create(file_name, bool(create_options & 1), file_attributes)
        node.handles += 1
        return node

    @operation
    def overwrite(self, file_context, file_attributes, replace_file_attributes, allocation_size):
        self.writable()
        self.store.resize(file_context, 0)
        attributes = file_attributes if replace_file_attributes else file_context.entry.attr_flags | file_attributes
        file_context.entry.attr_flags = attributes & 0x27
        self.store.changed(file_context)

    @operation
    def get_file_info(self, file_context):
        return self.info(file_context)

    @operation
    def set_basic_info(self, file_context, file_attributes, creation_time, last_access_time,
                       last_write_time, change_time, file_info):
        self.writable()
        e = file_context.entry
        if e.recno == 5:
            raise NTStatusAccessDenied()
        if file_attributes != 0xFFFFFFFF:
            e.attr_flags = (file_attributes & 0x27) | (FILE_ATTR_DIRECTORY if e.is_dir else 0)
        if creation_time:
            e.crtime = creation_time
        if last_write_time:
            e.mtime = last_write_time
        self.store.changed(file_context)
        return self.info(file_context)

    @operation
    def set_file_size(self, file_context, new_size, set_allocation_size):
        self.writable()
        self.store.resize(file_context, new_size, allocation=set_allocation_size)

    @operation
    def read(self, file_context, offset, length):
        if offset >= file_context.entry.size:
            raise NTStatusEndOfFile()
        return self.store.read(file_context, offset, min(length, file_context.entry.size - offset))

    @operation
    def write(self, file_context, buffer, offset, write_to_end_of_file, constrained_io):
        self.writable()
        return self.store.write(file_context, bytes(buffer), offset,
                                append=write_to_end_of_file, constrained=constrained_io)

    def ll_flush(self, file_context, file_info):
        # WinFsp passes NULL for a whole-volume flush. winfspy 0.8.4's default
        # trampoline unconditionally calls ffi.from_handle, so handle NULL here.
        if file_context != ffi.NULL:
            return super().ll_flush(file_context, file_info)
        try:
            self.flush(None)
            return NTSTATUS.STATUS_SUCCESS
        except NTStatusError as exc:
            return exc.value
        except Exception:
            logging.exception('Volume flush failed')
            return NTSTATUS.STATUS_IO_DEVICE_ERROR

    @operation
    def flush(self, file_context):
        self.store.flush()

    @operation
    def can_delete(self, file_context, file_name):
        self.writable()
        self.store.can_delete(file_context)

    @operation
    def cleanup(self, file_context, file_name, flags):
        if self.store.read_only:
            return
        if flags & 1:
            self.store.delete(file_context)
        # Windows cannot return a cleanup/close error to the caller. Record failures
        # durably, refuse subsequent writes and make explicit flush/unmount fail.
        try:
            self.store.flush()
        except Exception as exc:
            self.store.error = str(exc)
            self.store._save_pending()
            logging.exception('Close-time commit failed; recovery files retained')

    @operation
    def close(self, file_context):
        file_context.handles = max(0, file_context.handles - 1)

    @operation
    def rename(self, file_context, file_name, new_file_name, replace_if_exists):
        self.writable()
        self.store.rename(file_context, new_file_name, replace_if_exists)
        self.store.flush()

    @operation
    def read_directory(self, file_context, marker):
        entries = []
        if file_context.entry.recno != 5:
            entries = [{'file_name': '.', **self.info(file_context)},
                       {'file_name': '..', **self.info(self.store.nodes[file_context.entry.parent])}]
        entries.extend({'file_name': n.entry.name, **self.info(n)} for n in self.store.children(file_context))
        entries.sort(key=lambda e: e['file_name'].upper())
        if marker is None:
            return entries
        return [e for e in entries if e['file_name'].upper() > marker.upper()]

    @operation
    def get_dir_info_by_name(self, file_context, file_name):
        node = self.store.names.get((file_context.entry.recno, file_name.casefold()))
        if node is None:
            raise NTStatusObjectNameNotFound()
        return {'file_name': node.entry.name, **self.info(node)}

    def prepare_unmount(self):
        with self.lock:
            self.closing = True
            if any(n.handles for n in self.store.nodes.values() if not n.entry.is_dir):
                self.closing = False
                raise PmfError('Files are still open. Close applications using the mount and retry.')
            try:
                self.store.flush()
            except Exception:
                self.closing = False
                raise


def create_filesystem(store: MountStore, drive: str):
    operations = PmfOperations(store)
    return FileSystem(drive, operations, sector_size=512, sectors_per_allocation_unit=8,
                      max_component_length=255, file_info_timeout=0,
                      case_sensitive_search=0, case_preserved_names=1, unicode_on_disk=1,
                      persistent_acls=0, read_only_volume=int(store.read_only),
                      post_cleanup_when_modified_only=1, flush_and_purge_on_cleanup=1,
                      um_file_context_is_user_context2=1, file_system_name='PMFViewer')
