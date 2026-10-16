"""
Patches gevent first, for the whole suite.

The application does this in server.py before any other import, and the test
suite has to match: tests/test_socks_shim.py runs a cooperative server against
a blocking PySocks upstream in one hub, which only works if the standard
library was patched before requests, gevent.server and friends were imported.

Until Этап 5 that module could patch for itself, because nothing imported ahead
of it pulled in the proxy stack. tests/test_alive.py does (engine.alive imports
the provider registry), and under `unittest discover` it is imported first:
alphabetical order, with no say in it. The result was four shim failures and a
suite that took 150 seconds instead of 0.2 - visible only when running the
whole suite, never when running that module alone.
"""

from gevent import monkey

monkey.patch_all()
