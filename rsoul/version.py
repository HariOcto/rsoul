"""Release identity independent of the running container/image tag.

__version__ follows Semantic Versioning. A hyphenated suffix (``-beta.1``, ``-rc.1``)
marks a pre-release; drop it for the final release and tag the commit ``v<version>``.
"""

__version__ = "0.1.0-beta.1"
STATE_SCHEMA = 2
# A pre-release is labelled as such wherever R:soul reports its version
BUILD_ID = "pre-release" if "-" in __version__ else "release"
