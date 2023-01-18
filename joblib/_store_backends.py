"""Storage providers backends for Memory caching."""

import collections
import datetime
import io
import json
import operator
import os
import os.path
import re
import shutil
import threading
import warnings
from abc import ABCMeta, abstractmethod
from typing import Any, Callable, List, Mapping, Optional, Sequence, Union

from _typeshed import StrPath

from . import numpy_pickle
from .backports import concurrency_safe_rename
from .disk import memstr_to_bytes, mkdirp, rm_subdirs

CacheItemInfo = collections.namedtuple('CacheItemInfo',
                                       'path size last_access')


def concurrency_safe_write(object_to_write: Any, filename:StrPath, write_func:Callable):
    """Writes an object into a unique file in a concurrency-safe way."""
    thread_id = id(threading.current_thread())
    temporary_filename = '{}.thread-{}-pid-{}'.format(
        filename, thread_id, os.getpid())
    write_func(object_to_write, temporary_filename)

    return temporary_filename

ObjectToCache = Any
ItemPath = List[str]
"""
Path is a tuple of the function name and the hashed function arguments
['__main__-c%3A-Users-Ben-Development-datascience-%3Cipython-input-4e2c192497a8%3E\\mydataframe', '9c758c5db8d699eff9c7f6916593e29e']
"""

Location = StrPath

class StoreBackendBase(metaclass=ABCMeta):
    """Helper Abstract Base Class which defines all methods that
       a StorageBackend must implement."""

    location : Optional[Location] = None

    @abstractmethod
    def _open_item(self, f:StrPath, mode:Optional[str]) -> io.IOBase:
        """Opens an item on the store and return a file-like object.

        This method is private and only used by the StoreBackendMixin object.

        Parameters
        ----------
        f: a file-like object
            The file-like object where an item is stored and retrieved
        mode: string, optional
            the mode in which the file-like object is opened allowed valued are
            'rb', 'wb'

        Returns
        -------
        a file-like object
        """

    @abstractmethod
    def _item_exists(self, location:Location ) -> bool:
        """Checks if an item location exists in the store.

        This method is private and only used by the StoreBackendMixin object.

        Parameters
        ----------
        location: string
            The location of an item. On a filesystem, this corresponds to the
            absolute path, including the filename, of a file.

        Returns
        -------
        True if the item exists, False otherwise
        """

    @abstractmethod
    def _move_item(self, src:StrPath , dst:StrPath ) -> None:
        """Moves an item from src to dst in the store.

        This method is private and only used by the StoreBackendMixin object.

        Parameters
        ----------
        src: string
            The source location of an item
        dst: string
            The destination location of an item
        """

    @abstractmethod
    def create_location(self, location: Location) -> None:
        """Creates a location on the store.

        Parameters
        ----------
        location: string
            The location in the store. On a filesystem, this corresponds to a
            directory.
        """

    @abstractmethod
    def clear_location(self, location: Location) -> None:
        """Clears a location on the store.

        Parameters
        ----------
        location: string
            The location in the store. On a filesystem, this corresponds to a
            directory or a filename absolute path
        """

    @abstractmethod
    def get_items(self) -> List[CacheItemInfo]:
        """Returns the whole list of items available in the store.

        Returns
        -------
        The list of items identified by their ids (e.g filename in a
        filesystem).
        """

    @abstractmethod
    def configure(self, location:Location, verbose:int=0, backend_options:Mapping[str,Any]=dict()):
        """Configures the store.

        Parameters
        ----------
        location: string
            The base location used by the store. On a filesystem, this
            corresponds to a directory.
        verbose: int
            The level of verbosity of the store
        backend_options: dict
            Contains a dictionary of named parameters used to configure the
            store backend.
        """

    @abstractmethod
    def filename_for_item(self, path: ItemPath) -> StrPath:
        """
            generates the filename (relative to self.location) for the stored item
        """

    @abstractmethod
    def filename_for_metadata(self, path: ItemPath) -> StrPath:
        """
            generates the filename (relative to self.location) for the metadata
        """

    @abstractmethod
    def filename_for_functioncode(self, path: ItemPath) -> StrPath:
        """
            generates the filename (relative to self.location) for the function code
        """

