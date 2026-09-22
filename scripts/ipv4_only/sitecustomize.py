"""Force Python's name resolution to IPv4 for the process that imports this.

Some hosts advertise IPv6 routes that silently drop traffic. ``curl`` survives it
because Happy Eyeballs races both families and abandons the loser after a few
hundred milliseconds; CPython's ``socket.create_connection`` does not race. It
walks ``getaddrinfo`` in order and blocks on the full TCP timeout for every dead
AAAA record before trying an A record.

For a voice deployment this shows up as a start that appears to hang: the pipeline
is not loading models, it is waiting on a connection to an address that will never
answer. Model and asset downloads are the usual victims.

Enable it by putting this directory on ``PYTHONPATH``, which makes CPython import
it automatically at startup::

    PYTHONPATH=scripts/ipv4_only python -m whatever

It is a workaround, not a fix. The fix is to stop advertising a route that does not
work. Keep it out of any environment whose services actually need IPv6.
"""

import socket

_original_getaddrinfo = socket.getaddrinfo


def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    # AF_UNSPEC means "caller has no preference", which is the case we narrow.
    # A caller that explicitly asked for AF_INET6 gets what it asked for.
    if family == 0:
        family = socket.AF_INET
    return _original_getaddrinfo(host, port, family, type, proto, flags)


socket.getaddrinfo = _ipv4_only_getaddrinfo
