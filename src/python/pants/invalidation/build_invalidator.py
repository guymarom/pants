# coding=utf-8
# Copyright 2014 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import absolute_import, division, print_function, unicode_literals

import errno
import hashlib
import os
from abc import abstractmethod
from builtins import object
from collections import namedtuple

from pants.base.hash_utils import hash_all
from pants.build_graph.target import Target
from pants.fs.fs import safe_filename
from pants.subsystem.subsystem import Subsystem
from pants.util.dirutil import safe_mkdir
from pants.util.meta import AbstractClass


# Bump this to invalidate all existing keys in artifact caches across all pants deployments in the
# world. Do this if you've made a change that invalidates existing artifacts, e.g.,  fixed a bug
# that caused bad artifacts to be cached.
GLOBAL_CACHE_KEY_GEN_VERSION = '7'


class CacheKey(namedtuple('CacheKey', ['id', 'hash'])):
  """A CacheKey represents some version of a set of targets.

  - id identifies the set of targets.
  - hash is a fingerprint of all invalidating inputs to the build step, i.e., it uniquely
    determines a given version of the artifacts created when building the target set.
  """

  _UNCACHEABLE_HASH = '__UNCACHEABLE_HASH__'

  @classmethod
  def uncacheable(cls, id):
    """Creates a cache key this is never `cacheable`."""
    return cls(id=id, hash=cls._UNCACHEABLE_HASH)

  @classmethod
  def combine_cache_keys(cls, cache_keys):
    """Returns a cache key for a list of target sets that already have cache keys.

    This operation is 'idempotent' in the sense that if cache_keys contains a single key
    then that key is returned.

    Note that this operation is commutative but not associative.  We use the term 'combine' rather
    than 'merge' or 'union' to remind the user of this. Associativity is not a necessary property,
    in practice.
    """
    if len(cache_keys) == 1:
      return cache_keys[0]
    else:
      combined_id = Target.maybe_readable_combine_ids(cache_key.id for cache_key in cache_keys)
      combined_hash = hash_all(sorted(cache_key.hash for cache_key in cache_keys))
      return cls(combined_id, combined_hash)

  @property
  def cacheable(self):
    """Indicates whether artifacts associated with this cache key should be cached.

    :return: `True` if this cache key represents a cacheable set of target artifacts.
    :rtype: bool
    """
    return self.hash != self._UNCACHEABLE_HASH


class CacheKeyGeneratorInterface(AbstractClass):
  """Generates cache keys for versions of target sets."""

  @abstractmethod
  def key_for_target(self, target, transitive=False, fingerprint_strategy=None):
    """Get a key representing the given target and its sources.

    A key for a set of targets can be created by calling CacheKey.combine_cache_keys()
    on the target's individual cache keys.

    :target: The target to create a CacheKey for.
    :transitive: Whether or not to include a fingerprint of all of :target:'s dependencies.
    :fingerprint_strategy: A FingerprintStrategy instance, which can do per task, finer grained
      fingerprinting of a given Target.
    """


class CacheKeyGenerator(CacheKeyGeneratorInterface):
  def __init__(self, *base_fingerprint_inputs):
    """
    :base_fingerprint_inputs: Information to be included in the fingerprint for all cache keys
      generated by this CacheKeyGenerator.
    """
    hasher = hashlib.sha1()
    hasher.update(GLOBAL_CACHE_KEY_GEN_VERSION.encode('utf-8'))
    for base_fingerprint_input in base_fingerprint_inputs:
      hasher.update(base_fingerprint_input)
    self._base_hasher = hasher

  def key_for_target(self, target, transitive=False, fingerprint_strategy=None):
    hasher = self._base_hasher.copy()
    key_suffix = hasher.hexdigest()[:12]
    if transitive:
      target_key = target.transitive_invalidation_hash(fingerprint_strategy)
    else:
      target_key = target.invalidation_hash(fingerprint_strategy)
    if target_key is not None:
      full_key = '{target_key}_{key_suffix}'.format(target_key=target_key, key_suffix=key_suffix)
      return CacheKey(target.id, full_key)
    else:
      return None