class StoreBackendMixin(object):
    """Class providing all logic for managing the store in a generic way.

    The StoreBackend subclass has to implement 3 methods: create_location,
    clear_location and configure. The StoreBackend also has to provide
    a private _open_item, _item_exists and _move_item methods. The _open_item
    method has to have the same signature as the builtin open and return a
    file-like object.
    """

    # properties of StoreBackendBase
    location : StrPath
    _item_exists: Callable[[StrPath ], bool]
    create_location: Callable
    _open_item: Callable[[StrPath,Optional[str]], io.IOBase]
    clear_location: Callable[[Location], None]
    _move_item: Callable[[StrPath, StrPath], None]
    get_items: Callable[[], List[CacheItemInfo]]

    #later implemented by FileSystemStoreBackend.config
    mmap_mode : Optional[str]
    compress : Optional[Union[int,bool]]
    verbose : int

    def load_item(self, path, verbose=1, msg=None):
        """Load an item from the store given its path as a list of
           strings."""
        full_path = os.path.join(self.location, *path)

        if verbose > 1:
            if verbose < 10:
                print('{0}...'.format(msg))
            else:
                print('{0} from {1}'.format(msg, full_path))

        mmap_mode = (None if not hasattr(self, 'mmap_mode')
                     else self.mmap_mode)

        filename = os.path.join(full_path, self.filename_for_item(path))
        if not self._item_exists(filename):
            raise KeyError("Non-existing item (may have been "
                           "cleared).\nFile %s does not exist" % filename)

        # file-like object cannot be used when mmap_mode is set
        if mmap_mode is None:
            with self._open_item(filename, "rb") as f:
                item = numpy_pickle.load(f)
        else:
            item = numpy_pickle.load(filename, mmap_mode=mmap_mode)
        return item

    def dump_item(self, path:ItemPath, item, verbose=1):
        """Dump an item in the store at the path given as a list of
           strings."""
        try:
            item_path = os.path.join(self.location, *path)
            if not self._item_exists(item_path):
                self.create_location(item_path)
            filename = os.path.join(item_path, self.filename_for_item(path))
            if verbose > 10:
                print('Persisting in %s' % item_path)

            def write_func(to_write, dest_filename):
                with self._open_item(dest_filename, "wb") as f:
                    numpy_pickle.dump(to_write, f,
                                      compress=int(self.compress or 0))

            self._concurrency_safe_write(item, filename, write_func)
        except:  # noqa: E722
            " Race condition in the creation of the directory "

    def clear_item(self, path:ItemPath):
        """Clear the item at the path, given as a list of strings."""
        item_path = os.path.join(self.location, *path)
        if self._item_exists(item_path):
            self.clear_location(item_path)

    def contains_item(self, path:ItemPath):
        """Check if there is an item at the path, given as a list of
           strings"""
        item_path = os.path.join(self.location, *path)
        filename = os.path.join(item_path, self.filename_for_item(path))

        return self._item_exists(filename)

    def get_item_info(self, path:ItemPath):
        """Return information about item."""
        return {'location': os.path.join(self.location,
                                         *path)}

    def get_metadata(self, path:ItemPath):
        """Return actual metadata of an item."""
        try:
            item_path = os.path.join(self.location, *path)
            filename = os.path.join(item_path, self.filename_for_metadata(path))
            with self._open_item(filename, 'rb') as f:
                return json.loads(f.read().decode('utf-8'))
        except:  # noqa: E722
            return {}

    def store_metadata(self, path:ItemPath, metadata):
        """Store metadata of a computation."""
        try:
            item_path = os.path.join(self.location, *path)
            self.create_location(item_path)
            filename = os.path.join(item_path, 'metadata.json')

            def write_func(to_write, dest_filename):
                with self._open_item(dest_filename, "wb") as f:
                    f.write(json.dumps(to_write).encode('utf-8'))

            self._concurrency_safe_write(metadata, filename, write_func)
        except:  # noqa: E722
            pass

    def clear_path(self, path:ItemPath):
        """Clear all items with a common path in the store."""
        func_path = os.path.join(self.location, *path)
        if self._item_exists(func_path):
            self.clear_location(func_path)

    def store_cached_func_code(self, path:ItemPath, func_code:Any=None):
        """Store the code of the cached function."""
        func_path = os.path.join(self.location, *path)
        if not self._item_exists(func_path):
            self.create_location(func_path)

        if func_code is not None:
            filename = os.path.join(func_path, self.filename_for_functioncode(path))
            with self._open_item(filename, 'wb') as f:
                f.write(func_code.encode('utf-8'))

    def get_cached_func_code(self, path:ItemPath) -> str:
        """Store the code of the cached function."""
        path += ['func_code.py', ]
        filename = os.path.join(self.location, *path)
        try:
            with self._open_item(filename, 'rb') as f:
                return f.read().decode('utf-8')
        except:  # noqa: E722
            raise

    def get_cached_func_info(self, path:ItemPath) -> Mapping[str,Any]:
        """Return information related to the cached function if it exists."""
        return {'location': os.path.join(self.location, *path)}

    def clear(self) -> None:
        """Clear the whole store content."""
        self.clear_location(self.location)

    def reduce_store_size(self, bytes_limit:Union[str,int]) -> None:
        """Reduce store size to keep it under the given bytes limit."""
        items_to_delete = self._get_items_to_delete(bytes_limit)

        for item in items_to_delete:
            if self.verbose > 10:
                print('Deleting item {0}'.format(item))
            try:
                self.clear_location(item.path)
            except OSError:
                # Even with ignore_errors=True shutil.rmtree can raise OSError
                # with:
                # [Errno 116] Stale file handle if another process has deleted
                # the folder already.
                pass

    def _get_items_to_delete(self, bytes_limit:Union[str,int]):
        """Get items to delete to keep the store under a size limit."""
        if isinstance(bytes_limit, str):
            bytes_limit = memstr_to_bytes(bytes_limit)

        items = self.get_items()
        size = sum(item.size for item in items)

        to_delete_size = size - bytes_limit
        if to_delete_size < 0:
            return []

        # We want to delete first the cache items that were accessed a
        # long time ago
        items.sort(key=operator.attrgetter('last_access'))

        items_to_delete = []
        size_so_far = 0

        for item in items:
            if size_so_far > to_delete_size:
                break

            items_to_delete.append(item)
            size_so_far += item.size

        return items_to_delete

    def _concurrency_safe_write(self, to_write:ObjectToCache, filename, write_func):
        """Writes an object into a file in a concurrency-safe way."""
        temporary_filename = concurrency_safe_write(to_write,
                                                    filename, write_func)
        self._move_item(temporary_filename, filename)

    def __repr__(self):
        """Printable representation of the store location."""
        return '{class_name}(location="{location}")'.format(
            class_name=self.__class__.__name__, location=self.location)

    def filename_for_item(self, path: ItemPath) -> StrPath:
        """
            generates the filename (relative to self.location) for the stored item
        """

    def filename_for_metadata(self, path: ItemPath) -> StrPath:
        return 'metadata.json'

    def filename_for_functioncode(self, path: ItemPath) -> StrPath:
        return 'func_code.py'


