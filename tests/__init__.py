"""
Patches gevent first, for the whole suite.

The application does this in server.py before any other import, and the test
suite has to match: tests/test_socks_shim.py runs a cooperative server against
a blocking PySocks upstream in one hub, which only works if the standard
library was patched before requests, gevent.server and friends were imported.

Patching from inside that module is not enough: `unittest discover` imports
test modules in alphabetical order, and an earlier one (tests/test_alive.py,
through the provider registry) already pulls in the proxy stack. The symptom is
four shim failures and a suite that takes 150 seconds instead of 0.2 - visible
only when running everything at once, never when running that module alone.

Discovery therefore has to import the suite as a package, which means running
it as `python -m unittest discover -s tests -t .`; without `-t .` the modules
are loaded as top-level and this file never executes.
"""

from gevent import monkey

monkey.patch_all()