class UncacheableCacheKeyGenerator(CacheKeyGeneratorInterface):
  """A cache key generator that always returns uncacheable cache keys."""

  def key_for_target(self, target, transitive=False, fingerprint_strategy=None):
    return CacheKey.uncacheable(target.id)


# A persistent map from target set to cache key, which is a fingerprint of all
# the inputs to the current version of that target set. That cache key can then be used
# to look up build artifacts in an artifact cache.
class BuildInvalidator(object):
  """Invalidates build targets based on the SHA1 hash of source files and other inputs."""

  class Factory(Subsystem):
    options_scope = 'build-invalidator'

    @classmethod
    def create(cls, build_task=None):
      """Creates a build invalidator optionally scoped to a task.

      :param str build_task: An optional task name to scope the build invalidator to. If not
                             supplied the build invalidator will act globally across all build
                             tasks.
      """
      root = os.path.join(cls.global_instance().get_options().pants_workdir, 'build_invalidator')
      return BuildInvalidator(root, scope=build_task)

  @staticmethod
  def cacheable(cache_key):
    """Indicates whether artifacts associated with the given `cache_key` should be cached.

    :return: `True` if the `cache_key` represents a cacheable set of target artifacts.
    :rtype: bool
    """
    return cache_key.cacheable

  def __init__(self, root, scope=None):
    """Create a build invalidator using the given root fingerprint database directory.

    :param str root: The root directory to use for storing build invalidation fingerprints.
    :param str scope: The scope of this invalidator; if `None` then this invalidator will be global.
    """
    root = os.path.join(root, GLOBAL_CACHE_KEY_GEN_VERSION)
    if scope:
      root = os.path.join(root, scope)
    self._root = root
    safe_mkdir(self._root)

  def previous_key(self, cache_key):
    """If there was a previous successful build for the given key, return the previous key.

    :param cache_key: A CacheKey object (as returned by CacheKeyGenerator.key_for().
    :returns: The previous cache_key, or None if there was not a previous build.
    """
    if not self.cacheable(cache_key):
      # We should never successfully cache an uncacheable CacheKey.
      return None

    previous_hash = self._read_sha(cache_key)
    if not previous_hash:
      return None
    return CacheKey(cache_key.id, previous_hash)

  def needs_update(self, cache_key):
    """Check if the given cached item is invalid.

    :param cache_key: A CacheKey object (as returned by CacheKeyGenerator.key_for().
    :returns: True if the cached version of the item is out of date.
    """
    if not self.cacheable(cache_key):
      # An uncacheable CacheKey is always out of date.
      return True

    return self._read_sha(cache_key) != cache_key.hash

  def update(self, cache_key):
    """Makes cache_key the valid version of the corresponding target set.

    :param cache_key: A CacheKey object (typically returned by CacheKeyGenerator.key_for()).
    """
    if self.cacheable(cache_key):
      self._write_sha(cache_key)

  def force_invalidate_all(self):
    """Force-invalidates all cached items."""
    safe_mkdir(self._root, clean=True)

  def force_invalidate(self, cache_key):
    """Force-invalidate the cached item."""
    try:
      if self.cacheable(cache_key):
        os.unlink(self._sha_file(cache_key))
    except OSError as e:
      if e.errno != errno.ENOENT:
        raise

  def _sha_file(self, cache_key):
    return self._sha_file_by_id(cache_key.id)

  def _sha_file_by_id(self, id):
    return os.path.join(self._root, safe_filename(id, extension='.hash'))

  def _write_sha(self, cache_key):
    with open(self._sha_file(cache_key), 'w') as fd:
      fd.write(cache_key.hash)

  def _read_sha(self, cache_key):
    return self._read_sha_by_id(cache_key.id)

  def _read_sha_by_id(self, id):
    try:
      with open(self._sha_file_by_id(id), 'r') as fd:
        return fd.read().strip()
    except IOError as e:
      if e.errno != errno.ENOENT:
        raise
      return None  # File doesn't exist.