class FileSystemStoreBackend(StoreBackendBase, StoreBackendMixin):
    """A StoreBackend used with local or network file systems."""

    _open_item = staticmethod(open)
    _item_exists = staticmethod(os.path.exists)
    _move_item = staticmethod(concurrency_safe_rename)

    def clear_location(self, location):
        """Delete location on store."""
        if (location == self.location):
            rm_subdirs(location)
        else:
            shutil.rmtree(location, ignore_errors=True)

    def create_location(self, location):
        """Create object location on store"""
        mkdirp(location)

    def get_items(self) -> List[CacheItemInfo]:
        """Returns the whole list of items available in the store."""
        items = []
        if self.location is None: return items
        for dirpath, _, filenames in os.walk(self.location):
            is_cache_hash_dir = re.match('[a-f0-9]{32}',
                                         os.path.basename(dirpath))

            if is_cache_hash_dir:
                output_filename = os.path.join(dirpath, self.filename_for_item(path))
                try:
                    last_access = os.path.getatime(output_filename)
                except OSError:
                    try:
                        last_access = os.path.getatime(dirpath)
                    except OSError:
                        # The directory has already been deleted
                        continue

                last_access = datetime.datetime.fromtimestamp(last_access)
                try:
                    full_filenames = [os.path.join(dirpath, fn)
                                      for fn in filenames]
                    dirsize = sum(os.path.getsize(fn)
                                  for fn in full_filenames)
                except OSError:
                    # Either output_filename or one of the files in
                    # dirpath does not exist any more. We assume this
                    # directory is being cleaned by another process already
                    continue

                items.append(CacheItemInfo(dirpath, dirsize,
                                           last_access))

        return items

    def configure(self, location, verbose=1, backend_options=None):
        """Configure the store backend.

        For this backend, valid store options are 'compress' and 'mmap_mode'
        """
        if backend_options is None:
            backend_options = {}

        # setup location directory
        self.location = location
        if not os.path.exists(self.location):
            mkdirp(self.location)

        # item can be stored compressed for faster I/O
        self.compress = backend_options.get('compress', False)

        # FileSystemStoreBackend can be used with mmap_mode options under
        # certain conditions.
        mmap_mode = backend_options.get('mmap_mode')
        if self.compress and mmap_mode is not None:
            warnings.warn('Compressed items cannot be memmapped in a '
                          'filesystem store. Option will be ignored.',
                          stacklevel=2)

        self.mmap_mode = mmap_mode
        self.verbose = verbose
