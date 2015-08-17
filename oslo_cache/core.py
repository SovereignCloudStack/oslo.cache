# Copyright 2013 Metacloud
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""Caching Layer Implementation.

To use this library:

You must call :func:`configure`.

Inside your application code, decorate the methods that you want the results
to be cached with a memoization decorator created with
:func:`get_memoization_decorator`. This function takes a group name from the
config. Register [`group`] ``caching`` and [`group`] ``cache_time`` options
for the groups that your decorators use so that caching can be configured.

This library's configuration options must be registered in your application's
:class:`oslo_config.cfg.ConfigOpts` instance. Do this by passing the ConfigOpts
instance to :func:`configure`.

The library has special public value for nonexistent or expired keys called
:data:`NO_VALUE`. To use this value you should import it from oslo_cache.core::

    from oslo_cache import core
    NO_VALUE = core.NO_VALUE
"""

import dogpile.cache
from dogpile.cache import api
from dogpile.cache import proxy
from dogpile.cache import util
from oslo_log import log
from oslo_utils import importutils

from oslo_cache._i18n import _
from oslo_cache._i18n import _LE
from oslo_cache import _opts
from oslo_cache import exception


__all__ = [
    'configure',
    'configure_cache_region',
    'create_region',
    'get_memoization_decorator',
    'NO_VALUE',
]

NO_VALUE = api.NO_VALUE
"""Value returned for nonexistent or expired keys."""

_LOG = log.getLogger(__name__)

_BACKENDS = [
    ('oslo_cache.noop', 'oslo_cache.backends.noop', 'NoopCacheBackend'),
    ('oslo_cache.mongo', 'oslo_cache.backends.mongo', 'MongoCacheBackend'),
    ('oslo_cache.memcache_pool', 'oslo_cache.backends.memcache_pool',
     'PooledMemcachedBackend'),
    ('oslo_cache.dict', 'oslo_cache.backends.dictionary', 'DictCacheBackend'),
]


class _DebugProxy(proxy.ProxyBackend):
    """Extra Logging ProxyBackend."""
    # NOTE(morganfainberg): Pass all key/values through repr to ensure we have
    # a clean description of the information.  Without use of repr, it might
    # be possible to run into encode/decode error(s). For logging/debugging
    # purposes encode/decode is irrelevant and we should be looking at the
    # data exactly as it stands.

    def get(self, key):
        value = self.proxied.get(key)
        _LOG.debug('CACHE_GET: Key: "%(key)r" Value: "%(value)r"',
                   {'key': key, 'value': value})
        return value

    def get_multi(self, keys):
        values = self.proxied.get_multi(keys)
        _LOG.debug('CACHE_GET_MULTI: "%(keys)r" Values: "%(values)r"',
                   {'keys': keys, 'values': values})
        return values

    def set(self, key, value):
        _LOG.debug('CACHE_SET: Key: "%(key)r" Value: "%(value)r"',
                   {'key': key, 'value': value})
        return self.proxied.set(key, value)

    def set_multi(self, keys):
        _LOG.debug('CACHE_SET_MULTI: "%r"', keys)
        self.proxied.set_multi(keys)

    def delete(self, key):
        self.proxied.delete(key)
        _LOG.debug('CACHE_DELETE: "%r"', key)

    def delete_multi(self, keys):
        _LOG.debug('CACHE_DELETE_MULTI: "%r"', keys)
        self.proxied.delete_multi(keys)


def _build_cache_config(conf):
    """Build the cache region dictionary configuration.

    :returns: dict
    """
    prefix = conf.cache.config_prefix
    conf_dict = {}
    conf_dict['%s.backend' % prefix] = conf.cache.backend
    conf_dict['%s.expiration_time' % prefix] = conf.cache.expiration_time
    for argument in conf.cache.backend_argument:
        try:
            (argname, argvalue) = argument.split(':', 1)
        except ValueError:
            msg = _LE('Unable to build cache config-key. Expected format '
                      '"<argname>:<value>". Skipping unknown format: %s')
            _LOG.error(msg, argument)
            continue

        arg_key = '.'.join([prefix, 'arguments', argname])
        conf_dict[arg_key] = argvalue

        _LOG.debug('Oslo Cache Config: %s', conf_dict)
    # NOTE(yorik-sar): these arguments will be used for memcache-related
    # backends. Use setdefault for url to support old-style setting through
    # backend_argument=url:127.0.0.1:11211
    conf_dict.setdefault('%s.arguments.url' % prefix,
                         conf.cache.memcache_servers)
    for arg in ('dead_retry', 'socket_timeout', 'pool_maxsize',
                'pool_unused_timeout', 'pool_connection_get_timeout'):
        value = getattr(conf.cache, 'memcache_' + arg)
        conf_dict['%s.arguments.%s' % (prefix, arg)] = value

    return conf_dict


def _sha1_mangle_key(key):
    """Wrapper for dogpile's sha1_mangle_key.

    dogpile's sha1_mangle_key function expects an encoded string, so we
    should take steps to properly handle multiple inputs before passing
    the key through.
    """
    try:
        key = key.encode('utf-8', errors='xmlcharrefreplace')
    except (UnicodeError, AttributeError):
        # NOTE(stevemar): if encoding fails just continue anyway.
        pass
    return util.sha1_mangle_key(key)


def create_region():
    """Create a region.

    This is just dogpile.cache.make_region, but the key generator has a
    different to_str mechanism.

    .. note::

        You must call :func:`configure_cache_region` with this region before
        a memoized method is called.

    :returns: The new region.
    :rtype: :class:`dogpile.cache.CacheRegion`

    """

    return dogpile.cache.make_region(
        function_key_generator=_function_key_generator)


def configure_cache_region(conf, region):
    """Configure a cache region.

    If the cache region is already configured, this function does nothing.
    Otherwise, the region is configured.

    :param conf: config object, must have had :func:`configure` called on it.
    :type conf: oslo_config.cfg.ConfigOpts
    :param region: Cache region to configure (see :func:`create_region`).
    :type region: dogpile.cache.CacheRegion
    :raises oslo_cache.exception.ConfigurationError: If the region parameter is
        not a dogpile.cache.CacheRegion.
    :returns: The region.
    """
    if not isinstance(region, dogpile.cache.CacheRegion):
        raise exception.ConfigurationError(
            _('region not type dogpile.cache.CacheRegion'))

    if not region.is_configured:
        # NOTE(morganfainberg): this is how you tell if a region is configured.
        # There is a request logged with dogpile.cache upstream to make this
        # easier / less ugly.

        config_dict = _build_cache_config(conf)
        region.configure_from_config(config_dict,
                                     '%s.' % conf.cache.config_prefix)

        if conf.cache.debug_cache_backend:
            region.wrap(_DebugProxy)

        # NOTE(morganfainberg): if the backend requests the use of a
        # key_mangler, we should respect that key_mangler function.  If a
        # key_mangler is not defined by the backend, use the sha1_mangle_key
        # mangler provided by dogpile.cache. This ensures we always use a fixed
        # size cache-key.
        if region.key_mangler is None:
            region.key_mangler = _sha1_mangle_key

        for class_path in conf.cache.proxies:
            # NOTE(morganfainberg): if we have any proxy wrappers, we should
            # ensure they are added to the cache region's backend.  Since
            # configure_from_config doesn't handle the wrap argument, we need
            # to manually add the Proxies. For information on how the
            # ProxyBackends work, see the dogpile.cache documents on
            # "changing-backend-behavior"
            cls = importutils.import_class(class_path)
            _LOG.debug("Adding cache-proxy '%s' to backend.", class_path)
            region.wrap(cls)

    return region


def _get_should_cache_fn(conf, group):
    """Build a function that returns a config group's caching status.

    For any given object that has caching capabilities, a boolean config option
    for that object's group should exist and default to ``True``. This
    function will use that value to tell the caching decorator if caching for
    that object is enabled. To properly use this with the decorator, pass this
    function the configuration group and assign the result to a variable.
    Pass the new variable to the caching decorator as the named argument
    ``should_cache_fn``.

    :param conf: config object, must have had :func:`configure` called on it.
    :type conf: oslo_config.cfg.ConfigOpts
    :param group: name of the configuration group to examine
    :type group: string
    :returns: function reference
    """
    def should_cache(value):
        if not conf.cache.enabled:
            return False
        conf_group = getattr(conf, group)
        return getattr(conf_group, 'caching', True)
    return should_cache


def _get_expiration_time_fn(conf, group):
    """Build a function that returns a config group's expiration time status.

    For any given object that has caching capabilities, an int config option
    called ``cache_time`` for that driver's group should exist and typically
    default to ``None``. This function will use that value to tell the caching
    decorator of the TTL override for caching the resulting objects. If the
    value of the config option is ``None`` the default value provided in the
    ``[cache] expiration_time`` option will be used by the decorator. The
    default may be set to something other than ``None`` in cases where the
    caching TTL should not be tied to the global default(s).

    To properly use this with the decorator, pass this function the
    configuration group and assign the result to a variable. Pass the new
    variable to the caching decorator as the named argument
    ``expiration_time``.

    :param group: name of the configuration group to examine
    :type group: string
    :rtype: function reference
    """
    def get_expiration_time():
        conf_group = getattr(conf, group)
        return getattr(conf_group, 'cache_time', None)
    return get_expiration_time


def _key_generate_to_str(s):
    # NOTE(morganfainberg): Since we need to stringify all arguments, attempt
    # to stringify and handle the Unicode error explicitly as needed.
    try:
        return str(s)
    except UnicodeEncodeError:
        return s.encode('utf-8')


def _function_key_generator(namespace, fn, to_str=_key_generate_to_str):
    # NOTE(morganfainberg): This wraps dogpile.cache's default
    # function_key_generator to change the default to_str mechanism.
    return util.function_key_generator(namespace, fn, to_str=to_str)


def get_memoization_decorator(conf, region, group, expiration_group=None):
    """Build a function based on the `cache_on_arguments` decorator.

    The memoization decorator that gets created by this function is a
    :meth:`dogpile.cache.region.CacheRegion.cache_on_arguments` decorator,
    where

    * The ``should_cache_fn`` is set to a function that returns True if both
      the ``[cache] enabled`` option is true and [`group`] ``caching`` is
      True.

    * The ``expiration_time`` is set from the
      [`expiration_group`] ``cache_time`` option if ``expiration_group``
      is passed in and the value is set, or [`group`] ``cache_time`` if
      ``expiration_group`` is not passed in and the value is set, or
      ``[cache] expiration_time`` otherwise.

    Example usage::

        import oslo_cache.core

        MEMOIZE = oslo_cache.core.get_memoization_decorator(conf,
                                                            group='group1')

        @MEMOIZE
        def function(arg1, arg2):
            ...


        ALTERNATE_MEMOIZE = oslo_cache.core.get_memoization_decorator(
            conf, group='group2', expiration_group='group3')

        @ALTERNATE_MEMOIZE
        def function2(arg1, arg2):
            ...

    :param conf: config object, must have had :func:`configure` called on it.
    :type conf: oslo_config.cfg.ConfigOpts
    :param region: region as created by :func:`create_region`.
    :param group: name of the configuration group to examine
    :type group: string
    :param expiration_group: name of the configuration group to examine
                             for the expiration option. This will fall back to
                             using ``group`` if the value is unspecified or
                             ``None``
    :type expiration_group: string
    :rtype: function reference
    """
    if expiration_group is None:
        expiration_group = group
    should_cache = _get_should_cache_fn(conf, group)
    expiration_time = _get_expiration_time_fn(conf, expiration_group)

    memoize = region.cache_on_arguments(should_cache_fn=should_cache,
                                        expiration_time=expiration_time)

    # Make sure the actual "should_cache" and "expiration_time" methods are
    # available. This is potentially interesting/useful to pre-seed cache
    # values.
    memoize.should_cache = should_cache
    memoize.get_expiration_time = expiration_time

    return memoize


def configure(conf):
    """Configure the library.

    This must be called before conf().

    The following backends are registered in :mod:`dogpile.cache`:

    * ``oslo_cache.noop`` - :class:`oslo_cache.backends.noop.NoopCacheBackend`
    * ``oslo_cache.mongo`` -
      :class:`oslo_cache.backends.mongo.MongoCacheBackend`
    * ``oslo_cache.memcache_pool`` -
      :class:`oslo_cache.backends.memcache_pool.PooledMemcachedBackend`
    * ``oslo_cache.dict`` -
      :class:`oslo_cache.backends.dictionary.DictCacheBackend`

    :param conf: The configuration object.
    :type conf: oslo_config.cfg.ConfigOpts

    """
    _opts.configure(conf)

    for backend in _BACKENDS:
        dogpile.cache.register_backend(*backend)
