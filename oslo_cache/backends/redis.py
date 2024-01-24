from dogpile.cache.backends import redis as redis_backend

__all__ = [
    'RedisSentinelBackend'
]


class RedisSentinelBackend(redis_backend.RedisSentinelBackend):
    def __init__(self, arguments):
        """
        Wrapper class for dogpile.cache.redis_sentinel.

        Currently only translates arguments for
        dogpile.cache.backends.RedisSentinelBackend
        because user is unable to pass them using oslo
        config in it's current format
        Arguments:


           :param sentinels: list (required), of sentinels
               formated as sentinel1IP:port,sentinel2IP:port
           :param ssl: bool, turns on TLS, needs to
               be True if you pass other ssl arguments
           :param ssl-<keyfile,certfile,ca_certs>: TLS certificate, key and CA
           :param username,password: login credentials
        """
        # Reformat sentinels into (ip <string>, port <int>)
        # as required by dogpile.cahce.redis_sentinel
        sentinels = [
            (host, int(port))
            for host, port in [
                host_port.rsplit(":", 1)
                for host_port in arguments["sentinels"].split(",")
            ]
        ]
        # Username and password must be in connection_kwargs dictionary
        reformated_kwargs = dict(
            username=arguments['username'],
            password=arguments['password']
        )
        if 'ssl' in arguments and arguments['ssl']:
            reformated_kwargs.update(
                ssl=arguments['ssl'],
                ssl_certfile=arguments['ssl_certfile'],
                ssl_keyfile=arguments['ssl_keyfile'],
                ssl_ca_certs=arguments['ssl_ca_certs']
            )
        # Both connection and sentinel kwargs are needed
        arguments.update(
            sentinels=sentinels,
            connection_kwargs=reformated_kwargs,
            sentinel_kwargs=reformated_kwargs
        )
        # Usename and password must be deleted from arguments dict
        # after being added to connection_kwargs, otherwise there
        # will be AUTH error
        del arguments['username']
        del arguments['password']
        return super(RedisSentinelBackend, self).__init__(arguments)
